"""Loopback-only read-only Tablo browser interface. Credentials remain in memory."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from html import escape
import io
import json
import os
import re
import select
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
from urllib.parse import parse_qs, quote, urlsplit

from tablo import TabloClient, TabloError, scheduled_airings


STYLE = """
:root{font:16px system-ui,sans-serif;color-scheme:dark;background:#0d1720;color:#e9f3fa}
body{max-width:980px;margin:0 auto;padding:28px 20px}a{color:#83dcf3}
nav{display:flex;gap:16px;flex-wrap:wrap;margin:18px 0 28px}nav a{background:#1c3440;padding:9px 12px;border-radius:8px;text-decoration:none}
.card{background:#162a36;border:1px solid #315363;border-radius:12px;padding:15px 18px;margin:12px 0}
.card h2{font-size:19px;margin:0 0 6px}.muted{color:#a9c0cb}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}
input,button{font:inherit;padding:10px;border-radius:7px}input{display:block;width:min(100%,400px);box-sizing:border-box;margin:6px 0 14px}
button{background:#42bcdb;color:#06202b;border:0;cursor:pointer}details{margin-top:12px}summary{cursor:pointer}
.player-controls{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin:10px 0}
.player-controls button{padding:7px 10px}.player-controls input[type=range]{width:auto;min-width:180px;flex:1;margin:0}
.player-controls .icon-button{width:34px;height:34px;padding:0;display:grid;place-items:center;background:transparent;color:inherit}
.player-controls .icon-button:hover{background:#1c3440}.player-controls .icon-button:focus-visible{outline:2px solid #83dcf3;outline-offset:2px}
.icon-button svg{width:20px;height:20px;fill:currentColor}
#toggle[data-state="play"] .icon-pause,#toggle[data-state="pause"] .icon-play,#mute[data-state="unmuted"] .icon-muted,#mute[data-state="muted"] .icon-volume{display:none}
.video-wrap{position:relative;aspect-ratio:16/9;background:#000}.video-wrap video{display:block;width:100%;height:100%;object-fit:contain;background:#000}
.loading-overlay{position:absolute;inset:0;display:grid;place-items:center;background:#0d1720 center/contain no-repeat;color:#fff;font-weight:600;pointer-events:none}
.loading-overlay[hidden]{display:none}
.status-hero{display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap}
.status-hero h2{margin:0 0 4px}.status-pill{background:#1c594b;color:#d5fff2;border-radius:999px;padding:6px 12px;font-weight:700}
.status-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin:18px 0}
.status-grid>div{background:#162a36;border:1px solid #315363;border-radius:12px;padding:14px 16px}
.status-grid dt{color:#a9c0cb;font-size:13px;margin-bottom:6px}.status-grid dd{margin:0;font-weight:600;overflow-wrap:anywhere}
"""
HLS_SCRIPT = os.path.join(os.path.dirname(__file__), "vendor", "node_modules", "hls.js", "dist", "hls.min.js")
PLAY_BUTTON = ('<button type="button" class="icon-button" id="toggle" aria-label="Play" title="Play" data-state="play">'
               '<svg class="icon-play" viewBox="0 0 24 24" aria-hidden="true"><path d="M7 4v16l13-8z"/></svg>'
               '<svg class="icon-pause" viewBox="0 0 24 24" aria-hidden="true"><path d="M5 4h5v16H5zm9 0h5v16h-5z"/></svg></button>')
MUTE_BUTTON = ('<button type="button" class="icon-button" id="mute" aria-label="Mute" title="Mute" data-state="unmuted">'
               '<svg class="icon-volume" viewBox="0 0 24 24" aria-hidden="true"><path d="M3 9v6h4l5 4V5L7 9z"/>'
               '<path d="M16 8a6 6 0 0 1 0 8m2-11a10 10 0 0 1 0 14" fill="none" stroke="currentColor" stroke-width="2"/></svg>'
               '<svg class="icon-muted" viewBox="0 0 24 24" aria-hidden="true"><path d="M3 9v6h4l5 4V5L7 9z"/>'
               '<path d="m16 9 5 6m0-6-5 6" fill="none" stroke="currentColor" stroke-width="2"/></svg></button>')
FULLSCREEN_BUTTON = ('<button type="button" class="icon-button" id="fullscreen" aria-label="Fullscreen" title="Fullscreen">'
                     '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 3h7v2H5v5H3zm11 0h7v7h-2V5h-5zM3 14h2v5h5v2H3zm16 0h2v7h-7v-2h5z"/></svg></button>')


def page(title, content, signed_in=False):
    nav = '<nav><a href="/">Home</a><a href="/status">Status</a><a href="/channels">Channels</a><a href="/recordings">Recordings</a><a href="/schedule">Scheduled guide</a></nav>' if signed_in else ""
    signout = '<form method="post" action="/logout"><button>Sign out</button></form>' if signed_in else ""
    return (f'<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{escape(title)} · Tablo</title><style>{STYLE}</style><body><h1>{escape(title)}</h1>{nav}{content}{signout}</body></html>').encode()


def item_title(obj):
    details = obj.get("airing_details") or {}
    channel = obj.get("channel") or {}
    episode = obj.get("episode") or {}
    movie = obj.get("movie_airing") or {}
    name = details.get("show_title") or movie.get("title") or channel.get("name") or channel.get("call_sign")
    if not name and (channel.get("major") is not None):
        name = f"Channel {channel['major']}.{channel.get('minor', 0)}"
    name = str(name or obj.get("path") or "Item")
    if episode.get("title") and episode["title"] != name:
        name += " — " + str(episode["title"])
    return name


def ffmpeg_command(playlist_url):
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
        "-readrate", "3", "-i", playlist_url,
        "-vf", "yadif=deint=interlaced",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-ac", "2", "-b:a", "128k",
        "-movflags", "frag_keyframe+empty_moov+default_base_moof",
        "-f", "mp4", "pipe:1",
    ]


def hls_command(playlist_url, playlist_file, start=0):
    folder = os.path.dirname(playlist_file)
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-readrate", "1"]
    if start:
        command.extend(["-ss", str(int(start))])
    return command + [
        "-i", playlist_url,
        "-vf", "yadif=deint=interlaced",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-maxrate", "3000k", "-bufsize", "6000k", "-pix_fmt", "yuv420p",
        "-force_key_frames", "expr:gte(t,n_forced*4)",
        "-c:a", "aac", "-ac", "2", "-b:a", "128k",
        "-hls_time", "4", "-hls_list_size", "8",
        "-hls_flags", "delete_segments+temp_file+independent_segments",
        "-hls_segment_filename", os.path.join(folder, "segment%05d.ts"),
        "-f", "hls", playlist_file,
    ]


def summarize_hls(manifest, returncode, stderr):
    durations = []
    for line in manifest.splitlines():
        if line.startswith("#EXTINF:"):
            try:
                durations.append(float(line.split(":", 1)[1].split(",", 1)[0]))
            except ValueError:
                pass
    duration = sum(durations)
    if returncode is None:
        return {"state": "preparing" if not durations else "streaming", "seconds": duration}
    if returncode != 0 or ("#EXT-X-ENDLIST" in manifest and duration < 2):
        detail = re.sub(r"https?://\S+", "[stream URL]", stderr.strip())[-1000:]
        message = detail or f"Tablo stream ended before usable video arrived ({duration:.1f} seconds)."
        return {"state": "failed", "seconds": duration, "message": message}
    return {"state": "finished", "seconds": duration}


def read_stream_chunk(stream, timeout):
    try:
        ready, _, _ = select.select([stream], [], [], timeout)
    except (io.UnsupportedOperation, ValueError):
        return stream.read(65536)
    if not ready:
        raise TimeoutError("Playback stream stalled")
    return stream.read(65536)


def render_cards(title, items, playable=False):
    if not items:
        return '<p class="muted">No items were returned.</p>'
    out = [f'<p class="muted">{len(items)} items</p>']
    for obj in items:
        details = obj.get("airing_details") or {}
        when = escape(str(details.get("datetime") or ""))
        raw = escape(json.dumps(obj, indent=2, ensure_ascii=False))
        play = ""
        if playable and isinstance(obj.get("path"), str):
            try:
                duration = max(0, min(172800, int(details.get("duration") or 0)))
            except (TypeError, ValueError):
                duration = 0
            play_url = f'/watch?path={quote(obj["path"], safe="")}'
            if duration:
                play_url += f'&duration={duration}'
            play = f'<p><a href="{play_url}">Play recording</a></p>'
        out.append(f'<article class="card"><h2>{escape(item_title(obj))}</h2><div class="muted">{when}</div>'
                   f'{play}<details><summary>Details</summary><pre>{raw}</pre></details></article>')
    return "".join(out)


def render_status(info):
    model = info.get("model") if isinstance(info.get("model"), dict) else {}

    def shown(value):
        return escape(str(value)) if value is not None and value != "" else "Unknown"

    def field(label, value):
        return f'<div><dt>{escape(label)}</dt><dd>{shown(value)}</dd></div>'

    availability = info.get("availability")
    availability = str(availability).replace("_", " ").capitalize() if availability else "Unknown"
    setup = info.get("setup_completed")
    setup_text = "Setup complete" if setup is True else "Setup incomplete" if setup is False else "Unknown"
    tuners = model.get("tuners")
    tuner_text = f'{tuners} tuner' + ("" if tuners == 1 else "s") if isinstance(tuners, int) and not isinstance(tuners, bool) else "Unknown"
    wifi = model.get("wifi")
    wifi_text = "Wi-Fi capable" if wifi is True else "No Wi-Fi support reported" if wifi is False else "Unknown"
    fields = "".join((
        field("Setup", setup_text), field("Firmware", info.get("version")),
        field("Build", info.get("build_number")), field("Tuners", tuner_text),
        field("Wi-Fi support", wifi_text), field("Local address", info.get("local_address")),
        field("Server ID", info.get("server_id")),
    ))
    raw = escape(json.dumps(info, indent=2, ensure_ascii=False))
    return ('<section aria-label="Device overview"><h2>Device overview</h2>'
            f'<div class="card status-hero"><div><h2>{shown(info.get("name"))}</h2>'
            f'<div class="muted">{shown(model.get("name"))}</div></div>'
            f'<span class="status-pill">{escape(availability)}</span></div>'
            f'<dl class="status-grid">{fields}</dl></section>'
            '<details><summary>Technical details</summary><pre class="card">' + raw + '</pre></details>')


class AppState:
    def __init__(self, client=None):
        self.client = client or TabloClient()
        self.session = None
        self.lock = threading.Lock()
        self.start_lock = threading.Lock()
        self.schedule = None
        self.schedule_loading = False
        self.schedule_error = None
        self.generation = 0
        self.stream_gate = threading.BoundedSemaphore(1)
        self.playback = None

    def stop_hls(self, playback):
        if playback is None:
            return
        process = playback["process"]
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
        playback["errors"].close()
        shutil.rmtree(playback["folder"], ignore_errors=True)

    def start_hls(self, recording, session, start=0):
        with self.start_lock:
            with self.lock:
                if self.session != session:
                    raise TabloError("Session expired")
                previous = self.playback
                self.playback = None
            self.stop_hls(previous)
            playlist = self.client.watch_recording(recording)
            folder = tempfile.mkdtemp(prefix="tablo-hls-")
            errors = tempfile.TemporaryFile()
            try:
                process = subprocess.Popen(
                    hls_command(playlist, os.path.join(folder, "index.m3u8"), start),
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=errors,
                )
            except OSError:
                errors.close()
                shutil.rmtree(folder, ignore_errors=True)
                raise
            playback = {"token": secrets.token_urlsafe(20), "folder": folder,
                        "process": process, "errors": errors, "last": time.monotonic()}
            with self.lock:
                valid = self.session == session
                if valid:
                    self.playback = playback
            if not valid:
                self.stop_hls(playback)
                raise TabloError("Session expired")
            threading.Thread(target=self.watch_hls_idle, args=(playback,), daemon=True).start()
            return playback["token"]

    def watch_hls_idle(self, playback):
        while True:
            time.sleep(5)
            with self.start_lock:
                with self.lock:
                    if self.playback is not playback:
                        return
                    if time.monotonic() - playback["last"] <= 45:
                        continue
                    self.playback = None
                self.stop_hls(playback)
                return

    def hls_data(self, token, filename, session):
        if filename != "index.m3u8" and not re.fullmatch(r"segment\d{5}\.ts", filename):
            raise TabloError("Invalid stream asset")
        with self.lock:
            playback = self.playback
            if not playback or playback["token"] != token or self.session != session:
                raise TabloError("Playback session expired")
            playback["last"] = time.monotonic()
        target = os.path.join(playback["folder"], filename)
        deadline = time.monotonic() + (25 if filename == "index.m3u8" else 15)
        while not os.path.isfile(target):
            if playback["process"].poll() is not None:
                playback["errors"].seek(0)
                detail = playback["errors"].read(2000).decode("utf-8", errors="replace").strip()
                raise TabloError(detail or "Playback conversion stopped")
            if time.monotonic() >= deadline:
                raise TabloError("Timed out waiting for video segments")
            time.sleep(0.2)
        try:
            with open(target, "rb") as stream:
                return stream.read()
        except FileNotFoundError as exc:
            raise TabloError("Playback session expired") from exc

    def hls_status(self, token, session):
        with self.lock:
            playback = self.playback
            if not playback or playback["token"] != token or self.session != session:
                raise TabloError("Playback session expired")
            playback["last"] = time.monotonic()
        try:
            with open(os.path.join(playback["folder"], "index.m3u8"), encoding="utf-8") as playlist:
                manifest = playlist.read()
        except FileNotFoundError:
            manifest = ""
        returncode = playback["process"].poll()
        stderr = ""
        if returncode is not None:
            try:
                playback["errors"].seek(0)
                stderr = playback["errors"].read(10000).decode("utf-8", errors="replace")
            except ValueError:
                raise TabloError("Playback session expired")
        return summarize_hls(manifest, returncode, stderr)

    def login(self, email, password):
        self.client.login(email, password)
        self.client.server_info()
        with self.lock:
            self.generation += 1
            self.session = secrets.token_urlsafe(32)
            self.schedule = None
            self.schedule_error = None
            self.schedule_loading = False
        return self.session

    def valid_session(self, cookie):
        with self.lock:
            return bool(self.session and cookie == self.session)

    def logout(self):
        with self.lock:
            self.generation += 1
            self.session = None
            self.schedule = None
            self.schedule_error = None
            self.schedule_loading = False
            self.client = TabloClient(self.client.config) if isinstance(self.client, TabloClient) else type(self.client)()
            playback = self.playback
            self.playback = None
        self.stop_hls(playback)

    def load_schedule(self, client, generation):
        try:
            all_airings = client.guide_airings()
            result = scheduled_airings(all_airings)
            with self.lock:
                if generation == self.generation:
                    self.schedule = result
                    self.schedule_error = None
        except Exception as exc:
            with self.lock:
                if generation == self.generation:
                    self.schedule_error = str(exc)
        finally:
            with self.lock:
                if generation == self.generation:
                    self.schedule_loading = False

    def schedule_snapshot(self):
        with self.lock:
            if self.schedule is None and not self.schedule_loading and not self.schedule_error:
                self.schedule_loading = True
                threading.Thread(target=self.load_schedule, args=(self.client, self.generation), daemon=True).start()
            return self.schedule, self.schedule_loading, self.schedule_error


def make_server(host, port, state):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def respond(self, code, title, content, signed_in=False, cookie=None, refresh=None):
            body = page(title, content, signed_in)
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            if cookie:
                self.send_header("Set-Cookie", cookie)
            if refresh:
                self.send_header("Refresh", str(refresh))
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def redirect(self, where, cookie=None):
            self.send_response(303)
            self.send_header("Location", where)
            self.send_header("Cache-Control", "no-store")
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def session_cookie(self):
            for part in self.headers.get("Cookie", "").split(";"):
                key, _, value = part.strip().partition("=")
                if key == "tablo_session":
                    return value
            return None

        def authenticated(self):
            return state.valid_session(self.session_cookie())

        def recording_path(self):
            return parse_qs(urlsplit(self.path).query).get("path", [""])[0]

        def stream_recording(self):
            if not state.stream_gate.acquire(blocking=False):
                self.respond(429, "Playback busy", '<p>Another recording is playing. Stop it and try again.</p>', True)
                return
            session = self.session_cookie()
            process = None
            try:
                with tempfile.TemporaryFile() as errors:
                    try:
                        playlist = state.client.watch_recording(self.recording_path())
                        process = subprocess.Popen(
                            ffmpeg_command(playlist), stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=errors, bufsize=0,
                        )
                        first = read_stream_chunk(process.stdout, 20)
                        if not first:
                            errors.seek(0)
                            detail = errors.read(2000).decode("utf-8", errors="replace").strip()
                            raise TabloError(detail or "FFmpeg could not start this recording")
                    except (TabloError, OSError, TimeoutError) as exc:
                        self.respond(502, "Playback error", '<p>' + escape(str(exc)) + '</p>', True)
                        return
                    self.send_response(200)
                    self.send_header("Content-Type", "video/mp4")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.close_connection = True
                    self.wfile.write(first)
                    while state.valid_session(session):
                        try:
                            chunk = read_stream_chunk(process.stdout, 30)
                        except TimeoutError:
                            break
                        if not chunk:
                            break
                        self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                if process is not None:
                    process.stdout.close()
                    if process.poll() is None:
                        process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=2)
                state.stream_gate.release()

        def do_GET(self):
            path = urlsplit(self.path).path
            if not self.authenticated():
                if path != "/":
                    self.respond(403, "Sign in required", '<p><a href="/">Open sign in</a></p>')
                    return
                form = ('<p>Sign in with the same 4th-generation account used by the Tablo app. '
                        'The password is sent to Tablo over HTTPS and is not saved to disk.</p>'
                        '<form method="post" action="/login"><label>Email<input name="email" type="email" required autocomplete="username"></label>'
                        '<label>Password<input name="password" type="password" required autocomplete="current-password"></label>'
                        '<button>Connect to Tablo</button></form>')
                self.respond(200, "Connect to Tablo", form)
                return
            try:
                if path == "/":
                    device_host = urlsplit(state.client.DEVICE_URL).hostname
                    content = (f'<p>Connected to the Tablo at {escape(device_host or "unknown")}. Choose a view above.</p>'
                               '<p class="muted">This interface is read-only and available only on this computer at 127.0.0.1.</p>')
                    self.respond(200, "Your Tablo", content, True)
                elif path == "/status":
                    info = state.client.server_info()
                    self.respond(200, "Device status", render_status(info), True)
                elif path == "/channels":
                    items = state.client.channels()
                    self.respond(200, "Channels", render_cards("Channels", items), True)
                elif path == "/recordings":
                    items = state.client.recordings()
                    self.respond(200, "Recordings", render_cards("Recordings", items, playable=True), True)
                elif path == "/watch":
                    recording = self.recording_path()
                    if not recording.startswith("/recordings/"):
                        self.respond(400, "Invalid recording", "", True)
                    else:
                        params = parse_qs(urlsplit(self.path).query)
                        try:
                            duration = int(params.get("duration", ["0"])[0])
                            start = int(params.get("start", ["0"])[0])
                        except ValueError:
                            self.respond(400, "Invalid playback position", "", True)
                            return
                        if not (0 <= duration <= 172800 and 0 <= start < (duration or 1)):
                            self.respond(400, "Invalid playback position", "", True)
                            return
                        seek_base = "/watch?path=" + quote(recording, safe="") + f"&duration={duration}"
                        if "start" not in params:
                            progress_key = "tablo_progress:" + quote(recording, safe="")
                            script = ('<script>let saved=0;try{saved=Number(localStorage.getItem('
                                      + json.dumps(progress_key) + '));}catch(e){}'
                                      f'const duration={duration};'
                                      'const position=Number.isFinite(saved)&&saved>=15&&saved<duration-15?Math.floor(saved):0;'
                                      'location.replace(' + json.dumps(seek_base) + '+"&start="+position);</script>')
                            content = ('<p>Opening recording…</p>' + script
                                       + f'<p><a href="{escape(seek_base)}&amp;start=0">Start from beginning</a></p>')
                            self.respond(200, "Opening recording", content, True)
                            return
                        token = state.start_hls(recording, self.session_cookie(), start)
                        media_url = "/hls/" + token + "/index.m3u8"
                        seek_limit = max(0, duration - 10)
                        seek_controls = (f'<div class="player-controls">{PLAY_BUTTON}'
                                         f'<label for="seek">Jump to approximate time</label><input id="seek" type="range" min="0" max="{seek_limit}" value="{min(start, seek_limit)}" aria-label="Approximate recording position">'
                                         f'<span id="time"></span>{MUTE_BUTTON}{FULLSCREEN_BUTTON}</div>') if duration else (
                                         f'<div class="player-controls">{PLAY_BUTTON}{MUTE_BUTTON}{FULLSCREEN_BUTTON}</div>'
                                         '<p class="muted">Open this recording from Recordings to show its full timeline.</p>')
                        content = ('<p><a href="/recordings">Back to recordings</a></p>'
                                   '<div class="video-wrap"><video id="player" autoplay playsinline></video>'
                                   '<div id="loading-overlay" class="loading-overlay" role="status">Preparing video segments…</div></div>'
                                   + seek_controls +
                                   '<p id="player-status" class="muted">Preparing video segments…</p>'
                                   '<script src="/static/hls.min.js"></script>'
                                   '<script>const v=document.getElementById("player"),s=document.getElementById("player-status"),'
                                   f'url="{escape(media_url)}";'
                                   'const loading=document.getElementById("loading-overlay");'
                                   'try{const frame=sessionStorage.getItem("tablo_seek_frame");sessionStorage.removeItem("tablo_seek_frame");'
                                   'if(frame?.startsWith("data:image/jpeg;base64,")&&frame.length<500000){loading.style.backgroundImage=`linear-gradient(#0006,#0006),url("${frame}")`;loading.textContent="Seeking…";}}catch(e){}'
                                   'const toggle=document.getElementById("toggle"),mute=document.getElementById("mute");'
                                   'const setControl=(button,label,state)=>{button.setAttribute("aria-label",label);button.setAttribute("title",label);button.setAttribute("data-state",state);};'
                                   'try{v.muted=sessionStorage.getItem("tablo_muted")==="1";}catch(e){}'
                                   'setControl(toggle,"Play","play");setControl(mute,v.muted?"Unmute":"Mute",v.muted?"muted":"unmuted");'
                                   'toggle.addEventListener("click",()=>{if(v.paused)v.play();else v.pause();});'
                                   'mute.addEventListener("click",()=>{v.muted=!v.muted;setControl(mute,v.muted?"Unmute":"Mute",v.muted?"muted":"unmuted");'
                                   'try{sessionStorage.setItem("tablo_muted",v.muted?"1":"0");}catch(e){}});'
                                   'document.getElementById("fullscreen").addEventListener("click",()=>v.requestFullscreen?.());'
                                   'v.addEventListener("playing",()=>{s.textContent="Playing";setControl(toggle,"Pause","pause");loading.hidden=true;});'
                                   'v.addEventListener("pause",()=>setControl(toggle,"Play","play"));'
                                   'v.addEventListener("error",()=>{loading.hidden=true;s.textContent="Playback error (media code " + (v.error?.code ?? "unknown") + "). Refresh this page to try again.";});'
                                   'if(window.Hls && Hls.isSupported()){' 
                                   'const hls=new Hls();hls.loadSource(url);hls.attachMedia(v);'
                                   'hls.on(Hls.Events.MANIFEST_PARSED,()=>{s.textContent="Ready to play";v.play().catch(()=>{});});'
                                   'hls.on(Hls.Events.ERROR,(_,e)=>{if(e.fatal){loading.hidden=true;s.textContent="Playback error: " + e.details + ". Refresh this page to try again.";}});'
                                   '}else if(v.canPlayType("application/vnd.apple.mpegurl")){v.src=url;}'
                                   'else{s.textContent="This browser cannot play HLS video.";}'
                                   f'const statusUrl="/playback-status?token={token}";'
                                   'const check=setInterval(async()=>{try{const r=await fetch(statusUrl);const x=await r.json();'
                                   'if(x.state==="failed"){loading.hidden=true;s.textContent=x.message;clearInterval(check);}'
                                   '}catch(e){}},2000);'
                                   f'const seek=document.getElementById("seek"),seekBase={json.dumps(seek_base)},start={start},duration={duration};'
                                   'const clock=n=>{n=Math.floor(n);const h=Math.floor(n/3600),m=Math.floor(n%3600/60),s=n%60;'
                                   'return h?`${h}:${String(m).padStart(2,"0")}:${String(s).padStart(2,"0")}`:`${m}:${String(s).padStart(2,"0")}`;};'
                                   f'const progressKey="tablo_progress:"+{json.dumps(quote(recording, safe=""))};'
                                   'if(seek){let dragging=false,jumping=false,lastSave=-1;const label=document.getElementById("time");'
                                   'const saveProgress=(force=false)=>{if(jumping)return;const position=Math.floor(start+v.currentTime);'
                                   'if(!Number.isFinite(position)||position<1||position>=duration-15)return;'
                                   'const bucket=Math.floor(position/5);if(!force&&bucket===lastSave)return;'
                                   'try{localStorage.setItem(progressKey,String(position));lastSave=bucket;}catch(e){}};'
                                   'window.addEventListener("pagehide",()=>saveProgress(true));'
                                   'v.addEventListener("pause",()=>saveProgress(true));'
                                   'v.addEventListener("ended",()=>{if(start+v.currentTime>=duration-15){try{localStorage.removeItem(progressKey);}catch(e){}}});'
                                   'const update=n=>{if(!dragging)seek.value=Math.min(duration,n);label.textContent=`${clock(n)} / ${clock(duration)}`;};'
                                   'update(start);v.addEventListener("timeupdate",()=>{update(start+v.currentTime);saveProgress();});'
                                   'seek.addEventListener("input",()=>{dragging=true;label.textContent=`${clock(Number(seek.value))} / ${clock(duration)}`;});'
                                   'seek.addEventListener("change",()=>{jumping=true;try{localStorage.setItem(progressKey,String(Math.floor(Number(seek.value))));}catch(e){}'
                                   'try{if(v.videoWidth&&v.videoHeight){const c=document.createElement("canvas");'
                                   'c.width=Math.min(960,v.videoWidth);c.height=Math.round(c.width*v.videoHeight/v.videoWidth);'
                                   'c.getContext("2d").drawImage(v,0,0,c.width,c.height);sessionStorage.setItem("tablo_seek_frame",c.toDataURL("image/jpeg",0.7));}}catch(e){}'
                                   'loading.hidden=false;loading.textContent="Seeking…";'
                                   'location.assign(seekBase+"&start="+Math.floor(Number(seek.value)));});}'
                                   '</script>')
                        self.respond(200, "Recording player", content, True)
                elif path == "/playback-status":
                    token = parse_qs(urlsplit(self.path).query).get("token", [""])[0]
                    data = json.dumps(state.hls_status(token, self.session_cookie())).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                elif path == "/static/hls.min.js":
                    with open(HLS_SCRIPT, "rb") as script:
                        data = script.read()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/javascript; charset=utf-8")
                    self.send_header("Cache-Control", "public, max-age=86400")
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                elif path.startswith("/hls/"):
                    parts = path.split("/")
                    if len(parts) != 4:
                        self.respond(404, "Not found", "", True)
                        return
                    _, _, token, filename = parts
                    data = state.hls_data(token, filename, self.session_cookie())
                    self.send_response(200)
                    self.send_header("Content-Type", "application/vnd.apple.mpegurl" if filename == "index.m3u8" else "video/mp2t")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                elif path == "/media":
                    self.stream_recording()
                elif path == "/schedule":
                    items, loading, error = state.schedule_snapshot()
                    note = ('<p class="muted">Upcoming entries are inferred from the guide’s schedule state. '
                            'The full guide can take several minutes to load; leave this tab open.</p>')
                    if loading:
                        self.respond(200, "Scheduled guide", note + '<p>Loading guide… This page refreshes automatically.</p>', True, refresh=5)
                    elif error:
                        self.respond(200, "Scheduled guide", note + '<p>Could not load: ' + escape(error) + '</p>', True)
                    else:
                        self.respond(200, "Scheduled guide", note + render_cards("Scheduled guide", items or []), True)
                else:
                    self.respond(404, "Not found", "", True)
            except TabloError as exc:
                self.respond(502, "Tablo error", '<p>' + escape(str(exc)) + '</p>', True)

        def do_POST(self):
            path = urlsplit(self.path).path
            if path == "/logout":
                if self.authenticated():
                    state.logout()
                self.redirect("/", "tablo_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0")
                return
            if path != "/login":
                self.respond(404, "Not found", "")
                return
            if self.authenticated():
                self.redirect("/")
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 4096:
                    raise ValueError("Invalid sign-in form")
                form = parse_qs(self.rfile.read(length).decode("utf-8"))
                email = form.get("email", [""])[0].strip()
                password = form.get("password", [""])[0]
                if not email or not password:
                    raise ValueError("Email and password are required")
                session = state.login(email, password)
                self.redirect("/", f"tablo_session={session}; HttpOnly; SameSite=Strict; Path=/")
            except (TabloError, ValueError) as exc:
                self.respond(400, "Sign in failed", '<p>' + escape(str(exc)) + '</p><p><a href="/">Try again</a></p>')
    return ThreadingHTTPServer((host, port), Handler)


if __name__ == "__main__":
    server = make_server("127.0.0.1", 8787, AppState())
    print("Tablo browser: http://127.0.0.1:8787", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

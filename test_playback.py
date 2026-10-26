import http.cookiejar
import io
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener
import threading

from webapp import AppState, STYLE, ffmpeg_command, hls_command, make_server, read_stream_chunk, render_cards, summarize_hls


class FakeClient:
    DEVICE_URL = "http://tablo.lan:8887"
    def login(self, email, password):
        pass
    def server_info(self):
        return {"server_id": "SID_TEST"}
    def watch_recording(self, path):
        if path != "/recordings/series/episodes/3530286":
            raise AssertionError(path)
        return "http://192.0.2.10:8885/stream/abc.m3u8"


class FakeProcess:
    def __init__(self):
        self.stdout = io.BytesIO(b"fake-fragmented-mp4")
        self.returncode = 0
    def wait(self, timeout=None):
        return 0
    def poll(self):
        return 0
    def terminate(self):
        pass
    def kill(self):
        pass


class PlaybackTests(unittest.TestCase):
    def test_reopening_recording_stops_previous_stream_before_watch_request(self):
        events = []
        class Client(FakeClient):
            def watch_recording(self, path):
                events.append("watch")
                if path != "/recordings/sports/events/3456899":
                    raise AssertionError(path)
                return "http://192.0.2.10:8885/stream/abc.m3u8"
        class OldProcess:
            stopped = False
            def poll(self): return 0 if self.stopped else None
            def terminate(self):
                events.append("stop")
                self.stopped = True
            def wait(self, timeout=None): return 0
            def kill(self): self.stopped = True
        state = AppState(client=Client())
        session = state.login("person@example.com", "test")
        with tempfile.TemporaryDirectory() as folder, tempfile.TemporaryFile() as errors:
            state.playback = {"process": OldProcess(), "folder": folder, "errors": errors, "token": "old", "last": 0}
            with patch("webapp.subprocess.Popen", return_value=FakeProcess()):
                state.start_hls("/recordings/sports/events/3456899", session)
            self.assertEqual(events, ["stop", "watch"])
            state.logout()

    def test_player_keeps_full_video_area_while_seeking(self):
        # A fresh video has no intrinsic dimensions yet. Its wrapper must
        # reserve the player area so the previous-frame overlay cannot shrink.
        self.assertRegex(STYLE, r"\.video-wrap\{[^}]*aspect-ratio:16/9")
        self.assertRegex(STYLE, r"\.video-wrap video\{[^}]*height:100%")
        self.assertRegex(STYLE, r"\.video-wrap video\{[^}]*object-fit:contain")

    def test_stream_read_times_out_when_encoder_stalls(self):
        read_fd, write_fd = os.pipe()
        try:
            with os.fdopen(read_fd, "rb", buffering=0) as stream:
                with self.assertRaises(TimeoutError):
                    read_stream_chunk(stream, 0.01)
        finally:
            os.close(write_fd)

    def test_ffmpeg_outputs_browser_mp4_without_shell(self):
        command = ffmpeg_command("http://192.0.2.10:8885/stream/abc.m3u8")
        self.assertEqual(command[0], "ffmpeg")
        self.assertIn("libx264", command)
        self.assertNotIn("h264_videotoolbox", command)
        self.assertIn("aac", command)
        self.assertIn("pipe:1", command)
        self.assertIn("frag_keyframe+empty_moov+default_base_moof", command)
        self.assertEqual(command[command.index("-i") + 1], "http://192.0.2.10:8885/stream/abc.m3u8")

    def test_secondary_mp4_encoder_produces_h264_on_installed_ffmpeg(self):
        source = Path(__file__).parent / "test_media" / "input.m3u8"
        result = subprocess.run(ffmpeg_command(str(source)), capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        self.assertTrue(result.stdout)
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name",
             "-of", "default=noprint_wrappers=1", "-i", "pipe:0"],
            input=result.stdout, capture_output=True, timeout=10,
        )
        self.assertEqual(probe.returncode, 0, probe.stderr.decode(errors="replace"))
        self.assertIn(b"codec_name=h264", probe.stdout)

    def test_hls_encoder_creates_browser_compatible_segments(self):
        command = hls_command("http://192.0.2.10:8885/stream/abc.m3u8", "/tmp/test-index.m3u8")
        self.assertEqual(command[0], "ffmpeg")
        self.assertIn("libx264", command)
        self.assertIn("veryfast", command)
        self.assertIn("aac", command)
        self.assertIn("hls", command)
        self.assertIn("delete_segments+temp_file+independent_segments", command)
        self.assertEqual(command[-1], "/tmp/test-index.m3u8")
        seek = hls_command("http://192.0.2.10:8885/stream/abc.m3u8", "/tmp/test-index.m3u8", start=120)
        self.assertEqual(seek[seek.index("-ss") + 1], "120")
        self.assertLess(seek.index("-ss"), seek.index("-i"))

    def test_hls_encoder_produces_playlist_and_video_segments(self):
        source = Path(__file__).parent / "test_media" / "input.m3u8"
        with tempfile.TemporaryDirectory() as folder:
            playlist = Path(folder) / "index.m3u8"
            result = subprocess.run(hls_command(str(source), str(playlist)), capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
            manifest = playlist.read_text()
            self.assertIn("#EXTM3U", manifest)
            self.assertIn("segment00000.ts", manifest)
            segment = Path(folder) / "segment00000.ts"
            self.assertTrue(segment.is_file())
            probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name", "-of", "default=noprint_wrappers=1", str(segment)], capture_output=True, timeout=10)
            self.assertIn(b"codec_name=h264", probe.stdout)

    def test_hls_encoder_seeks_into_recording(self):
        source = Path(__file__).parent / "test_media" / "input.m3u8"
        with tempfile.TemporaryDirectory() as folder:
            playlist = Path(folder) / "index.m3u8"
            result = subprocess.run(hls_command(str(source), str(playlist), start=1), capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
            self.assertTrue(playlist.is_file())
            segment = Path(folder) / "segment00000.ts"
            self.assertTrue(segment.is_file())
            probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(segment)], capture_output=True, timeout=10)
            self.assertLess(float(probe.stdout.strip()), 3)

    def test_short_finished_hls_is_reported_as_playback_failure(self):
        playlist = "#EXTM3U\n#EXTINF:0.000000,\nsegment00000.ts\n#EXT-X-ENDLIST\n"
        result = summarize_hls(playlist, 0, "")
        self.assertEqual(result["state"], "failed")
        self.assertIn("before usable video", result["message"])

    def test_recording_cards_have_play_link(self):
        path = "/recordings/series/episodes/3530286"
        html = render_cards("Recordings", [{"path": path, "airing_details": {"show_title": "A & B", "duration": 5400}}], playable=True)
        self.assertIn("Play recording", html)
        self.assertIn(quote(path, safe=""), html)
        self.assertIn("A &amp; B", html)
        self.assertIn("duration=5400", html)

    def test_player_and_media_require_session_and_stream_bytes(self):
        state = AppState(client=FakeClient())
        server = make_server("127.0.0.1", 0, state)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        opener = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
        path = quote("/recordings/series/episodes/3530286", safe="")
        try:
            with self.assertRaises(HTTPError) as denied:
                opener.open(base + "/media?path=" + path)
            self.assertEqual(denied.exception.code, 403)
            payload = urlencode({"email": "person@example.com", "password": "test"}).encode()
            opener.open(Request(base + "/login", data=payload, method="POST"))
            with patch.object(state, "start_hls") as preflight_start:
                preflight = opener.open(base + "/watch?path=" + path + "&duration=5400").read().decode()
            preflight_start.assert_not_called()
            preflight_script = re.search(r"<script>(.*?)</script>", preflight, re.S).group(1)
            resume_harness = r'''
const script=process.argv[1];let destination="",saved=93;
global.localStorage={getItem:()=>saved};
global.location={replace:url=>destination=url};
eval(script);
if(!destination.includes("&start=93"))process.exit(1);
destination="";saved=null;eval(script);
if(!destination.includes("&start=0"))process.exit(2);
'''
            result = subprocess.run(["node", "-e", resume_harness, preflight_script], capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
            with patch.object(state, "start_hls", return_value="test-token") as start_hls:
                player = opener.open(base + "/watch?path=" + path + "&duration=5400&start=0").read().decode()
            start_hls.assert_called_once()
            self.assertIn("<video", player)
            self.assertIn("/hls/test-token/index.m3u8", player)
            self.assertIn('src="/static/hls.min.js"', player)
            self.assertIn("Hls.isSupported()", player)
            self.assertIn("/playback-status?token=test-token", player)
            self.assertIn('type="range"', player)
            self.assertNotIn('id="resume"', player)
            for control, label in (("toggle", "Play"), ("mute", "Mute"), ("fullscreen", "Fullscreen")):
                self.assertRegex(player, rf'<button[^>]*id="{control}"[^>]*aria-label="{label}"')
            self.assertEqual(player.count('class="icon-button"'), 3)
            icon_style = re.search(r'\.player-controls \.icon-button\{([^}]*)\}', player).group(1)
            self.assertIn('background:transparent', icon_style)
            self.assertIn('color:inherit', icon_style)
            self.assertIn('width:34px;height:34px', icon_style)
            self.assertNotIn('id="player" controls', player)
            with patch.object(state, "start_hls", return_value="seek-token") as seek_hls:
                seek_page = opener.open(base + "/watch?path=" + path + "&duration=5400&start=1200").read().decode()
            self.assertIn('max="5390"', seek_page)
            self.assertIn('value="1200"', seek_page)
            self.assertEqual(seek_hls.call_args.args[2], 1200)
            other_path = quote("/recordings/series/episodes/999", safe="")
            with patch.object(state, "start_hls", return_value="other-token"):
                other_page = opener.open(base + "/watch?path=" + other_path + "&duration=5400&start=0").read().decode()
            self.assertIn('id="loading-overlay"', seek_page)
            self.assertIn("sessionStorage.setItem", seek_page)
            self.assertIn("loading.hidden=true", seek_page)
            self.assertIn('if(e.fatal){loading.hidden=true;', seek_page)
            self.assertIn('if(x.state==="failed"){loading.hidden=true;', seek_page)
            script_source = re.search(r"<script>(.*?)</script>", seek_page, re.S).group(1)
            script_check = subprocess.run(["node", "--check"], input=script_source.encode(), capture_output=True, timeout=5)
            self.assertEqual(script_check.returncode, 0, script_check.stderr.decode(errors="replace"))
            self.assertIn('sessionStorage.getItem("tablo_muted")', script_source)
            self.assertIn('sessionStorage.setItem("tablo_muted"', script_source)
            mute_harness = r'''
const script=process.argv[1], storage=new Map();
function boot(){
  const handlers={};
  const video={muted:false,paused:true,canPlayType:()=>true,addEventListener:(n,f)=>handlers[`video:${n}`]=f};
  const control=id=>({attributes:{},setAttribute(k,v){this.attributes[k]=v},addEventListener:(n,f)=>handlers[`${id}:${n}`]=f});
  const elements={player:video,"player-status":{textContent:""},"loading-overlay":{style:{},hidden:false,textContent:""},
    toggle:control("toggle"),mute:control("mute"),fullscreen:control("fullscreen")};
  global.document={getElementById:id=>elements[id]??null};
  global.window={Hls:null,addEventListener:()=>{}};
  global.sessionStorage={getItem:k=>storage.get(k)??null,setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)};
  global.setInterval=()=>1;
  global.location={assign:()=>{}};
  eval(script);
  return {video,handlers,elements};
}
const first=boot();first.handlers["mute:click"]();
if(!first.video.muted||storage.get("tablo_muted")!=="1"||first.elements.mute.attributes["aria-label"]!=="Unmute")process.exit(1);
const second=boot();
if(!second.video.muted||second.elements.mute.attributes["aria-label"]!=="Unmute")process.exit(2);
second.handlers["mute:click"]();
if(second.video.muted||storage.get("tablo_muted")!=="0"||second.elements.mute.attributes["aria-label"]!=="Mute")process.exit(3);
const third=boot();
if(third.video.muted||third.elements.mute.attributes["aria-label"]!=="Mute")process.exit(4);
third.handlers["video:playing"]();
if(third.elements.toggle.attributes["aria-label"]!=="Pause")process.exit(5);
third.handlers["video:pause"]();
if(third.elements.toggle.attributes["aria-label"]!=="Play")process.exit(6);
'''
            mute_check = subprocess.run(["node", "-e", mute_harness, script_source], capture_output=True, timeout=5)
            self.assertEqual(mute_check.returncode, 0, mute_check.stderr.decode(errors="replace"))
            initial_script = re.search(r"<script>(.*?)</script>", player, re.S).group(1)
            other_script = re.search(r"<script>(.*?)</script>", other_page, re.S).group(1)
            progress_harness = r'''
const script=process.argv[1], otherScript=process.argv[2], storage=new Map(), handlers={};
let assigned="";
function boot(source=script){
  const video={muted:false,paused:false,currentTime:0,canPlayType:()=>true,addEventListener:(n,f)=>handlers[`video:${n}`]=f};
  const control=id=>({attributes:{},setAttribute(k,v){this.attributes[k]=v},addEventListener:(n,f)=>handlers[`${id}:${n}`]=f});
  const elements={player:video,"player-status":{textContent:""},"loading-overlay":{style:{},hidden:false,textContent:""},
    toggle:control("toggle"),mute:control("mute"),
    fullscreen:control("fullscreen"),
    seek:{value:"0",addEventListener:(n,f)=>handlers[`seek:${n}`]=f},time:{textContent:""}};
  global.document={getElementById:id=>elements[id]??null};
  global.window={Hls:null,addEventListener:(n,f)=>handlers[`window:${n}`]=f};
  global.sessionStorage={getItem:()=>null,setItem:()=>{},removeItem:()=>{}};
  global.localStorage={getItem:k=>storage.get(k)??null,setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)};
  global.setInterval=()=>1;
  global.location={assign:url=>assigned=url};
  eval(source);
  return {video,elements};
}
const first=boot();
first.video.currentTime=93;handlers["video:timeupdate"]();
const key=[...storage.keys()].find(k=>k.startsWith("tablo_progress:"));
if(!key||Number(storage.get(key))!==93)process.exit(1);
boot(otherScript);
const third=boot();third.elements.seek.value="600";handlers["seek:change"]();handlers["window:pagehide"]();
if(!assigned.includes("&start=600")||Number(storage.get(key))!==600)process.exit(4);
const fourth=boot();fourth.video.currentTime=5390;handlers["video:ended"]();
if(storage.has(key))process.exit(5);
'''
            progress_check = subprocess.run(["node", "-e", progress_harness, initial_script, other_script], capture_output=True, timeout=5)
            self.assertEqual(progress_check.returncode, 0, progress_check.stderr.decode(errors="replace"))
            script = opener.open(base + "/static/hls.min.js")
            self.assertIn("javascript", script.headers["Content-Type"])
            self.assertIn(b"Hls", script.read())
            with patch.object(state, "hls_data", return_value=b"#EXTM3U\n"):
                hls = opener.open(base + "/hls/test-token/index.m3u8")
                self.assertEqual(hls.headers["Content-Type"], "application/vnd.apple.mpegurl")
                self.assertEqual(hls.read(), b"#EXTM3U\n")
            state.stream_gate.acquire()
            try:
                with self.assertRaises(HTTPError) as busy:
                    opener.open(base + "/media?path=" + path)
                self.assertEqual(busy.exception.code, 429)
            finally:
                state.stream_gate.release()
            with patch("webapp.subprocess.Popen", return_value=FakeProcess()) as start:
                response = opener.open(base + "/media?path=" + path)
                self.assertEqual(response.headers["Content-Type"], "video/mp4")
                self.assertEqual(response.read(), b"fake-fragmented-mp4")
                self.assertIn("http://192.0.2.10:8885/stream/abc.m3u8", start.call_args.args[0])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()

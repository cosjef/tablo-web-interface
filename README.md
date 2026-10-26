# Tablo web interface

A local browser interface for a 4th-generation Tablo. It shows device status, channels, recordings, and scheduled guide entries, and plays recordings. The app does not provide controls to delete recordings or change schedules.

This is an unofficial project tested with one 4th-generation device. Other models and firmware versions have not been verified.

## Requirements

- Python 3 (tested with 3.14)
- FFmpeg and `ffprobe`, with the `libx264` and AAC encoders available
- A 4th-generation Tablo reachable on the same network and a matching Tablo account
- Request-signing values you are authorized to use. They are not included in the current source tree; their provenance and redistribution rights have not been established.

The browser HLS.js bundle is included under `vendor/`. Node.js is needed only to run the JavaScript tests.

## Configure and run

Copy `config.example.json` to `config.local.json` and fill in all four fields. The local file is ignored by Git. The `device_url` must be `http://YOUR_TABLO_ADDRESS:8887`; `server_id` is the `server_id` returned by your Tablo's `/server/info` endpoint on port 8887. Put your authorized request-signing values in `signing_key` and `device_key`. Do not put your Tablo account password in this file.

```sh
cp config.example.json config.local.json
chmod 600 config.local.json
python3 webapp.py
```

Alternatively, set `TABLO_CONFIG` to the path of a JSON file with the same fields. Open <http://127.0.0.1:8787/> and sign in with the account used by the Tablo app.

The server binds to `127.0.0.1` by default. It sends the account password to Tablo's cloud login endpoint over HTTPS and keeps the resulting credentials in process memory. Restarting the server signs you out. The app has one active session and one active browser HLS stream at a time. **Do not expose this server to the internet or an untrusted network.** Python's `http.server` is intended here only for local use.

## Playback

The scheduled guide can take several minutes to load. Playback transcodes recordings with FFmpeg and serves a local HLS stream. Seeking starts a new stream at the selected approximate time. For recordings with a reported duration, the player saves progress in the current browser profile. Opening a recording from Recordings resumes at a saved position at least 15 seconds from either end. An explicit start position overrides it; the saved position is cleared when playback ends near the reported duration.

## Test

```sh
python3 -m unittest discover -q
```

Tests use synthetic video in `test_media/` and do not need a Tablo or account. The full suite also needs FFmpeg, `ffprobe`, and Node.js.

## License and third-party software

The project code is licensed under [MIT](LICENSE). The included HLS.js bundle retains its [upstream notice](vendor/node_modules/hls.js/LICENSE) and the [full Apache 2.0 license text](vendor/node_modules/hls.js/LICENSE-2.0.txt). FFmpeg is a separate executable dependency and is not bundled here. Check the applicable FFmpeg build license before redistributing a binary.

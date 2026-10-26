"""Small read-only client for the unofficial Tablo 4th-generation protocol."""
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import format_datetime
import hashlib
import hmac
import json
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, ProxyHandler


class TabloError(Exception):
    pass


@dataclass(frozen=True)
class TabloConfig:
    server_id: str
    device_url: str
    signing_key: str
    device_key: str

    def __post_init__(self):
        for name in ("server_id", "signing_key", "device_key"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value or value != value.strip() or any(ord(c) < 32 for c in value):
                raise TabloError(f"Invalid {name} in config.local.json")
        try:
            parsed = urlsplit(self.device_url)
            valid = (parsed.scheme == "http" and parsed.hostname and parsed.port == 8887
                     and not parsed.username and not parsed.password and not parsed.path
                     and not parsed.query and not parsed.fragment)
        except (TypeError, ValueError):
            valid = False
        if not valid:
            raise TabloError("Invalid device_url in config.local.json: use http://HOST:8887")


def load_config(path=None):
    config_path = Path(path or os.environ.get("TABLO_CONFIG") or Path(__file__).with_name("config.local.json"))
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise TabloError("Create config.local.json from config.example.json before starting the app") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise TabloError("Could not read config.local.json as JSON") from exc
    if not isinstance(data, dict):
        raise TabloError("config.local.json must contain a JSON object")
    try:
        return TabloConfig(**{name: data[name] for name in ("server_id", "device_url", "signing_key", "device_key")})
    except KeyError as exc:
        raise TabloError(f"Missing {exc.args[0]} in config.local.json") from exc


class TabloClient:
    CLOUD = "https://lighthousetv.ewscloud.com"
    CLOUD_UA = "Tablo-FAST/2.0.0 (Mobile; iPhone; iOS 16.6)"
    DEVICE_UA = "Tablo-FAST/1.7.0 (Mobile; iPhone; iOS 18.4)"

    def __init__(self, config=None):
        self.config = config or load_config()
        self.SERVER_ID = self.config.server_id
        self.DEVICE_URL = self.config.device_url
        self.HASH_KEY = self.config.signing_key
        self.DEVICE_KEY = self.config.device_key
        self.access = None
        self.lighthouse = None
        self.device_base = None
        self.opener = build_opener(ProxyHandler({}))

    def request_json(self, method, url, body=None, headers=None):
        data = json.dumps(body, separators=(",", ":")).encode() if body is not None else (b"" if method == "POST" else None)
        req = Request(url, data=data, method=method, headers=headers or {})
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with self.opener.open(req, timeout=35) as response:
                result = response.read(15_000_000)
                return json.loads(result)
        except HTTPError as exc:
            raise TabloError(f"HTTP {exc.code} from {url.split('?')[0]}") from exc
        except (URLError, TimeoutError) as exc:
            raise TabloError(f"Could not reach {url.split('?')[0]}: {exc.reason if isinstance(exc, URLError) else exc}") from exc
        except json.JSONDecodeError as exc:
            raise TabloError("The Tablo API returned invalid JSON") from exc

    def login(self, email, password):
        login = self.request_json(
            "POST", self.CLOUD + "/api/v2/login/",
            {"email": email, "password": password},
            {"User-Agent": self.CLOUD_UA, "Accept": "*/*"},
        )
        if not isinstance(login, dict) or login.get("code") or not login.get("access_token"):
            raise TabloError((login.get("message") if isinstance(login, dict) else None) or "Tablo login was not accepted")
        self.access = f"{login.get('token_type', 'Bearer')} {login['access_token']}"
        cloud_headers = {"User-Agent": self.CLOUD_UA, "Accept": "*/*", "Authorization": self.access}
        account = self.request_json("GET", self.CLOUD + "/api/v2/account/", headers=cloud_headers)
        if not isinstance(account, dict):
            raise TabloError("Tablo account response was invalid")
        profiles = account.get("profiles") or []
        devices = account.get("devices") or []
        device = next((d for d in devices if d.get("serverId") == self.SERVER_ID), None)
        if not profiles or device is None:
            raise TabloError("This account does not list the expected Tablo device and a profile")
        selected = self.request_json(
            "POST", self.CLOUD + "/api/v2/account/select/",
            {"pid": profiles[0]["identifier"], "sid": self.SERVER_ID}, cloud_headers,
        )
        if not isinstance(selected, dict) or not selected.get("token"):
            raise TabloError("Tablo did not return a device token")
        self.lighthouse = selected["token"]
        self.device_base = self.DEVICE_URL
        return account

    def authorization(self, method, path, body_text, date):
        body_hash = hashlib.md5(body_text.encode()).hexdigest() if body_text else ""
        string_to_sign = f"{method}\n{path}\n{body_hash}\n{date}".encode()
        signature = hmac.new(self.HASH_KEY.encode(), string_to_sign, hashlib.md5).hexdigest()
        return f"tablo:{self.DEVICE_KEY}:{signature}"

    def device_json(self, method, path, body=None):
        if not self.device_base or not self.lighthouse:
            raise TabloError("Sign in first")
        date = format_datetime(datetime.now(timezone.utc), usegmt=True)
        body_text = json.dumps(body, separators=(",", ":")) if body is not None else None
        headers = {
            "Date": date,
            "User-Agent": self.DEVICE_UA,
            "Accept": "*/*",
            "Lighthouse": self.lighthouse,
            "Authorization": self.authorization(method, path, body_text, date),
        }
        # request_json uses the same compact JSON encoding as the signed body.
        return self.request_json(method, self.device_base + path, body, headers)

    def batch(self, paths):
        result = {}
        for start in range(0, len(paths), 50):
            chunk = paths[start:start + 50]
            response = self.device_json("POST", "/batch", chunk)
            if isinstance(response, dict):
                result.update(response)
        return result

    def objects(self, path):
        paths = self.device_json("GET", path)
        if not isinstance(paths, list):
            raise TabloError(f"Unexpected listing from {path}")
        namespace = {
            "/recordings/airings": "/recordings/",
            "/guide/airings": "/",
        }.get(path, path + "/")
        if any(
            not isinstance(p, str)
            or not p.startswith(namespace)
            or p.startswith("//")
            or ".." in p.split("/")
            or "\\" in p
            or "?" in p
            or "#" in p
            for p in paths
        ):
            raise TabloError(f"Unexpected paths from {path}")
        objects = self.batch(paths)
        missing = [p for p in paths if not isinstance(objects.get(p), dict)]
        if missing:
            raise TabloError(f"The {path} listing is incomplete ({len(missing)} of {len(paths)} objects missing)")
        if path == "/guide/airings" and any(not isinstance(objects[p].get("airing_details"), dict) for p in paths):
            raise TabloError("The guide returned an invalid airing object")
        return [dict(objects[p], path=p) for p in paths]

    def recordings(self):
        return self.objects("/recordings/airings")

    def channels(self):
        return self.objects("/guide/channels")

    def guide_airings(self):
        return self.objects("/guide/airings")

    def server_info(self):
        info = self.device_json("GET", "/server/info")
        if not isinstance(info, dict) or info.get("server_id") != self.SERVER_ID:
            raise TabloError("The device did not return the expected server ID")
        return info

    def watch_recording(self, path):
        if (
            not isinstance(path, str)
            or not path.startswith("/recordings/")
            or path.startswith("//")
            or ".." in path.split("/")
            or any(c in path for c in ("?", "#", "\\"))
        ):
            raise TabloError("Invalid recording path")
        response = self.device_json("POST", path.rstrip("/") + "/watch")
        url = response.get("playlist_url") if isinstance(response, dict) else None
        try:
            parsed = urlsplit(url) if isinstance(url, str) else None
            device_host = urlsplit(self.DEVICE_URL).hostname
            valid = (parsed and parsed.scheme == "http" and parsed.hostname == device_host
                     and parsed.port == 8885 and not parsed.username and not parsed.password)
        except ValueError:
            valid = False
        if not valid:
            raise TabloError("Tablo returned an invalid recording playlist URL")
        return url


def scheduled_airings(airings, now=None):
    now_dt = datetime.fromisoformat((now or datetime.now(timezone.utc).isoformat()).replace("Z", "+00:00"))
    result = []
    for airing in airings:
        schedule = airing.get("schedule")
        state = schedule.get("state") if isinstance(schedule, dict) else None
        if not state or state == "none":
            continue
        when = (airing.get("airing_details") or {}).get("datetime")
        if when:
            try:
                if datetime.fromisoformat(str(when).replace("Z", "+00:00")) < now_dt:
                    continue
            except (ValueError, TypeError):
                pass
        result.append(airing)
    return result

import http.cookiejar
import threading
import unittest
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener

from tablo import TabloClient, TabloConfig
from webapp import AppState, make_server, render_cards, render_status


class FakeClient:
    DEVICE_URL = "http://tablo.lan:8887"
    def __init__(self):
        self.password_seen = None
    def login(self, email, password):
        self.password_seen = password
    def server_info(self):
        return {"server_id": "SID_TEST", "name": "Tablo"}
    def recordings(self):
        return [{"path": "/recordings/airings/1", "airing_details": {"show_title": "News <script>", "datetime": "2026-10-25T20:00:00Z"}}]
    def channels(self):
        return [{"channel": {"major": 4, "minor": 1, "call_sign": "WXYZ"}}]
    def guide_airings(self):
        return []


class WebTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeClient()
        self.state = AppState(client=self.fake)
        self.server = make_server("127.0.0.1", 0, self.state)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.opener = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_login_cookie_gates_recordings_and_escapes_titles(self):
        with self.assertRaises(HTTPError) as blocked:
            self.opener.open(self.base + "/recordings")
        self.assertEqual(blocked.exception.code, 403)
        payload = urlencode({"email": "person@example.com", "password": "test-password"}).encode()
        self.opener.open(Request(self.base + "/login", data=payload, method="POST"))
        page = self.opener.open(self.base + "/recordings").read().decode()
        self.assertIn("News &lt;script&gt;", page)
        self.assertNotIn("News <script>", page)
        self.assertNotIn("test-password", repr(self.state.__dict__))

    def test_render_cards_includes_raw_details_safely(self):
        html = render_cards("Recordings", [{"path": "/x", "airing_details": {"show_title": "A & B"}}])
        self.assertIn("A &amp; B", html)
        self.assertIn("&quot;show_title&quot;", html)

    def test_status_overview_uses_device_fields_and_escapes_values(self):
        info = {
            "server_id": "SID_TEST", "name": "Tablo <script>",
            "availability": "ready", "setup_completed": True,
            "version": "2.2.58", "build_number": 2612800,
            "local_address": "192.0.2.10",
            "model": {"name": "Tablo 4G DUAL 128GB", "tuners": 2, "wifi": True},
        }
        html = render_status(info)
        for value in ("Ready", "Setup complete", "2.2.58", "2612800", "192.0.2.10", "2 tuners", "Wi-Fi capable", "SID_TEST"):
            self.assertIn(value, html)
        self.assertIn("Tablo &lt;script&gt;", html)
        self.assertNotIn("Tablo <script>", html)
        self.assertIn("Technical details", html)
        self.assertIn("<details", html)
        self.assertIn("Unknown", render_status({"name": "Tablo"}))

    def test_status_route_renders_overview(self):
        payload = urlencode({"email": "person@example.com", "password": "test-password"}).encode()
        self.opener.open(Request(self.base + "/login", data=payload, method="POST"))
        html = self.opener.open(self.base + "/status").read().decode()
        self.assertIn("Device overview", html)
        self.assertIn("SID_TEST", html)

    def test_home_uses_configured_device_address(self):
        payload = urlencode({"email": "person@example.com", "password": "test-password"}).encode()
        self.opener.open(Request(self.base + "/login", data=payload, method="POST"))
        html = self.opener.open(self.base + "/").read().decode()
        self.assertIn("tablo.lan", html)
        self.assertNotIn("192.0.2.10", html)


if __name__ == "__main__":
    unittest.main()

class ScheduleIsolationTests(unittest.TestCase):
    def test_old_guide_load_cannot_populate_new_session(self):
        waiting = threading.Event()
        release = threading.Event()
        class SlowClient(FakeClient):
            def guide_airings(self):
                waiting.set()
                release.wait(2)
                return [{"path": "/guide/airings/1", "airing_details": {"datetime": "2099-01-01T00:00:00Z"}, "schedule": {"state": "scheduled"}}]
        state = AppState(client=SlowClient())
        state.login("a@example.com", "password")
        state.schedule_snapshot()
        self.assertTrue(waiting.wait(1))
        state.logout()
        state.client = FakeClient()
        state.login("b@example.com", "password")
        release.set()
        threading.Event().wait(0.1)
        self.assertIsNone(state.schedule)


class ConfigurationLifecycleTests(unittest.TestCase):
    def test_logout_keeps_device_config_but_discards_authentication(self):
        config = TabloConfig("SID_TEST", "http://tablo.lan:8887", "test-signing-key", "test-device-key")
        state = AppState(client=TabloClient(config))
        state.client.lighthouse = "private-token"
        state.client.access = "Bearer private-token"
        state.logout()
        self.assertEqual(state.client.config, config)
        self.assertIsNone(state.client.lighthouse)
        self.assertIsNone(state.client.access)

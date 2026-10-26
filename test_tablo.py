import hashlib
import hmac
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tablo import TabloClient, TabloConfig, TabloError, load_config, scheduled_airings

TEST_CONFIG = TabloConfig("SID_TEST", "http://192.0.2.10:8887", "test-signing-key", "test-device-key")

def new_client():
    return TabloClient(TEST_CONFIG)


class ConfigurationTests(unittest.TestCase):
    def test_loads_device_settings_from_local_json(self):
        with tempfile.TemporaryDirectory() as folder:
            config_path = Path(folder) / "config.local.json"
            config_path.write_text(json.dumps({
                "device_url": "http://tablo.lan:8887",
                "server_id": "SID_EXAMPLE",
                "signing_key": "test-signing-key",
                "device_key": "test-device-key",
            }))
            config = load_config(config_path)
        self.assertEqual(config.device_url, "http://tablo.lan:8887")
        self.assertEqual(config.server_id, "SID_EXAMPLE")
        self.assertEqual(TabloClient(config).DEVICE_URL, "http://tablo.lan:8887")

    def test_missing_or_invalid_config_has_actionable_error(self):
        with tempfile.TemporaryDirectory() as folder:
            missing = Path(folder) / "missing.json"
            with self.assertRaisesRegex(TabloError, "config.local.json"):
                load_config(missing)
            config_path = Path(folder) / "config.local.json"
            config_path.write_text(json.dumps({
                "device_url": "https://example.test/path",
                "server_id": "SID_EXAMPLE",
                "signing_key": "test-signing-key",
                "device_key": "test-device-key",
            }))
            with self.assertRaisesRegex(TabloError, "device_url"):
                load_config(config_path)

    def test_custom_device_address_is_checked_for_playlists(self):
        config = TabloConfig("SID_EXAMPLE", "http://tablo.lan:8887", "test-signing-key", "test-device-key")
        client = TabloClient(config)
        with patch.object(client, "device_json", return_value={"playlist_url": "http://tablo.lan:8885/stream/abc.m3u8"}):
            self.assertEqual(client.watch_recording("/recordings/airings/1"), "http://tablo.lan:8885/stream/abc.m3u8")
        with patch.object(client, "device_json", return_value={"playlist_url": "http://other.lan:8885/stream/abc.m3u8"}):
            with self.assertRaisesRegex(TabloError, "invalid recording playlist"):
                client.watch_recording("/recordings/airings/1")
        with patch.object(client, "device_json", return_value={"playlist_url": "http://tablo.lan:bad/stream/abc.m3u8"}):
            with self.assertRaisesRegex(TabloError, "invalid recording playlist"):
                client.watch_recording("/recordings/airings/1")


class ProtocolTests(unittest.TestCase):
    def test_signature_uses_empty_third_line_for_bodyless_get(self):
        date = "Wed, 16 Jul 2025 14:03:22 GMT"
        client = new_client()
        actual = client.authorization("GET", "/recordings/airings", None, date)
        message = f"GET\n/recordings/airings\n\n{date}".encode()
        expected_mac = hmac.new(client.HASH_KEY.encode(), message, hashlib.md5).hexdigest()
        self.assertEqual(actual, f"tablo:{client.DEVICE_KEY}:{expected_mac}")

    def test_signature_hashes_a_real_post_body(self):
        client = new_client()
        date = "Wed, 16 Jul 2025 14:03:22 GMT"
        body = '["/recordings/airings/1"]'
        actual = client.authorization("POST", "/batch", body, date)
        digest = hashlib.md5(body.encode()).hexdigest()
        message = f"POST\n/batch\n{digest}\n{date}".encode()
        expected_mac = hmac.new(client.HASH_KEY.encode(), message, hashlib.md5).hexdigest()
        self.assertEqual(actual, f"tablo:{client.DEVICE_KEY}:{expected_mac}")

    def test_login_selects_matching_device_and_profile(self):
        client = new_client()
        responses = iter([
            {"token_type": "Bearer", "access_token": "access"},
            {"profiles": [{"identifier": "profile-1"}], "devices": [
                {"serverId": "OTHER", "url": "http://192.168.68.99:8887"},
                {"serverId": "SID_TEST", "url": "http://192.0.2.10:8887"}]},
            {"token": "lighthouse"},
        ])
        calls = []
        def fake_request(method, url, body=None, headers=None):
            calls.append((method, url, body, headers))
            return next(responses)
        with patch.object(client, "request_json", side_effect=fake_request):
            client.login("person@example.com", "password")
        self.assertEqual(client.lighthouse, "lighthouse")
        self.assertEqual(client.device_base, "http://192.0.2.10:8887")
        self.assertEqual(calls[2][2], {"pid": "profile-1", "sid": "SID_TEST"})
        self.assertEqual(calls[1][3]["Authorization"], "Bearer access")

    def test_batch_chunks_at_50(self):
        client = new_client()
        client.device_base = "http://192.0.2.10:8887"
        client.lighthouse = "token"
        paths = [f"/recordings/airings/{i}" for i in range(51)]
        chunks = []
        def fake_device(method, path, body=None):
            chunks.append(body)
            return {p: {"path": p} for p in body}
        with patch.object(client, "device_json", side_effect=fake_device):
            result = client.batch(paths)
        self.assertEqual([len(c) for c in chunks], [50, 1])
        self.assertEqual(len(result), 51)

    def test_scheduled_airings_filters_none_and_past(self):
        now = "2026-10-25T12:00:00Z"
        airings = [
            {"path": "/guide/airings/1", "airing_details": {"datetime": "2026-10-26T12:00:00Z"}, "schedule": {"state": "scheduled"}},
            {"path": "/guide/airings/2", "airing_details": {"datetime": "2026-10-26T12:00:00Z"}, "schedule": {"state": "none"}},
            {"path": "/guide/airings/3", "airing_details": {"datetime": "2026-10-24T12:00:00Z"}, "schedule": {"state": "scheduled"}},
        ]
        self.assertEqual([a["path"] for a in scheduled_airings(airings, now)], ["/guide/airings/1"])


if __name__ == "__main__":
    unittest.main()

class RealPathShapeTests(unittest.TestCase):
    def test_recording_list_allows_other_internal_recording_paths(self):
        client = new_client()
        with patch.object(client, "device_json", return_value=["/recordings/series/episodes/123"]), \
             patch.object(client, "batch", return_value={"/recordings/series/episodes/123": {"episode": {"title": "Sample"}}}):
            items = client.objects("/recordings/airings")
        self.assertEqual(items[0]["path"], "/recordings/series/episodes/123")

    def test_recording_list_rejects_external_urls(self):
        client = new_client()
        with patch.object(client, "device_json", return_value=["http://example.test/evil"]):
            with self.assertRaises(Exception):
                client.objects("/recordings/airings")

class ListingCompletenessTests(unittest.TestCase):
    def test_guide_channel_list_rejects_other_guide_objects(self):
        client = new_client()
        with patch.object(client, "device_json", return_value=["/guide/airings/123"]):
            with self.assertRaises(Exception):
                client.objects("/guide/channels")

    def test_missing_batch_object_is_an_error(self):
        client = new_client()
        with patch.object(client, "device_json", return_value=["/recordings/airings/123"]), \
             patch.object(client, "batch", return_value={}):
            with self.assertRaisesRegex(Exception, "incomplete"):
                client.objects("/recordings/airings")

class GuidePathShapeTests(unittest.TestCase):
    def test_guide_airings_allow_nested_internal_path_with_airing_details(self):
        client = new_client()
        path = "/guide/series/episodes/airings/456"
        obj = {"airing_details": {"datetime": "2026-10-25T20:00:00Z"}, "schedule": {"state": "none"}}
        with patch.object(client, "device_json", return_value=[path]), \
             patch.object(client, "batch", return_value={path: obj}):
            items = client.objects("/guide/airings")
        self.assertEqual(items[0]["path"], path)

    def test_guide_airings_reject_wrong_object_shape(self):
        client = new_client()
        path = "/guide/series/123"
        with patch.object(client, "device_json", return_value=[path]), \
             patch.object(client, "batch", return_value={path: {"title": "Series"}}):
            with self.assertRaisesRegex(Exception, "invalid airing"):
                client.objects("/guide/airings")

class GuidePathFallbackTests(unittest.TestCase):
    def test_guide_airings_allow_absolute_internal_path_without_guide_prefix(self):
        client = new_client()
        path = "/airings/456"
        obj = {"airing_details": {"datetime": "2026-10-25T20:00:00Z"}}
        with patch.object(client, "device_json", return_value=[path]), \
             patch.object(client, "batch", return_value={path: obj}):
            self.assertEqual(client.objects("/guide/airings")[0]["path"], path)

    def test_guide_airings_reject_scheme_relative_url(self):
        client = new_client()
        with patch.object(client, "device_json", return_value=["//example.test/airings/1"]):
            with self.assertRaises(Exception):
                client.objects("/guide/airings")

class PlaybackProtocolTests(unittest.TestCase):
    def test_watch_recording_uses_signed_empty_post_and_local_playlist(self):
        client = new_client()
        with patch.object(client, "device_json", return_value={"playlist_url": "http://192.0.2.10:8885/stream/abc.m3u8"}) as request:
            url = client.watch_recording("/recordings/series/episodes/3530286")
        self.assertEqual(url, "http://192.0.2.10:8885/stream/abc.m3u8")
        request.assert_called_once_with("POST", "/recordings/series/episodes/3530286/watch")

    def test_watch_recording_rejects_external_playlist(self):
        client = new_client()
        with patch.object(client, "device_json", return_value={"playlist_url": "https://example.test/stream.m3u8"}):
            with self.assertRaises(Exception):
                client.watch_recording("/recordings/airings/1")

    def test_watch_recording_rejects_non_recording_path(self):
        client = new_client()
        with self.assertRaises(Exception):
            client.watch_recording("/guide/channels/1")

class EmptyPostWireTests(unittest.TestCase):
    def test_post_without_body_sends_zero_length_json_body(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, limit): return b'{"playlist_url":"http://192.0.2.10:8885/x.m3u8"}'
        class Opener:
            request = None
            def open(self, request, timeout):
                self.request = request
                return Response()
        client = new_client()
        opener = Opener()
        client.opener = opener
        client.request_json("POST", "http://192.0.2.10:8887/recordings/airings/1/watch", headers={"User-Agent": "test"})
        self.assertEqual(opener.request.data, b"")
        self.assertEqual(opener.request.get_header("Content-type"), "application/json")

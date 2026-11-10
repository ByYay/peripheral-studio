from http.client import HTTPConnection
import json
from pathlib import Path
import tempfile
import threading
import unittest

from backend.server import create_server


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.static = Path(self.directory.name) / "static"
        self.static.mkdir()
        (self.static / "index.html").write_text("<html>UPM</html>", encoding="utf-8")
        (self.static / "main.js").write_text("const x = 1;", encoding="utf-8")
        self.server = create_server(data_dir=Path(self.directory.name) / "data", token="test-session", static_dir=self.static)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, method, path, body=None, headers=None, authenticated=True):
        connection = HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=3)
        self.addCleanup(connection.close)
        actual = {"X-UPM-Token": "test-session"} if authenticated else {}
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
            actual["Content-Type"] = "application/json"
        actual.update(headers or {})
        connection.request(method, path, body, actual)
        response = connection.getresponse()
        raw = response.read()
        payload = json.loads(raw) if response.getheader("Content-Type", "").startswith("application/json") else raw
        return response.status, payload, dict(response.getheaders())

    def test_http_round_trip(self):
        status, health, _ = self.request("GET", "/api/v1/health")
        self.assertEqual(status, 200)
        self.assertEqual(health, {"status": "ok", "mode": "simulation", "apiVersion": 1, "devicesRevision": 0})
        status, result, _ = self.request("PATCH", "/api/v1/devices/aerox-3/settings", {"dpi": 1200})
        self.assertEqual(status, 200)
        self.assertEqual(result["device"]["settings"]["dpi"], 1200)
        status, result, _ = self.request("POST", "/api/v1/profiles", {"name": "Focus"})
        self.assertEqual(status, 201)
        profile_id = result["profile"]["id"]
        self.assertEqual(result["profileState"], {"mainProfileId": profile_id, "activeProfileId": profile_id, "activeBy": "main"})
        self.request("POST", "/api/v1/profiles", {"name": "Work"})
        self.request("PATCH", "/api/v1/devices/aerox-3/settings", {"dpi": 2400})  # saved into Work
        status, result, _ = self.request("POST", f"/api/v1/profiles/{profile_id}/activate", {})
        self.assertEqual(status, 200)
        self.assertEqual(result["devices"][2]["settings"]["dpi"], 1200)

    def test_language_preference_round_trip(self):
        status, result, _ = self.request("PUT", "/api/v1/preferences", {"language": "tr"})
        self.assertEqual((status, result), (200, {"language": "tr"}))
        self.assertEqual(self.request("GET", "/api/v1/preferences")[1], {"language": "tr"})
        status, result, _ = self.request("PUT", "/api/v1/preferences", {"language": "fr"})
        self.assertEqual((status, result["error"]["code"]), (400, "invalid_preferences"))
        self.assertEqual(self.request("PUT", "/api/v1/preferences", {"language": "en"}, authenticated=False)[0], 401)

    def test_auth_host_origin_and_error_shape(self):
        for headers, authenticated, expected in [({}, False, 401), ({"X-UPM-Token": "wrong"}, True, 401),
                ({"Host": "attacker.example"}, True, 403), ({"Origin": "https://attacker.example"}, True, 403),
                ({"Origin": "http://localhost:5173"}, True, 403)]:
            with self.subTest(headers=headers):
                status, payload, _ = self.request("GET", "/api/v1/devices", headers=headers, authenticated=authenticated)
                self.assertEqual(status, expected)
                self.assertEqual(set(payload["error"]), {"code", "message"})

    def test_own_origin_and_preflight(self):
        origin = f"http://127.0.0.1:{self.server.server_address[1]}"
        status, _, headers = self.request("OPTIONS", "/api/v1/devices", headers={"Origin": origin}, authenticated=False)
        self.assertEqual(status, 204)
        self.assertEqual(headers["Access-Control-Allow-Origin"], origin)

    def test_invalid_bodies(self):
        for body, headers, expected in [("{}", {}, 415), ("{", {"Content-Type": "application/json"}, 400),
                ('{"dpi":NaN}', {"Content-Type": "application/json"}, 400),
                ("x" * 17000, {"Content-Type": "application/json"}, 413),
                ({"dpi": 1600, "pollingRate": 999}, {}, 400)]:
            with self.subTest(expected=expected):
                status, _, _ = self.request("PATCH", "/api/v1/devices/aerox-3/settings", body, headers)
                self.assertEqual(status, expected)
        _, result, _ = self.request("GET", "/api/v1/devices")
        self.assertEqual(result["devices"][2]["settings"]["dpi"], 800)

    def test_escaped_unpaired_unicode_returns_validation_error(self):
        status, result, _ = self.request("POST", "/api/v1/profiles", {"name": "bad\ud800"})
        self.assertEqual(status, 400)
        self.assertEqual(result["error"]["code"], "invalid_name")
        status, result, _ = self.request("PATCH", "/api/v1/devices/aerox-3/settings", {"\ud800": 1})
        self.assertEqual(status, 400)
        self.assertEqual(result["error"]["code"], "invalid_settings")
        status, result, _ = self.request("POST", "/api/v1/profiles", {"name": "Çalışma 🎮"})
        self.assertEqual(status, 201)
        self.assertEqual(result["profile"]["name"], "Çalışma 🎮")

    def test_static_files_and_traversal(self):
        status, body, headers = self.request("GET", "/", authenticated=False)
        self.assertEqual((status, body), (200, b"<html>UPM</html>"))
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        status, _, headers = self.request("GET", "/main.js", authenticated=False)
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/javascript")
        for path in ("/%2e%2e/secret", "/..%5csecret", "/C:/secret", "/%00"):
            with self.subTest(path=path):
                status, _, _ = self.request("GET", path, authenticated=False)
                self.assertEqual(status, 400)
        self.assertEqual(self.request("GET", "/missing.js")[0], 404)
        self.assertEqual(self.request("GET", "/settings", headers={"Accept": "text/html"})[0], 200)
        self.assertEqual(self.request("GET", "/api/v1/missing", headers={"Accept": "text/html"})[0], 404)

    def test_network_bind_rejected(self):
        with self.assertRaises(ValueError):
            create_server(host="0.0.0.0", data_dir=self.directory.name)

    def test_default_token_is_random_and_development_cors_is_explicit(self):
        with create_server(data_dir=self.directory.name) as first:
            with create_server(data_dir=self.directory.name) as second:
                self.assertGreaterEqual(len(first.api_token), 32)
                self.assertNotEqual(first.api_token, second.api_token)
            thread = threading.Thread(target=first.serve_forever, daemon=True)
            thread.start()
            try:
                connection = HTTPConnection("127.0.0.1", first.server_address[1], timeout=3)
                try:
                    connection.request("GET", "/api/v1/health", headers={"Origin": "http://localhost:5173", "X-UPM-Token": first.api_token})
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.getheader("Access-Control-Allow-Origin"), "http://localhost:5173")
                    response.read()
                finally:
                    connection.close()
            finally:
                first.shutdown()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()

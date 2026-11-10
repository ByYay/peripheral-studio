"""Acceptance checks for the Unified Peripheral Manager simulation API."""

from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory

from backend.server import create_server


TOKEN = "qa-test-token"
SCHEMA_PATH = Path(__file__).resolve().parents[1] / "contracts" / "api.schema.json"


class ServerContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._temporary_directory = TemporaryDirectory()
        cls.data_dir = Path(cls._temporary_directory.name)
        cls.schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        cls.device_ids = set(
            cls.schema["$defs"]["Device"]["properties"]["id"]["enum"]
        )
        cls.server = create_server(
            host="127.0.0.1", port=0, data_dir=cls.data_dir, token=TOKEN
        )
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}/api/v1"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)
        cls._temporary_directory.cleanup()

    def request(
        self,
        method: str,
        path: str,
        body: object = None,
        *,
        token: str | None = TOKEN,
        base_url: str | None = None,
    ):
        headers = {}
        if token is not None:
            headers["X-UPM-Token"] = token
        payload = None
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            (base_url or self.base_url) + path,
            data=payload,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            raw_body = error.read()
            try:
                decoded = json.loads(raw_body) if raw_body else None
            except json.JSONDecodeError:
                decoded = raw_body.decode("utf-8", errors="replace")
            return error.code, decoded

    def test_health_and_devices_are_explicitly_simulated(self) -> None:
        status, health = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(health, {"status": "ok", "mode": "simulation", "apiVersion": 1, "devicesRevision": 0})

        status, payload = self.request("GET", "/devices")
        self.assertEqual(status, 200)
        devices = payload["devices"]
        device_schema = self.schema["$defs"]["Device"]
        device_properties = device_schema["properties"]
        device_ids = set(device_properties["id"]["enum"])
        self.assertEqual({device["id"] for device in devices}, device_ids)
        for device in devices:
            with self.subTest(device=device["id"]):
                self.assertTrue(set(device_schema["required"]) <= set(device) <= set(device_properties))
                self.assertIn(device["kind"], device_properties["kind"]["enum"])
                self.assertIn(device["connection"], device_properties["connection"]["enum"])
                self.assertEqual(device["connection"], "simulated")
                self.assertIsInstance(device["batteryPercent"], (int, float))
                self.assertGreaterEqual(device["batteryPercent"], 0)
                self.assertLessEqual(device["batteryPercent"], 100)
                capabilities_schema = device_properties["capabilities"]
                self.assertEqual(set(device["capabilities"]), set(capabilities_schema["required"]))
                self.assertTrue(all(type(value) is bool for value in device["capabilities"].values()))
                settings_schema = self.schema["$defs"]["Settings"]["properties"]
                self.assertTrue(set(device["settings"]).issubset(settings_schema))
                limits_schema = device_properties["limits"]["properties"]
                self.assertTrue(set(device["limits"]).issubset(limits_schema))

    def test_token_is_required_and_custom_token_is_enforced(self) -> None:
        status, _ = self.request("GET", "/devices", token=None)
        self.assertIn(status, (401, 403))
        status, _ = self.request("GET", "/devices", token="wrong-token")
        self.assertIn(status, (401, 403))
        status, _ = self.request(
            "POST", "/profiles", {"name": "unauthorized"}, token="wrong-token"
        )
        self.assertIn(status, (401, 403))

    def test_device_settings_patch_is_partial_and_device_scoped(self) -> None:
        status, result = self.request(
            "PATCH", "/devices/aerox-3/settings", {"dpi": 1200}
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["device"]["id"], "aerox-3")
        self.assertEqual(result["device"]["settings"]["dpi"], 1200)
        self.assertEqual(result["device"]["connection"], "simulated")

        status, _ = self.request("PATCH", "/devices/not-a-device/settings", {"dpi": 1200})
        self.assertGreaterEqual(status, 400)

    def test_unsupported_or_out_of_range_settings_are_rejected(self) -> None:
        status, _ = self.request(
            "PATCH", "/devices/mx-keys-mini/settings", {"lightingColor": "#ffffff"}
        )
        self.assertGreaterEqual(status, 400)
        status, _ = self.request("PATCH", "/devices/aerox-3/settings", {"dpi": -1})
        self.assertGreaterEqual(status, 400)

    def test_profiles_create_activate_and_survive_server_restart(self) -> None:
        status, result = self.request("POST", "/profiles", {"name": "QA profile"})
        self.assertEqual(status, 201)
        profile_id = result["profile"]["id"]
        profile_schema = self.schema["$defs"]["Profile"]
        self.assertEqual(set(result["profile"]), set(profile_schema["required"]))
        self.assertEqual(result["profile"]["name"], "QA profile")
        self.assertIsInstance(result["profile"]["settings"], dict)

        status, activated = self.request(
            "POST", f"/profiles/{profile_id}/activate", {}
        )
        self.assertEqual(status, 200)
        self.assertEqual(activated["profile"]["id"], profile_id)
        self.assertEqual({device["id"] for device in activated["devices"]}, self.device_ids)

        self.server.shutdown()
        self.thread.join(timeout=3)
        self.server.server_close()
        self.__class__.server = create_server(
            host="127.0.0.1", port=0, data_dir=self.data_dir, token=TOKEN
        )
        self.__class__.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.thread.start()
        self.__class__.base_url = f"http://127.0.0.1:{self.server.server_port}/api/v1"
        status, profiles = self.request("GET", "/profiles")
        self.assertEqual(status, 200)
        self.assertIn(profile_id, {profile["id"] for profile in profiles["profiles"]})

    def test_concurrent_profile_creation_returns_distinct_profiles(self) -> None:
        results: list[tuple[int, object]] = []
        lock = threading.Lock()

        def create(index: int) -> None:
            result = self.request("POST", "/profiles", {"name": f"Concurrent {index}"})
            with lock:
                results.append(result)

        workers = [threading.Thread(target=create, args=(index,)) for index in range(8)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive())

        self.assertEqual([status for status, _ in results], [201] * 8)
        profile_ids = [payload["profile"]["id"] for _, payload in results]
        self.assertEqual(len(set(profile_ids)), len(profile_ids))
        status, profiles = self.request("GET", "/profiles")
        self.assertEqual(status, 200)
        names = {profile["name"] for profile in profiles["profiles"]}
        self.assertTrue({f"Concurrent {index}" for index in range(8)}.issubset(names))

    def test_separate_server_instances_do_not_overwrite_profile_updates(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            data_dir = Path(temporary_directory)
            first = create_server(host="127.0.0.1", port=0, data_dir=data_dir, token=TOKEN)
            second = create_server(host="127.0.0.1", port=0, data_dir=data_dir, token=TOKEN)
            threads = [
                threading.Thread(target=server.serve_forever, daemon=True)
                for server in (first, second)
            ]
            for thread in threads:
                thread.start()
            urls = [f"http://127.0.0.1:{server.server_port}/api/v1" for server in (first, second)]
            try:
                for url, name in zip(urls, ("Instance one", "Instance two")):
                    status, _ = self.request(
                        "POST", "/profiles", {"name": name}, base_url=url
                    )
                    self.assertEqual(status, 201)

                stored = json.loads((data_dir / "profiles.json").read_text(encoding="utf-8"))
                self.assertEqual(
                    {profile["name"] for profile in stored},
                    {"Instance one", "Instance two"},
                )
            finally:
                for server in (first, second):
                    server.shutdown()
                    server.server_close()
                for thread in threads:
                    thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()

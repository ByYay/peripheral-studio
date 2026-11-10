from datetime import datetime
import json
from pathlib import Path
import tempfile
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import Mock, patch

from backend.domain import ApiError, Device
from backend.providers import HardwareProvider
from backend.service import DeviceService


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.service = DeviceService(self.directory.name)

    def test_simulation_catalog_and_copy_isolation(self):
        devices = self.service.devices()
        self.assertEqual(len(devices), 3)
        self.assertTrue(all(d["connection"] == "simulated" for d in devices))
        devices[2]["settings"]["dpi"] = 900
        self.assertEqual(self.service.devices()[2]["settings"]["dpi"], 800)

    def test_language_defaults_to_system_and_saved_choice_survives_restart(self):
        with patch("backend.service.system_language", return_value="tr"):
            self.assertEqual(self.service.preferences(), {"language": "tr"})
        changed = []
        self.service.add_preference_listener(changed.append)
        self.assertEqual(self.service.update_preferences({"language": "en"}), {"language": "en"})
        self.assertEqual(changed, ["en"])
        with patch("backend.service.system_language", return_value="tr"):
            self.assertEqual(DeviceService(self.directory.name).language(), "en")

    def test_invalid_or_damaged_language_preference(self):
        for body in ({"language": "de"}, {"language": ["tr"]}, {"language": "tr", "x": 1}, [], None):
            with self.subTest(body=body), self.assertRaises(ApiError) as error:
                self.service.update_preferences(body)
            self.assertEqual(error.exception.code, "invalid_preferences")
        (Path(self.directory.name) / "preferences.json").write_text("{", encoding="utf-8")
        with patch("backend.service.system_language", return_value="en"):
            self.assertEqual(DeviceService(self.directory.name).language(), "en")

    def test_start_with_windows_is_offered_only_when_the_app_can_register(self):
        self.assertNotIn("startWithWindows", self.service.preferences())
        with self.assertRaises(ApiError) as unavailable:
            self.service.update_preferences({"startWithWindows": True})
        self.assertEqual(unavailable.exception.code, "autostart_unavailable")
        autostart = Mock(available=True)
        autostart.enabled.return_value = False
        service = DeviceService(self.directory.name, autostart=autostart)
        self.assertEqual(service.preferences(), {"language": service.language(), "startWithWindows": False})
        autostart.enabled.return_value = True
        self.assertTrue(service.update_preferences({"startWithWindows": True})["startWithWindows"])
        autostart.set.assert_called_once_with(True)
        for body in ({"startWithWindows": "yes"}, {"startWithWindows": True, "theme": "dark"}, {}):
            with self.subTest(body=body), self.assertRaises(ApiError):
                service.update_preferences(body)
        autostart.set.side_effect = OSError("access denied")
        with self.assertRaises(ApiError) as failed:
            service.update_preferences({"startWithWindows": False})
        self.assertEqual(failed.exception.code, "autostart_failed")
    def test_failing_language_listener_does_not_reject_saved_choice(self):
        self.service.add_preference_listener(lambda language: 1 / 0)
        with self.assertLogs("backend.service", "ERROR"):
            self.assertEqual(self.service.update_preferences({"language": "tr"}), {"language": "tr"})
        self.assertEqual(DeviceService(self.directory.name).language(), "tr")

    def test_simulated_keyboard_backlight_is_validated_against_limits(self):
        device = self.service.update("mx-keys-mini", {"backlightMode": "manual", "backlightLevel": 7})
        self.assertEqual(device["settings"], {"backlightMode": "manual", "backlightLevel": 7})
        for values in ({"backlightMode": "bright"}, {"backlightLevel": 8}, {"backlightLevel": -1},
                       {"backlightLevel": True}, {"lightingColor": "#ffffff"}):
            with self.subTest(values=values), self.assertRaises(ApiError):
                self.service.update("mx-keys-mini", values)
        with self.assertRaises(ApiError) as error:
            self.service.update("aerox-3", {"backlightMode": "auto"})
        self.assertEqual(error.exception.code, "unsupported_setting")

    def test_hardware_provider_distinguishes_readback_and_acknowledgement(self):
        manager = unittest.mock.Mock()
        provider = HardwareProvider(manager)
        manager.apply.return_value = {"settings": {"dpi": 800}, "verification": "acknowledged"}
        provider.apply({"aerox-3": {"dpi": 800}})
        manager.apply.return_value = {"settings": {"backlightMode": "auto", "backlightLevel": 2}, "ignored": ["backlightLevel"]}
        provider.apply({"mx-keys-mini": {"backlightMode": "auto", "backlightLevel": 7}})
        manager.apply.return_value = {"settings": {"lightingColor": "#00ffee"}}
        with self.assertRaises(ApiError) as error:
            provider.apply({"g733": {"lightingColor": "#ff0000"}})
        self.assertEqual(error.exception.code, "hardware_readback_unverified")

    def test_partial_update_preserves_other_values(self):
        device = self.service.update("aerox-3", {"dpi": 1600})
        self.assertEqual(device["settings"]["dpi"], 1600)
        self.assertEqual(device["settings"]["pollingRate"], 1000)

    def test_invalid_updates_are_atomic(self):
        invalid = [{"dpi": True}, {"dpi": 101}, {"dpi": 18001}, {"pollingRate": 999},
                   {"lightingColor": "red"}, {"lightingBrightness": -1}, {"lightingBrightness": 101},
                   {"dpi": 1600, "unknown": 1}, [], None, {"dpi": 800.0}]
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(ApiError):
                self.service.update("aerox-3", values)
            self.assertEqual(self.service.devices()[2]["settings"]["dpi"], 800)

    def test_capability_and_unknown_device_rejection(self):
        with self.assertRaises(ApiError) as error:
            self.service.update("mx-keys-mini", {"lightingColor": "#FFFFFF"})
        self.assertEqual(error.exception.code, "unsupported_setting")
        with self.assertRaises(ApiError) as error:
            self.service.update("missing", {})
        self.assertEqual(error.exception.status, 404)

    def test_profile_restart_and_activation(self):
        self.service.update("aerox-3", {"dpi": 1600, "lightingColor": "#123456"})
        profile = self.service.create_profile({"name": "  Focus  "})
        self.assertEqual(profile["name"], "Focus")
        restarted = DeviceService(self.directory.name)
        self.assertEqual(restarted.devices()[2]["settings"]["dpi"], 800)
        result = restarted.activate(profile["id"])
        self.assertEqual(result["devices"][2]["settings"]["dpi"], 1600)
        self.assertEqual(result["devices"][2]["settings"]["lightingColor"], "#123456")
        result["profile"]["name"] = "changed"
        self.assertEqual(restarted.profiles()[0]["name"], "Focus")

    def test_deleted_profile_is_gone_after_restart(self):
        keep = self.service.create_profile({"name": "Keep"})
        drop = self.service.create_profile({"name": "Drop"})  # a new main profile is the chosen one
        with self.assertRaises(ApiError) as chosen:
            self.service.delete_profile(drop["id"])
        self.assertEqual(chosen.exception.code, "main_profile_in_use")
        self.service.activate(keep["id"])
        self.assertEqual([p["id"] for p in self.service.delete_profile(drop["id"])], [keep["id"]])
        with self.assertRaises(ApiError) as missing:
            self.service.delete_profile(drop["id"])
        self.assertEqual(missing.exception.code, "profile_not_found")
        self.assertEqual([p["name"] for p in DeviceService(self.directory.name).profiles()], ["Keep"])

    def test_profile_name_and_shape_validation(self):
        for body in ({"name": ""}, {"name": " "}, {"name": "a" * 65}, {"name": "x\n"},
                     {"name": 1}, {"name": "x", "extra": True}, []):
            with self.subTest(body=body), self.assertRaises(ApiError):
                self.service.create_profile(body)
        self.service.create_profile({"name": "Focus"})
        with self.assertRaises(ApiError) as error:
            self.service.create_profile({"name": "focus"})
        self.assertEqual(error.exception.status, 409)

    def test_invalid_unicode_name_does_not_reach_persistence(self):
        original = self.service.create_profile({"name": "First"})
        with self.assertRaises(ApiError) as error:
            self.service.create_profile({"name": "broken\ud800"})
        self.assertEqual(error.exception.code, "invalid_name")
        self.assertEqual(self.service.profiles(), [original])
        self.assertEqual(list(Path(self.directory.name).glob("*.tmp")), [])

    def test_unicode_profile_round_trip(self):
        profile = self.service.create_profile({"name": "Çalışma 🎮"})
        self.assertEqual(DeviceService(self.directory.name).profiles(), [profile])

    def test_invalid_unicode_profile_id_is_rejected_on_load(self):
        profile = self.service.create_profile({"name": "First"})
        profile["id"] = "\ud800"
        store = Path(self.directory.name) / "profiles.json"
        payload = json.dumps([profile])
        store.write_text(payload, encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Invalid profile store"):
            DeviceService(self.directory.name)
        self.assertEqual(store.read_text(encoding="utf-8"), payload)

    def test_failed_save_does_not_publish_or_damage_existing_profile(self):
        original = self.service.create_profile({"name": "First"})
        with patch("backend.service.os.replace", side_effect=PermissionError("locked")):
            with self.assertRaises(ApiError) as error:
                self.service.create_profile({"name": "Second"})
        self.assertEqual(error.exception.code, "persistence_failed")
        self.assertEqual(self.service.profiles(), [original])
        self.assertEqual(DeviceService(self.directory.name).profiles(), [original])
        self.assertEqual(list(Path(self.directory.name).glob("*.tmp")), [])

    def test_corrupt_or_incompatible_profiles_fail_explicitly(self):
        store = Path(self.directory.name) / "profiles.json"
        for payload in ("not JSON", "{}", json.dumps([{"id": "a", "name": "x", "settings": {}}])):
            store.write_text(payload, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Invalid profile store"):
                DeviceService(self.directory.name)
            self.assertEqual(store.read_text(encoding="utf-8"), payload)

    def test_separate_instances_refresh_profiles_and_activate_new_snapshot(self):
        other = DeviceService(self.directory.name)
        self.service.update("aerox-3", {"dpi": 3200})
        first = self.service.create_profile({"name": "First"})
        second = other.create_profile({"name": "Second"})
        self.assertEqual(self.service.profiles(), [first, second])
        self.assertEqual(other.activate(first["id"])["devices"][2]["settings"]["dpi"], 3200)
        with self.assertRaises(ApiError) as error:
            other.create_profile({"name": "first"})
        self.assertEqual(error.exception.code, "duplicate_profile")

    def test_concurrent_instances_preserve_all_profile_writes(self):
        services = [DeviceService(self.directory.name) for _ in range(6)]
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda item: item[1].create_profile({"name": f"Profile {item[0]}"}), enumerate(services)))
        self.assertEqual({p["id"] for p in self.service.profiles()}, {p["id"] for p in results})


NIGHT = {"type": "time", "start": "22:00", "end": "07:00"}
GAME = {"type": "app", "apps": ["game.exe"]}


class AutomaticProfileTests(unittest.TestCase):
    """Simulated devices; the tests drive the rule checks with their own clock and programs."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        watcher = patch.object(DeviceService, "_watch_rules", lambda service: None)
        watcher.start()
        self.addCleanup(watcher.stop)
        self.service = DeviceService(self.directory.name)
        self.service.update("aerox-3", {"dpi": 800})
        self.day = self.service.create_profile({"name": "Day"})
        self.night = self.automatic("Night", NIGHT, 1600)
        self.service.activate(self.day["id"])

    def automatic(self, name, trigger, dpi):
        profile = self.service.create_profile({"name": name, "kind": "automatic", "trigger": trigger})
        self.service.update("aerox-3", {"dpi": dpi})  # active by hand after creation: configures it
        return profile

    def check(self, hour, apps=()):
        self.service.check_rules(datetime(2026, 11, 10, hour), frozenset(apps))
        state = self.service.profile_state()
        dpi = self.service.devices()[2]["settings"]["dpi"]
        return dpi, state["activeProfileId"], state["activeBy"]

    def test_a_rule_applies_its_profile_and_the_main_profile_returns_when_it_ends(self):
        self.assertEqual(self.check(12), (800, self.day["id"], "main"))
        self.assertEqual(self.check(23), (1600, self.night["id"], "rule"))
        self.assertEqual(self.check(8), (800, self.day["id"], "main"))

    def test_overlapping_rules_end_on_the_main_profile_not_the_previous_one(self):
        game = self.automatic("Game", GAME, 3200)
        self.service.activate(self.day["id"])
        self.assertEqual(self.check(23, {"game.exe"}), (3200, game["id"], "rule"))  # the program beats the clock
        self.assertEqual(self.check(23), (1600, self.night["id"], "rule"))  # game closed, still night
        self.assertEqual(self.check(23, {"game.exe"}), (3200, game["id"], "rule"))
        self.assertEqual(self.check(8, {"game.exe"}), (3200, game["id"], "rule"))  # morning, still playing
        self.assertEqual(self.check(8), (800, self.day["id"], "main"))  # not Night, which applied before

    def test_choosing_a_main_profile_sets_the_active_rule_aside_until_it_ends(self):
        self.check(23)
        evening = self.service.create_profile({"name": "Evening"})  # a new main profile is chosen
        self.assertEqual(self.check(23)[1:], (evening["id"], "main"))
        self.service.activate(self.day["id"])
        self.assertEqual(self.check(23), (800, self.day["id"], "main"))
        self.check(8)
        self.assertEqual(self.check(23)[1:], (self.night["id"], "rule"))  # the next night applies it again

    def test_changes_are_saved_into_the_active_profile(self):
        self.check(23)
        self.service.update("aerox-3", {"dpi": 2000})
        self.assertEqual(self.check(8), (800, self.day["id"], "main"))
        self.assertEqual(self.check(23)[0], 2000)
        settings = {p["name"]: p["settings"]["aerox-3"]["dpi"] for p in self.service.profiles()}
        self.assertEqual(settings, {"Day": 800, "Night": 2000})

    def test_an_automatic_profile_applied_by_hand_lasts_until_a_rule_starts_or_ends(self):
        self.check(12)
        self.service.activate(self.night["id"])
        self.assertEqual(self.check(13), (1600, self.night["id"], "manual"))
        self.assertEqual(self.check(23)[1:], (self.night["id"], "rule"))
        self.assertEqual(self.check(8)[1:], (self.day["id"], "main"))

    def test_deleting_profiles_keeps_a_main_profile_to_return_to(self):
        self.check(23)
        self.assertEqual(len(self.service.delete_profile(self.night["id"])), 1)
        self.assertEqual(self.check(23), (800, self.day["id"], "main"))
        self.service.create_profile({"name": "Game", "kind": "automatic", "trigger": GAME})
        with self.assertRaises(ApiError) as error:  # the automatic profile needs it
            self.service.delete_profile(self.day["id"])
        self.assertEqual(error.exception.code, "main_profile_in_use")

    def test_the_first_automatic_profile_creates_a_default_main_profile(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        service = DeviceService(directory.name)
        with patch("backend.service.system_language", return_value="tr"):
            night = service.create_profile({"name": "Gece", "kind": "automatic", "trigger": NIGHT})
        default = next(p for p in service.profiles() if p["kind"] == "main")
        self.assertEqual(default["name"], "Varsayılan")
        self.assertEqual(service.profile_state(), {"mainProfileId": default["id"], "activeProfileId": night["id"], "activeBy": "manual"})
        for body in ({"name": "x", "kind": "automatic"}, {"name": "x", "trigger": NIGHT}, {"name": "x", "kind": "other"},
                     {"name": "x", "kind": "automatic", "trigger": {"type": "time", "start": "1", "end": "2"}}):
            with self.subTest(body=body), self.assertRaises(ApiError):
                service.create_profile(body)

    def test_rules_can_be_changed_and_the_state_survives_a_restart(self):
        self.service.update_profile(self.night["id"], {"trigger": {"type": "time", "start": "12:00", "end": "13:00"}, "name": "Lunch"})
        self.assertEqual(self.check(12)[1:], (self.night["id"], "rule"))
        with self.assertRaises(ApiError):
            self.service.update_profile(self.day["id"], {"trigger": NIGHT})
        restarted = DeviceService(self.directory.name)
        self.assertEqual(restarted.profile_state()["activeProfileId"], self.night["id"])
        restarted.check_rules(datetime(2026, 11, 10, 14), frozenset())  # the app was closed when the rule ended
        self.assertEqual(restarted.profile_state(), {"mainProfileId": self.day["id"], "activeProfileId": self.day["id"], "activeBy": "main"})
        self.assertEqual(restarted.devices()[2]["settings"]["dpi"], 800)

    def test_profiles_saved_before_automatic_profiles_load_as_main_profiles(self):
        store = Path(self.directory.name) / "profiles.json"
        stored = json.loads(store.read_text(encoding="utf-8"))
        old = [{"id": p["id"], "name": p["name"], "settings": p["settings"]} for p in stored if p["kind"] == "main"]
        store.write_text(json.dumps(old), encoding="utf-8")
        self.assertEqual([p["kind"] for p in DeviceService(self.directory.name).profiles()], ["main"])


class RecordingHardware:
    """Hardware provider double: the mouse cannot read settings back, the headset can."""

    def __init__(self):
        self.ready = {"aerox-3": True, "g733": True, "mx-keys-mini": False}
        self.headset = {"lightingColor": "#ff0000", "lightingBrightness": 100, "lightingEffect": "static"}
        self.applied, self.failures = [], 0

    def devices(self):
        def device(device_id, kind, capabilities, settings, limits, verification):
            status = "connected" if self.ready[device_id] else "unavailable"
            return Device(device_id, device_id, kind, "connected", 50, capabilities, dict(settings), limits,
                          status={"code": status, "message": ""}, telemetry={"writeVerification": verification})
        lighting = {"lighting": True, "dpi": False, "pollingRate": False, "backlight": False}
        return [device("aerox-3", "mouse", {**lighting, "dpi": True}, {}, {"dpi": {"min": 100, "max": 18000, "step": 100},
                       "lightingEffects": ["static", "breathing", "rainbow"], "lightingSpeedEffects": ["breathing"]}, "acknowledged"),
                device("g733", "headset", lighting, self.headset, {"lightingEffects": ["static", "breathing", "rainbow"],
                       "lightingSpeedEffects": ["breathing", "rainbow"]}, "readback"),
                device("mx-keys-mini", "keyboard", {**lighting, "lighting": False, "backlight": True},
                       {"backlightMode": "auto", "backlightLevel": 2}, {"backlightLevel": {"min": 0, "max": 7, "step": 1}}, "readback")]

    def apply(self, updates):
        if self.failures:
            self.failures -= 1
            raise OSError("device asleep")
        self.applied.append(updates)
        if "g733" in updates:
            self.headset = dict(updates["g733"])  # the headset reports what was written


class RestoreTests(unittest.TestCase):
    saved = {"aerox-3": {"dpi": 800, "lightingColor": "#ffd000", "lightingBrightness": 100},
             "g733": {"lightingColor": "#ff0000", "lightingBrightness": 100, "lightingEffect": "static", "lightingSpeed": "fast"},
             "mx-keys-mini": {"backlightMode": "auto", "backlightLevel": 7}}

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        Path(self.directory.name, "hardware-settings.json").write_text(json.dumps(self.saved), encoding="utf-8")
        watcher = patch.object(DeviceService, "_watch_devices", lambda service: None)  # the tests drive restore_settings
        watcher.start()
        self.addCleanup(watcher.stop)
        self.hardware = RecordingHardware()
        self.service = DeviceService(self.directory.name, self.hardware, mode="hardware")
        self.service.automation.reapply = Mock()

    def test_saved_settings_are_applied_when_devices_appear_return_or_drift(self):
        mouse, headset = {"aerox-3": self.saved["aerox-3"]}, {"g733": self.saved["g733"]}
        self.service.restore_settings()
        # The mouse cannot report its settings: they are sent. The headset already shows its saved
        # colour (speed means nothing for a static effect). The keyboard is asleep.
        self.assertEqual(self.hardware.applied, [mouse])
        self.assertEqual({call.args[0] for call in self.service.automation.reapply.call_args_list}, {"aerox-3", "g733"})
        self.service.restore_settings()
        self.assertEqual(self.hardware.applied, [mouse], "devices that kept their settings are left alone")
        self.hardware.ready["aerox-3"] = False  # powered off
        self.hardware.headset = {"lightingEffect": "rainbow", "lightingBrightness": 100}  # off and on between checks
        self.hardware.ready["mx-keys-mini"] = True  # woke up; automatic mode chooses its own level
        self.service.restore_settings()
        self.assertEqual(self.hardware.applied, [mouse, headset])
        self.hardware.ready["aerox-3"] = True
        self.service.restore_settings()
        self.assertEqual(self.hardware.applied, [mouse, headset, mouse])
        self.service.restore_settings()
        self.assertEqual(len(self.hardware.applied), 3)
    def test_settings_unused_by_the_saved_mode_are_not_compared(self):
        matches = DeviceService._matches
        self.assertTrue(matches({"lightingEffect": "rainbow", "lightingBrightness": 60},
                                {"lightingEffect": "rainbow", "lightingBrightness": 60, "lightingColor": "#ff0000"}))
        self.assertTrue(matches({"backlightMode": "auto", "backlightLevel": 2}, {"backlightMode": "auto", "backlightLevel": 7}))
        self.assertFalse(matches({"backlightMode": "manual", "backlightLevel": 2}, {"backlightMode": "manual", "backlightLevel": 7}))

    def test_waking_up_drops_stale_readings_and_checks_at_once(self):
        self.hardware.forget_readings = Mock()
        self.service.recheck_devices()
        self.hardware.forget_readings.assert_called_once()
        self.assertTrue(self.service._recheck.is_set())

    def test_devices_that_came_back_are_restored_and_announced(self):
        self.service.restore_settings()
        self.assertEqual(len(self.hardware.applied), 1)
        heard = []
        self.service.add_device_listener(lambda: heard.append(1))
        revision = self.service.devices_revision
        self.service.devices_changed({"aerox-3"})  # e.g. its cable was plugged in, it never looked gone
        self.assertNotIn("aerox-3", self.service._restored)
        self.assertTrue(self.service._recheck.is_set())
        self.assertEqual((heard, self.service.devices_revision > revision), ([1], True))
        self.service.restore_settings()
        self.assertEqual(self.hardware.applied[-1], {"aerox-3": self.saved["aerox-3"]})
    def test_a_failed_restore_is_retried_on_the_next_check(self):
        self.hardware.failures = 1
        self.service.restore_settings()
        self.assertEqual(self.hardware.applied, [])
        self.assertNotIn("aerox-3", self.service._restored)
        self.service.restore_settings()
        self.assertEqual(self.hardware.applied, [{"aerox-3": self.saved["aerox-3"]}])

    def test_a_profile_reaches_devices_that_are_off_when_they_come_back(self):
        self.service.restore_settings()
        red = self.service.create_profile({"name": "Red"})
        self.assertEqual(red["settings"]["aerox-3"], self.saved["aerox-3"], "the mouse's settings come from this app")
        self.service.create_profile({"name": "Blue"})
        self.service.update("g733", dict(self.saved["g733"], lightingColor="#0000ff"))  # saved into Blue
        self.hardware.ready["g733"] = False  # switched off
        self.hardware.applied.clear()
        self.service.activate(red["id"])
        self.assertEqual([sorted(update) for update in self.hardware.applied], [["aerox-3"]])
        self.hardware.ready["g733"] = True
        self.service.restore_settings()
        self.assertEqual(self.hardware.applied[-1]["g733"]["lightingColor"], "#ff0000")
        self.service.restore_settings()
        self.assertEqual(len(self.hardware.applied), 2, "sent once")


if __name__ == "__main__":
    unittest.main()

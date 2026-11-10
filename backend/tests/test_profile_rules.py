from datetime import datetime
import os
import unittest

from backend import profile_rules
from backend.domain import ApiError

NIGHT = {"type": "time", "start": "22:00", "end": "07:00"}


def at(hour, minute=0):
    return datetime(2026, 11, 10, hour, minute)


class TriggerTests(unittest.TestCase):
    def test_time_ranges_may_pass_midnight_and_end_is_exclusive(self):
        holds = lambda trigger, now: profile_rules.holds(trigger, now, frozenset())
        self.assertEqual([holds(NIGHT, at(h, m)) for h, m in ((21, 59), (22, 0), (3, 0), (6, 59), (7, 0))],
                         [False, True, True, True, False])
        day = {"type": "time", "start": "09:30", "end": "17:00"}
        self.assertEqual([holds(day, at(h, m)) for h, m in ((9, 29), (9, 30), (16, 59), (17, 0))], [False, True, True, False])

    def test_triggers_are_validated_and_app_paths_become_file_names(self):
        self.assertEqual(profile_rules.validate_trigger({"type": "app", "apps": [r"C:\Games\CS2.exe", "cs2.exe", "notepad.EXE"]}),
                         {"type": "app", "apps": ["cs2.exe", "notepad.exe"]})
        for trigger in ({"type": "time", "start": "22:00", "end": "22:00"}, {"type": "time", "start": "24:00", "end": "07:00"},
                        {"type": "time", "start": "7:00", "end": "08:00"}, {"type": "time", "start": "22:00"},
                        {"type": "app", "apps": []}, {"type": "app", "apps": ["game.bat"]}, {"type": "app", "apps": "x.exe"},
                        {"type": "app", "apps": ["x.exe"] * 17}, {"type": "app", "apps": ["x.exe"], "extra": 1}, {"type": "usb"}, None):
            with self.subTest(trigger=trigger), self.assertRaises(ApiError) as error:
                profile_rules.validate_trigger(trigger)
            self.assertEqual(error.exception.code, "invalid_trigger")

    def test_an_application_rule_beats_a_time_rule_then_list_order_decides(self):
        profiles = [{"id": "main", "kind": "main"},
                    {"id": "night", "kind": "automatic", "trigger": NIGHT},
                    {"id": "late", "kind": "automatic", "trigger": {"type": "time", "start": "23:00", "end": "01:00"}},
                    {"id": "game", "kind": "automatic", "trigger": {"type": "app", "apps": ["game.exe"]}}]
        self.assertEqual(profile_rules.active(profiles, at(23, 30), frozenset({"game.exe"})), ["game", "night", "late"])
        self.assertEqual(profile_rules.active(profiles, at(12), frozenset({"explorer.exe"})), [])
        self.assertTrue(profile_rules.needs_apps(profiles))
        self.assertFalse(profile_rules.needs_apps(profiles[:3]))

    @unittest.skipUnless(os.name == "nt", "Windows process list")
    def test_running_programs_include_this_one(self):
        self.assertIn(os.path.basename(os.sys.executable).lower(), profile_rules.running_apps())


if __name__ == "__main__":
    unittest.main()

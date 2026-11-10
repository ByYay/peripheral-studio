"""Synthetic contract tests; no physical devices or OS input are exercised."""

import tempfile
import unittest
from pathlib import Path

from automation.engine import Action, Engine, InputEvent, Recorder, validate_events


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.engine = Engine({"test:button4", "test:button5"}, {"CTRL", "A"}, {"left"})
        self.action = Action("shortcut", keys=("CTRL", "A"))

    def test_plan_and_profile_switch(self):
        self.engine.assign("profile-1", "test:button4", self.action)
        self.assertIsNone(self.engine.plan("test:button4"))
        self.engine.switch("profile-1")
        plan = self.engine.plan("test:button4")
        self.assertFalse(plan.execution_enabled)
        self.assertEqual([e.value for e in plan.events], ["CTRL", "A", "A", "CTRL"])
        validate_events(plan.events)
        self.assertIsNone(self.engine.plan("test:button4", injected=True))
        self.engine.switch("profile-2")
        self.assertIsNone(self.engine.plan("test:button4"))

    def test_conflicts_and_explicit_replace(self):
        self.engine.assign("p", "test:button4", self.action)
        self.assertEqual(self.engine.conflicts("p", "test:button4", self.action),
                         ["control_already_assigned", "duplicate_action"])
        with self.assertRaises(ValueError):
            self.engine.assign("p", "test:button4", self.action)
        self.engine.assign("p", "test:button4", self.action, replace=True)
        self.engine.unassign("p", "test:button4")
        self.assertEqual(self.engine.conflicts("p", "test:button4", self.action), [])

    def test_no_invented_capabilities(self):
        with self.assertRaises(ValueError):
            self.engine.assign("p", "g733:unknown", self.action)
        with self.assertRaises(ValueError):
            self.engine.validate_action(Action("shortcut", keys=("UNKNOWN",)))

    def test_urls(self):
        self.engine.validate_action(Action("url", "https://example.org/path"))
        for url in ("javascript:alert(1)", "file:///tmp/test", "https://u:p@example.org", "https://example.org\n", "https://example.org:bad"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.engine.validate_action(Action("url", url))

    def test_scripts_are_local_and_execution_disabled(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "approved.py"
            path.write_text("raise RuntimeError('must never execute')")
            self.engine.assign("p", "test:button4", Action("script", str(path)))
            self.engine.switch("p")
            self.assertFalse(self.engine.plan("test:button4").execution_enabled)
            with self.assertRaises(PermissionError):
                self.engine.trigger("test:button4")
        for target in ("https://example.org/script.py", "relative.py", "\\\\server\\share\\script.py"):
            with self.assertRaises(ValueError):
                self.engine.validate_action(Action("script", target))

    def test_invalid_macros(self):
        for events in ((InputEvent("key_down", "A"),),
                       (InputEvent("key_up", "A"),),
                       (InputEvent("wait", delay=float("nan")),),
                       (InputEvent("wait", delay=61),),
                       (InputEvent("wait"),) * 1001):
            with self.subTest(events=events[:1]), self.assertRaises(ValueError):
                validate_events(events)

    def test_recording_and_playback_plan(self):
        times = iter([0, .1, .2])
        recorder = Recorder(lambda: next(times))
        recorder.start()
        recorder.feed("key_down", "A", injected=True)
        recorder.feed("key_down", "A")
        recorder.feed("key_up", "A")
        events = recorder.stop()
        self.assertEqual(len(events), 2)
        self.engine.assign("p", "test:button4", Action("macro", events=events))
        self.engine.switch("p")
        self.assertEqual(self.engine.plan("test:button4").events, events)
        self.assertFalse(recorder.active)

    def test_recording_limit_and_unbalanced_stop(self):
        times = iter([0, 61])
        recorder = Recorder(lambda: next(times))
        recorder.start()
        with self.assertRaises(ValueError):
            recorder.feed("key_down", "A")
        self.assertFalse(recorder.active)
        recorder = Recorder(lambda: 0)
        recorder.start()
        recorder.feed("key_down", "A")
        with self.assertRaises(ValueError):
            recorder.stop()
        self.assertFalse(recorder.active)

    def test_strict_immutable_actions(self):
        with self.assertRaises(ValueError):
            Action("shortcut", keys=["A"])
        with self.assertRaises(ValueError):
            self.engine.validate_action(Action("shortcut", target="extra", keys=("A",)))


if __name__ == "__main__":
    unittest.main()

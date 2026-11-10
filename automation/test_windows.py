"""Executor regression tests. Live input tests target only an owned Tk window."""

import os
from pathlib import Path
import tempfile
from threading import Event
import time
import unittest
from unittest.mock import patch

from automation.engine import Action, InputEvent
from automation.windows import WindowsAutomation


class FakeInput:
    def __init__(self):
        self.running = False
        self.events = []
        self.window = 123
        self.fail = False
        self.rehooks = 0

    def start(self, receive):
        self.receive = receive
        self.running = True

    def stop(self):
        self.running = False

    request_stop = stop

    def rehook(self):
        self.rehooks += 1

    def foreground(self):
        return self.window

    def owned_window(self, window):
        return window == 123

    def fixed_drive(self, path):
        return True

    def modifiers_down(self):
        return False

    def input_down(self, event):
        return False

    def emit(self, event):
        if self.fail and event.kind == "key_down" and event.value == "A":
            raise OSError("injection refused")
        self.events.append(event)


def wait_idle(service):
    deadline = time.monotonic() + 5
    while service.status()["busy"] and time.monotonic() < deadline:
        time.sleep(.01)
    if service.status()["busy"]:
        raise AssertionError("Worker failed to stop")


class WindowsTests(unittest.TestCase):
    def test_waking_up_reinstalls_hooks_or_restarts_a_stopped_hook_thread(self):
        service = WindowsAutomation(lambda url: None)
        service._input = fake = FakeInput()
        service.refresh_hooks()  # not enabled: nothing to refresh
        self.assertEqual((fake.rehooks, fake.running), (0, False))
        service.configure("devices", {"key:F23": Action("url", target="https://example.org/")})
        service.enable()
        service.refresh_hooks()
        self.assertEqual(fake.rehooks, 1)
        fake.running = False  # the hook thread ended after resume
        service.refresh_hooks()
        self.assertTrue(fake.running)
        self.assertTrue(service.status()["enabled"])
        service.close()

    def setUp(self):
        self.fake = FakeInput()
        patcher = patch("automation.windows.Win32Input", return_value=self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.urls = []
        self.service = WindowsAutomation(self.urls.append)
        self.addCleanup(self.service.close)

    def test_explicit_enable_and_release(self):
        self.service.configure("p", {"key:F8": Action("shortcut", keys=("LCTRL", "A"))})
        with self.assertRaises(PermissionError):
            self.service.execute("key:F8")
        self.service.enable()
        self.service.execute("key:F8")
        wait_idle(self.service)
        self.assertEqual([e.kind for e in self.fake.events], ["key_down", "key_down", "key_up", "key_up"])
        self.assertIsNone(self.service.status()["error"])

    def test_failure_releases_modifier(self):
        self.fake.fail = True
        self.service.configure("p", {"key:F8": Action("shortcut", keys=("LCTRL", "A"))})
        self.service.enable()
        self.service.execute("key:F8")
        wait_idle(self.service)
        self.assertEqual(self.fake.events[-1], InputEvent("key_up", "LCTRL"))
        self.assertIn("injection refused", self.service.status()["error"])

    def test_cancel_during_wait_releases_key(self):
        events = (InputEvent("key_down", "A"), InputEvent("key_up", "A", 5))
        self.service.configure("p", {"key:F8": Action("macro", events=events)})
        self.service.enable()
        self.service.execute("key:F8")
        deadline = time.monotonic() + 2
        while not self.fake.events and time.monotonic() < deadline:
            time.sleep(.01)
        self.service.disable()
        wait_idle(self.service)
        self.assertEqual(self.fake.events[-1], InputEvent("key_up", "A"))

    def test_release_trigger_and_emergency_stop(self):
        self.service.configure("p", {"button:x1": Action("url", "https://example.org")})
        self.service.enable()
        self.service._receive("button", "x1", True)
        self.assertEqual(self.urls, [])
        self.service._receive("button", "x1", False)
        wait_idle(self.service)
        self.assertEqual(self.urls, ["https://example.org"])
        # Plain device-button actions survive ordinary F12 use.
        self.service._receive("key", "F12", True)
        self.assertTrue(self.service.status()["enabled"])
        # While a remap suppresses real input, F12 is the emergency stop.
        self.service.configure("p", {"button:x1": Action("remap", keys=("A",))})
        self.service.enable()
        self.service._receive("key", "F12", True)
        self.assertFalse(self.service.status()["enabled"])

    def test_open_action_uses_the_shell_for_local_files(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "notes.txt"
            target.write_text("x", encoding="utf-8")
            self.service.configure("p", {"key:F23": Action("open", str(target))})
            self.service.enable()
            with patch("automation.windows.os.startfile") as startfile:
                self.service.execute("key:F23")
                wait_idle(self.service)
            startfile.assert_called_once_with(str(target.resolve()))
            with self.assertRaises(OSError):  # FileNotFoundError from the strict path check
                self.service.configure("p", {"key:F23": Action("open", str(Path(directory) / "missing.txt"))})

    def test_media_keys_are_sent_as_single_key_presses(self):
        self.service.configure("p", {"key:F23": Action("shortcut", keys=("MEDIA_PLAY_PAUSE",))})
        self.service.enable()
        self.service.execute("key:F23")
        wait_idle(self.service)
        self.assertEqual(self.fake.events, [InputEvent("key_down", "MEDIA_PLAY_PAUSE"), InputEvent("key_up", "MEDIA_PLAY_PAUSE")])

    def test_recording_focus_and_cancel(self):
        self.service.enable()
        self.service.start_recording()
        self.service._receive("key", "A", True)
        self.service._receive("key", "A", False)
        events = self.service.stop_recording()
        self.assertEqual([event.kind for event in events], ["key_down", "key_up"])
        self.service.start_recording()
        self.fake.window = 999
        self.service._receive("key", "A", True)
        self.assertFalse(self.service.status()["recording"])
        self.assertIn("focus", self.service.status()["error"])

    def test_invalid_configuration_is_atomic(self):
        self.service.configure("p", {"key:F8": Action("url", "https://example.org")})
        self.service.enable()
        with self.assertRaises(ValueError):
            self.service.configure("p", {"key:F12": Action("url", "https://example.org")})
        self.assertTrue(self.service.status()["enabled"])
        self.service.execute("key:F8")
        wait_idle(self.service)
        self.assertEqual(len(self.urls), 1)

    def test_script_approval_receives_exact_content_and_decline_does_not_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "script.py"
            source.write_text("print('test')", encoding="utf-8")
            reviews = []
            self.service._request_script_approval = lambda review: reviews.append(review) or False
            self.service._python = str(Path(directory) / "python.exe")
            Path(self.service._python).touch()
            self.service.configure("p", {"key:F8": Action("script", str(source))})
            self.service.enable()
            with patch("automation.windows.subprocess.Popen") as launch:
                self.service.execute("key:F8")
                wait_idle(self.service)
                launch.assert_not_called()
            self.assertEqual(reviews[0]["content"], "print('test')")
            self.assertIn("declined", self.service.status()["error"])

    def test_strict_action_parser(self):
        self.assertEqual(Action.from_dict({"kind": "shortcut", "keys": ["A"]}), Action("shortcut", keys=("A",)))
        for raw in ({"kind": "url", "approved": True}, {"kind": "macro", "events": [None]}, {"kind": "shortcut", "keys": "A"}):
            with self.assertRaises(ValueError):
                Action.from_dict(raw)


@unittest.skipUnless(os.name == "nt" and os.environ.get("UPM_LIVE_INPUT_TEST") == "1", "Opt-in owned Windows test window")
class LiveWindowsTests(unittest.TestCase):
    def test_hooks_are_replaced_without_stopping_the_hook_thread(self):
        from automation.win32 import Win32Input
        hooks = Win32Input()
        hooks.start(lambda family, value, down: False)
        try:
            self.assertEqual(hooks.installs, 1)
            hooks.rehook()
            deadline = time.monotonic() + 3
            while hooks.installs < 2 and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertEqual(hooks.installs, 2)
            self.assertTrue(hooks.running)
        finally:
            hooks.stop()

    def test_owned_window_sendinput_and_injected_hook_filter(self):
        import tkinter as tk
        root = tk.Tk()
        root.title("UPM owned input test")
        entry = tk.Entry(root)
        entry.pack()
        root.update()
        root.lift()
        root.focus_force()
        entry.focus_force()
        root.update()
        service = WindowsAutomation(lambda url: self.fail("Unexpected browser launch"))
        try:
            service.configure("test", {"key:F8": Action("shortcut", keys=("A",))})
            service.enable()
            self.assertTrue(service.status()["enabled"])
            service.start_recording()
            # SendInput must not be recorded by the physical-input hook.
            service._input.emit(InputEvent("key_down", "A"))
            service._input.emit(InputEvent("key_up", "A"))
            for _ in range(10):
                root.update()
                time.sleep(.01)
            with self.assertRaises(ValueError):
                service.stop_recording()  # empty, because injected input is ignored
            entry.delete(0, "end")
            service.execute("key:F8")
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                root.update()
                if not service.status()["busy"]:
                    break
                time.sleep(.01)
            root.update()
            self.assertEqual(entry.get().lower(), "a")
            self.assertIsNone(service.status()["error"])
        finally:
            service.close()
            root.destroy()

    def test_approved_local_script_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "marker.txt"
            source = Path(directory) / "owned.py"
            source.write_text(f"from pathlib import Path\nPath({str(marker)!r}).write_text('ok')", encoding="utf-8")
            reviews = []
            service = WindowsAutomation(lambda url: None, request_script_approval=lambda review: reviews.append(review) or True)
            try:
                service.configure("test", {"key:F8": Action("script", str(source))})
                service.enable()
                service.execute("key:F8")
                wait_idle(service)
                self.assertIsNone(service.status()["error"])
                self.assertEqual(marker.read_text(), "ok")
                self.assertEqual(len(reviews), 1)
                # A modified script must request fresh approval.
                source.write_text("pass", encoding="utf-8")
                service.execute("key:F8")
                wait_idle(service)
                self.assertEqual(len(reviews), 2)
            finally:
                service.close()


if __name__ == "__main__":
    unittest.main()

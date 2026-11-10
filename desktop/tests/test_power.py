import ctypes
from ctypes import wintypes
import subprocess
import sys
import threading
import time
import unittest

from desktop.power import WakeWatcher


class WakeWatcherTests(unittest.TestCase):
    def watch(self, clock_steps, refresh=600.0):
        """Run the watcher with a tiny tick and a scripted wall clock; return how often it fired."""
        steps = iter(clock_steps)
        fired, done, last = [], threading.Event(), [0.0]

        def clock():
            try:
                last[0] = next(steps)
            except StopIteration:
                done.set()  # the clock stays put from now on: no further wake-ups
            return last[0]
        watcher = WakeWatcher(lambda: fired.append(1), tick=0.01, refresh=refresh, clock=clock)
        watcher.start()
        done.wait(2)
        watcher.stop()
        return len(fired)

    def test_a_jump_of_the_wall_clock_means_the_computer_slept(self):
        # Ticks of 0.01 s, then 3600 s pass in one tick (sleep), then normal ticks again.
        self.assertEqual(self.watch([0, 0.01, 0.02, 3600.02, 3600.03]), 1)

    def test_normal_ticks_only_refresh_on_the_safety_interval(self):
        self.assertEqual(self.watch([i / 100 for i in range(6)]), 0)
        self.assertGreaterEqual(self.watch([i / 100 for i in range(6)], refresh=0.02), 2)  # every 2 ticks

    def test_a_failing_refresh_keeps_watching(self):
        calls = []

        def on_wake():
            calls.append(1)
            raise RuntimeError("device busy")
        watcher = WakeWatcher(on_wake, tick=0.01, refresh=0.01)
        with self.assertLogs("desktop.power", "ERROR"):
            watcher.start()
            deadline = time.monotonic() + 2
            while len(calls) < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
        watcher.stop()
        self.assertGreaterEqual(len(calls), 2)


@unittest.skipUnless(sys.platform == "win32", "Windows Error Reporting")
class RestartRegistrationTests(unittest.TestCase):
    def test_windows_records_the_restart_command(self):
        child = subprocess.Popen([sys.executable, "-c",
                                  "import sys, time; sys.path.insert(0, '.');"
                                  "from desktop.power import register_restart;"
                                  "print(register_restart('--simulation'), flush=True); time.sleep(5)"],
                                 stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), "True")
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.restype = wintypes.HANDLE
            process = kernel32.OpenProcess(0x0410, False, child.pid)  # QUERY_INFORMATION | VM_READ
            self.assertTrue(process)
            try:
                size, flags = wintypes.DWORD(64), wintypes.DWORD()
                command = ctypes.create_unicode_buffer(64)
                result = kernel32.GetApplicationRestartSettings(wintypes.HANDLE(process), command,
                                                                ctypes.byref(size), ctypes.byref(flags))
                self.assertEqual(result, 0)
                self.assertEqual((command.value, flags.value), ("--simulation", 4 | 8))
            finally:
                kernel32.CloseHandle(wintypes.HANDLE(process))
        finally:
            child.kill()
            child.wait()


if __name__ == "__main__":
    unittest.main()

"""Long-running app support: waking up from sleep and recovering from crashes or hangs."""

import ctypes
import logging
import threading
import time

LOGGER = logging.getLogger(__name__)
RESTART_NO_PATCH, RESTART_NO_REBOOT = 4, 8


class WakeWatcher:
    """Calls on_wake after the computer resumes from sleep or hibernation, seen as a jump of
    the wall clock between two short waits (no window or power notification needed), and every
    `refresh` seconds as a safety net: Windows can also drop input hooks without sleeping."""

    def __init__(self, on_wake, tick=15.0, refresh=600.0, clock=time.time):
        self._on_wake, self._tick, self._refresh, self._clock = on_wake, tick, refresh, clock
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, name="upm-wake-watcher", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        last, waited = self._clock(), 0.0
        while not self._stop.wait(self._tick):
            now = self._clock()
            slept, last = now - last > self._tick * 3, now
            waited += self._tick
            if slept or waited >= self._refresh:
                if slept:
                    LOGGER.info("The computer woke up; refreshing input hooks and devices")
                waited = 0.0
                try:
                    self._on_wake()
                except Exception:
                    LOGGER.exception("Refreshing after wake-up failed")


def register_restart(arguments: str) -> bool:
    """Ask Windows to start the app again if it crashes or stops responding (once it has run
    for 60 seconds); a normal Quit is not restarted, and boot is left to the startup entry."""
    result = ctypes.windll.kernel32.RegisterApplicationRestart(ctypes.c_wchar_p(arguments),
                                                               RESTART_NO_PATCH | RESTART_NO_REBOOT)
    if result != 0:
        LOGGER.warning("Automatic restart after a crash is unavailable (HRESULT %#x)", result & 0xFFFFFFFF)
    return result == 0

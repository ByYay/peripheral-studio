"""One running app per data folder: launching it again shows the running app's window.

A named Windows event (per user session, per data folder and mode) both detects the
running instance and carries the "show yourself" request to it.
"""

import ctypes
import hashlib
import logging
from threading import Thread

ERROR_ALREADY_EXISTS = 183
WAIT_OBJECT_0 = 0
INFINITE = 0xFFFFFFFF
ASFW_ANY = 0xFFFFFFFF
LOGGER = logging.getLogger(__name__)


class SingleInstance:
    def __init__(self, data_dir, mode):
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32.CreateEventW.restype = ctypes.c_void_p
        self._kernel32.SetEvent.argtypes = self._kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        self._kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        digest = hashlib.sha256(f"{mode}|{str(data_dir).lower()}".encode()).hexdigest()[:16]
        # Auto-reset event: each SetEvent wakes the running app once.
        self._handle = self._kernel32.CreateEventW(None, False, False, f"Local\\UnifiedPeripheralManager-{digest}")
        if not self._handle:
            raise OSError(ctypes.get_last_error(), "CreateEventW failed")
        self.first = ctypes.get_last_error() != ERROR_ALREADY_EXISTS

    def notify_running(self):
        """Ask the app that is already running to show its window."""
        # This launch owns the foreground (the user just started it); let the running app take it.
        ctypes.windll.user32.AllowSetForegroundWindow(ASFW_ANY)
        self._kernel32.SetEvent(self._handle)

    def listen(self, on_show):
        """Call on_show (on a background thread) whenever another launch asks for the window."""
        def wait():
            while self._kernel32.WaitForSingleObject(self._handle, INFINITE) == WAIT_OBJECT_0:
                try:
                    on_show()
                except Exception:  # the next launch must still be able to open the window
                    LOGGER.exception("Showing the window failed")

        # A daemon thread: the event goes away with the process's last handle at exit.
        Thread(target=wait, name="upm-single-instance", daemon=True).start()

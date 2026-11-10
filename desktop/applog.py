"""Log files for the windowed app, which has no console: warnings, recoveries and crashes go to
%LOCALAPPDATA%\\UnifiedPeripheralManager\\logs (app.log, rotated at 512 KiB; crash.log for
native crashes). Logging problems never stop the app."""

import faulthandler
import logging
from logging.handlers import RotatingFileHandler
import threading

PACKAGES = ("backend", "desktop", "hardware", "automation")
LOGGER = logging.getLogger(__name__)


class AppLog:
    def __init__(self, folder):
        self._handler = self._crash = None
        self._levels = {name: logging.getLogger(name).level for name in PACKAGES}
        self._thread_hook = threading.excepthook
        self._faults_were_on = faulthandler.is_enabled()
        try:
            folder.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(folder / "app.log", maxBytes=512 * 1024, backupCount=2,
                                          encoding="utf-8", delay=True)
            handler.setLevel(logging.INFO)
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(threadName)s %(name)s: %(message)s"))
            logging.getLogger().addHandler(handler)
            self._handler = handler
            for name in PACKAGES:
                logging.getLogger(name).setLevel(logging.INFO)
            self._crash = open(folder / "crash.log", "a", encoding="utf-8")
            faulthandler.enable(self._crash)
        except OSError:
            pass
        threading.excepthook = self._thread_stopped

    @staticmethod
    def _thread_stopped(args):
        name = args.thread.name if args.thread is not None else "?"
        LOGGER.error("Thread %s stopped unexpectedly", name, exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    def close(self):
        threading.excepthook = self._thread_hook
        if self._crash is not None:
            faulthandler.disable()
            if self._faults_were_on:
                try:
                    faulthandler.enable()  # back to the console (python -X faulthandler)
                except (RuntimeError, ValueError):
                    pass
            self._crash.close()
        if self._handler is not None:
            logging.getLogger().removeHandler(self._handler)
            self._handler.close()
        for name, level in self._levels.items():
            logging.getLogger(name).setLevel(level)

import logging
from pathlib import Path
import tempfile
import threading
import unittest

from desktop.applog import AppLog


class AppLogTests(unittest.TestCase):
    def test_warnings_and_stopped_threads_reach_the_log_file(self):
        with tempfile.TemporaryDirectory() as folder:
            log = AppLog(Path(folder) / "logs")
            try:
                logging.getLogger("hardware.runtime").info("Keyboard woke up")
                thread = threading.Thread(target=lambda: 1 / 0, name="mx-keys-listener")
                thread.start()
                thread.join()
            finally:
                log.close()
            text = (Path(folder) / "logs" / "app.log").read_text(encoding="utf-8")
            self.assertIn("Keyboard woke up", text)
            self.assertIn("Thread mx-keys-listener stopped unexpectedly", text)
            self.assertIn("ZeroDivisionError", text)
            self.assertTrue((Path(folder) / "logs" / "crash.log").is_file())
        self.assertIs(threading.excepthook, threading.__excepthook__)


if __name__ == "__main__":
    unittest.main()

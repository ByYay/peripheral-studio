import threading
import time
import unittest
from unittest.mock import Mock

from desktop.tray import TrayController


class TrayMenuTests(unittest.TestCase):
    def tray(self, autostart):
        # The icon is built but never run: no tray icon appears.
        return TrayController(Mock(), lambda: [], read_language=lambda: "tr", autostart=autostart)

    def item(self, tray, text):
        return next(item for item in tray.icon.menu.items if item.text == text)

    def test_start_with_windows_item_toggles_and_shows_the_state(self):
        autostart = Mock(available=True)
        autostart.enabled.return_value = False
        tray = self.tray(autostart)
        tray.icon.update_menu = Mock()
        item = self.item(tray, "Windows ile başlat")
        self.assertTrue(item.visible)
        self.assertFalse(item.checked)
        tray.toggle_startup()
        autostart.set.assert_called_once_with(True)
        autostart.enabled.return_value = True
        self.assertTrue(item.checked)

    def test_a_device_that_came_back_is_checked_for_low_battery_at_once(self):
        reads = []
        tray = TrayController(Mock(), lambda: reads.append(1) or [], read_language=lambda: "tr")
        icon = Mock(HAS_NOTIFICATION=False)
        worker = threading.Thread(target=tray._monitor, args=(icon,), daemon=True)
        worker.start()
        tray.ready.wait(2)
        deadline = time.monotonic() + 2
        while len(reads) < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        tray.check_now()  # instead of waiting a minute
        while len(reads) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        tray.stopped.set()  # what stop() does, without the tray icon that never ran
        tray._check.set()
        worker.join(2)
        self.assertGreaterEqual(len(reads), 2)
    def test_item_is_hidden_when_windows_cannot_start_the_app(self):
        tray = self.tray(Mock(available=False))
        self.assertFalse(self.item(tray, "Windows ile başlat").visible)


if __name__ == "__main__":
    unittest.main()

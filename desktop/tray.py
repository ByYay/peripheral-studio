"""Windows tray lifetime and notifications from confirmed device telemetry."""

import logging
from pathlib import Path
import time
from threading import Event

from .strings import text


LOGGER = logging.getLogger(__name__)
ICON = Path(__file__).parent / "assets" / "upm.png"  # drawn by packaging/make_icon.py


class BatteryAlerts:
    """Hysteresis and cooldown prevent repeated alerts on every refresh."""

    def __init__(self, clock=time.monotonic, cooldown=300):
        self.clock = clock
        self.cooldown = cooldown
        self.low = set()
        self.last_alert = {}

    def update(self, devices):
        alerts = []
        connected = set()
        for device in devices:
            identity = device.get("id")
            if not isinstance(identity, str) or device.get("connection") != "connected":
                continue
            connected.add(identity)
            level = device.get("batteryPercent")
            if type(level) is not int or not 0 <= level <= 100:
                continue
            if level > 25:
                self.low.discard(identity)
            elif level <= 20 and identity not in self.low:
                now = self.clock()
                if now - self.last_alert.get(identity, float("-inf")) >= self.cooldown:
                    alerts.append((str(device.get("name", identity)), level))
                    self.last_alert[identity] = now
                    self.low.add(identity)
        self.low.intersection_update(connected)
        return alerts


class TrayController:
    def __init__(self, lifecycle, read_devices, *, hardware=True, read_language=lambda: "en", autostart=None):
        # Deferred imports keep policy unit tests independent of native GUI libraries.
        import pystray
        from PIL import Image

        self.lifecycle = lifecycle  # desktop.lifecycle.WindowLifecycle
        self.autostart = autostart  # desktop.autostart.Autostart
        self.read_devices = read_devices
        self.hardware = hardware
        self.read_language = read_language
        self.alerts = BatteryAlerts()
        self.stopped = Event()
        self._check = Event()  # a device came back: check batteries now
        self.ready = Event()
        image = Image.open(ICON)
        self.icon = pystray.Icon(
            "UnifiedPeripheralManager", image, "Unified Peripheral Manager",
            # Callable texts are re-read whenever the native menu is rebuilt.
            menu=pystray.Menu(
                pystray.MenuItem(lambda _: self._text("tray.show"), self.show, default=True),
                pystray.MenuItem(lambda _: self._text("tray.hide"), self.hide),
                pystray.MenuItem(lambda _: self._text("tray.startup"), self.toggle_startup,
                                 checked=lambda _: self.starts_with_windows(),
                                 visible=autostart is not None and autostart.available),
                pystray.MenuItem(lambda _: self._text("tray.quit"), self.quit),
            ),
        )

    def _text(self, key, **values):
        return text(self.read_language(), key, **values)

    def refresh_language(self, *_):
        self.icon.update_menu()

    def show(self, *_):
        self.lifecycle.show()

    def hide(self, *_):
        self.lifecycle.hide()

    def starts_with_windows(self):
        try:
            return self.autostart.enabled()
        except OSError:
            return False

    def toggle_startup(self, *_):
        try:
            self.autostart.set(not self.starts_with_windows())
        except OSError as exc:
            LOGGER.warning("Could not change the Windows startup entry (%s)", type(exc).__name__)
        self.icon.update_menu()

    def quit(self, *_):
        self.stopped.set()
        self.lifecycle.quit()

    def start(self):
        self.icon.run_detached(setup=self._monitor)

    def _monitor(self, icon):
        icon.visible = True
        self.ready.set()
        failed = False
        while not self.stopped.is_set():
            if self.hardware:
                try:
                    for name, level in self.alerts.update(self.read_devices()):
                        if icon.HAS_NOTIFICATION and not self.stopped.is_set():
                            icon.notify(self._text("battery.message", name=name, level=level),
                                        self._text("battery.title"))
                    failed = False
                except Exception as exc:
                    if not failed:
                        LOGGER.warning("Tray battery refresh failed (%s)", type(exc).__name__)
                    failed = True
            self._check.wait(60)
            self._check.clear()

    def check_now(self):
        self._check.set()

    def stop(self):
        self.stopped.set()
        self._check.set()
        self.icon.stop()

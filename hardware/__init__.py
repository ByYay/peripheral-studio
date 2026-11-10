"""Peripheral MVP API. Upstream support is not physical-device verification.

Sources: https://flozz.github.io/rivalcfg/devices/aerox3_wireless.html
https://github.com/Sapd/HeadsetControl
https://pwr-solaar.github.io/Solaar/
"""
from contextlib import contextmanager
import importlib
import os
import re
import threading
from datetime import datetime, timezone


# Only the DPI button is managed: native DPI cycling, disabled, or F23 for an app action.
AEROX_WIRED = (0x183A, 0x187A)  # rivalcfg aerox3_wireless_wired: the mouse connected by its cable
DPI_BUTTON_MAPPINGS = {'dpi': 'buttons(button6=dpi)', 'disabled': 'buttons(button6=disabled)',
                       'action': 'buttons(button6=F23)'}


class DeviceError(RuntimeError):
    pass


class PeripheralAPI:
    """JSON-compatible synchronous API; call off the UI thread.

    settings writes are transport acknowledgements, not hardware readback.
    No network listener, arbitrary commands, or persistent firmware writes.
    """

    def __init__(self, *, allow_unverified_writes=False):
        self.allow_unverified_writes = allow_unverified_writes
        self._lock = threading.RLock()

    def catalog(self):
        return [
            {"id": "aerox-3", "backend": "rivalcfg", "verification": "upstream-only",
             "settings": ["dpi", "polling_rate", "top_color", "middle_color",
                          "bottom_color", "sleep_timer", "dim_timer"], "battery": True},
            {"id": "g733", "backend": "headsetcontrol", "verification": "blocked",
             "settings": [], "battery": False,
             "reason": "Backend integration pending installed executable and schema verification; upstream supports battery, lights toggle and sidetone, not RGB color."},
            {"id": "mx-keys-mini", "backend": "hid++", "verification": "blocked",
             "settings": [], "battery": False,
             "reason": "Windows transport and device feature discovery not verified; Solaar is Linux-specific."},
        ]

    def _open(self, device_id):
        if device_id != "aerox-3":
            raise DeviceError("unsupported_backend")
        if any(k.startswith("RIVALCFG_") for k in os.environ):
            raise DeviceError("rivalcfg_environment_override_not_allowed")
        try:
            devices = importlib.import_module("rivalcfg.devices")
            mouse = importlib.import_module("rivalcfg.mouse")
        except ImportError as exc:
            raise DeviceError("missing_dependency: rivalcfg") from exc
        candidates = [d for d in devices.list_plugged_devices()
                      if d["vendor_id"] == 0x1038 and d["product_id"] in
                      (0x1838, 0x183A, 0x1878, 0x187A)]
        # With its cable plugged in the mouse works in wired mode; the receiver then cannot reach it.
        wired = [d for d in candidates if d["product_id"] in AEROX_WIRED]
        candidates = wired or candidates
        if len(candidates) != 1:
            raise DeviceError("device_missing_or_ambiguous")
        return mouse.get_mouse(vendor_id=0x1038, product_id=candidates[0]["product_id"])

    def status(self, device_id):
        with self._lock:
            mouse = self._open(device_id)
            try:
                battery = mouse.battery
                level = battery.get("level")
                if level is not None and (type(level) is not int or not 0 <= level <= 100):
                    raise DeviceError("invalid_battery_response")
                return {"id": device_id, "battery_percent": level,
                        "charging": battery.get("is_charging"),
                        "observed_at": datetime.now(timezone.utc).isoformat(),
                        "source": "hardware", "settings_readback": False}
            finally:
                mouse.close()

    def set_setting(self, device_id, setting, value=None):
        method = self._validate(setting, value)
        if not self.allow_unverified_writes:
            raise DeviceError("hardware_writes_disabled_pending_verification")
        with self._lock:
            mouse = self._open(device_id)
            try:
                if setting == "rainbow_effect":
                    getattr(mouse, method)()
                else:
                    getattr(mouse, method)(value)
                return {"id": device_id, "setting": setting, "requested": value,
                        "status": "sent", "readback_verified": False}
            finally:
                mouse.close()

    @contextmanager
    def color_session(self, device_id):
        """Keep the mouse open for frequent zone color updates (software effects).

        Yields set(zone, color) with zone in z1/z2/z3; each call is validated.
        """
        if not self.allow_unverified_writes:
            raise DeviceError("hardware_writes_disabled_pending_verification")
        with self._lock:
            mouse = self._open(device_id)
            # rivalcfg waits 50 ms after each command by default; the Aerox 3
            # Wireless commands already wait for the mouse's reply, and 15 ms
            # keeps an animation smooth (measured 2026-11-10, no hangs observed).
            mouse.command_delay = 0.015
        def set_color(zone, color):
            self._validate("top_color", color)
            if zone not in ("z1", "z2", "z3"):
                raise ValueError("unknown zone")
            with self._lock:
                getattr(mouse, f"set_{zone}_color")(color)
        try:
            yield set_color
        finally:
            with self._lock:
                mouse.close()

    @staticmethod
    def _validate(setting, value):
        ranges = {"dpi": (100, 18000), "sleep_timer": (0, 20), "dim_timer": (0, 1200)}
        if setting in ranges:
            low, high = ranges[setting]
            if type(value) is not int or not low <= value <= high:
                raise ValueError("integer out of range")
            if setting == 'dpi' and value % 100:
                raise ValueError('DPI must be a multiple of 100')
        elif setting == "polling_rate":
            if type(value) is not int or value not in (125, 250, 500, 1000):
                raise ValueError("invalid polling rate")
        elif setting in ("top_color", "middle_color", "bottom_color"):
            if not isinstance(value, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
                raise ValueError("expected #RRGGBB")
        elif setting == "rainbow_effect":
            if value is not None:
                raise ValueError("rainbow effect takes no value")
        elif setting == "buttons_mapping":
            if value not in DPI_BUTTON_MAPPINGS.values():
                raise ValueError("unsupported button mapping")
        else:
            raise DeviceError("unsupported_setting")
        return "set_" + {"dpi": "sensitivity", "top_color": "z1_color",
                         "middle_color": "z2_color", "bottom_color": "z3_color"}.get(setting, setting)

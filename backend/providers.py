"""Device adapter boundary and explicitly selected simulation provider."""

from copy import deepcopy
from typing import Any, Protocol

from .domain import ApiError, Device

# Assignable MX Keys Mini keys (F-row positions 4-12 and the volume-up key next to del).
MX_KEYS = ("F4", "F5", "F6", "F7", "F8", "F9", "F10", "F11", "F12", "INS")
# Assignable G733 buttons: microphone button and a short press of the power button.
G733_BUTTONS = ("g1", "g2")


class DeviceProvider(Protocol):
    def devices(self) -> list[Device]: ...

    def apply(self, updates: dict[str, dict[str, Any]]) -> None:
        """Apply validated updates. Only simulation guarantees batch atomicity.

        Hardware adapters must report failures and expose observed state; they
        must never present transport acknowledgements as verified readback.
        """
        ...


class SimulatedProvider:
    def __init__(self) -> None:
        # These limits describe the simulator, not verified hardware support.
        self._devices = [
            Device("mx-keys-mini", "Logitech MX Keys Mini", "keyboard", "simulated", 78,
                   {"lighting": False, "dpi": False, "pollingRate": False, "backlight": True},
                   {"backlightMode": "auto", "backlightLevel": 4},
                   {"backlightLevel": {"min": 0, "max": 7, "step": 1}}),
            Device("g733", "Logitech G733", "headset", "simulated", 64,
                   {"lighting": True, "dpi": False, "pollingRate": False, "backlight": False},
                   {"lightingColor": "#8B5CF6", "lightingBrightness": 70},
                   {"lightingZones": ["top", "bottom"], "lightingEffects": ["static", "breathing", "rainbow"],
                    "lightingSpeedEffects": ["breathing", "rainbow"]}),
            Device("aerox-3", "SteelSeries Aerox 3 Wireless", "mouse", "simulated", 91,
                   {"lighting": True, "dpi": True, "pollingRate": True, "backlight": False},
                   {"lightingColor": "#22D3EE", "lightingBrightness": 80, "dpi": 800, "pollingRate": 1000},
                   {"dpi": {"min": 100, "max": 18000, "step": 100}, "pollingRates": [125, 250, 500, 1000],
                    "lightingZones": ["front", "middle", "rear"], "lightingEffects": ["static", "breathing", "rainbow"],
                    "lightingSpeedEffects": ["breathing"]}),
        ]

    def devices(self) -> list[Device]:
        return deepcopy(self._devices)

    def apply(self, updates: dict[str, dict[str, Any]]) -> None:
        devices = deepcopy(self._devices)
        for device in devices:
            device.settings.update(updates.get(device.id, {}))
        self._devices = devices


class HardwareProvider:
    """Translate the hardware-owned runtime without inventing capabilities."""

    def __init__(self, manager=None):
        if manager is None:
            from hardware.runtime import HardwareManager
            manager = HardwareManager()
        self.manager = manager

    @staticmethod
    def _device(raw: dict[str, Any]) -> Device:
        return Device(**{key: value for key, value in raw.items() if key in Device.__dataclass_fields__})

    def devices(self) -> list[Device]:
        return [self._device(raw) for raw in self.manager.enumerate_devices()]

    def apply(self, updates: dict[str, dict[str, Any]]) -> None:
        for device_id, settings in updates.items():
            if not settings:
                continue
            try:
                observed = self.manager.apply(device_id, settings)
            except ApiError:
                raise
            except (RuntimeError, OSError, ValueError) as exc:
                raise ApiError(409, "hardware_apply_failed", str(exc)) from exc
            if not isinstance(observed, dict):
                raise ApiError(409, "hardware_readback_unverified", "Hardware did not verify the requested settings by readback.")
            # 'acknowledged' is only reported for physically verified commands of a
            # device that cannot read settings back (see compatibility-report.json).
            if observed.get("verification") == "acknowledged":
                continue
            ignored = set(observed.get("ignored", ()))
            if any(observed.get("settings", {}).get(key) != value for key, value in settings.items() if key not in ignored):
                raise ApiError(409, "hardware_readback_unverified", "Hardware did not verify the requested settings by readback.")

    def assign_control(self, device_id: str, control: str, mode: str, *, wait: bool = True) -> None:
        """Route a device control natively ('default'), to nothing ('disabled') or to the app ('action').

        Without wait (restoring saved assignments) a sleeping keyboard is updated when it wakes up.
        """
        try:
            if (device_id, control) == ("aerox-3", "dpi"):
                self.manager.set_aerox_dpi_button({"default": "dpi", "disabled": "disabled", "action": "action"}[mode])
            elif device_id == "mx-keys-mini" and control in MX_KEYS:
                # A diverted key is reported only to the app; without an action it does nothing.
                self.manager.set_key_diversion(control, mode != "default", wait=wait)
            elif device_id == "g733" and control in G733_BUTTONS:
                self.manager.set_headset_button(control, mode != "default", wait=wait)
            else:
                raise ApiError(404, "not_found", "This control cannot be assigned.")
        except (RuntimeError, OSError, ValueError) as exc:
            raise ApiError(409, "hardware_apply_failed", str(exc)) from exc

    def watch_presence(self, on_change) -> None:
        """on_change(device_ids) is called when devices appear, return or change connection."""
        watch = getattr(self.manager, "watch_presence", None)
        if watch is not None:
            watch(on_change)

    def forget_readings(self) -> None:
        """The next read scans the devices instead of using cached readings."""
        forget = getattr(self.manager, "forget_readings", None)
        if forget is not None:
            forget()

    def watch_controls(self, on_control) -> None:
        """on_control(device_id, control) is called for presses of app-routed keys."""
        watch = getattr(self.manager, "watch_controls", None)
        if watch is not None:
            watch(on_control)

    def close(self) -> None:
        close = getattr(self.manager, "close", None)
        if close is not None:
            close()

"""Transport-independent device types and validation."""

from dataclasses import asdict, dataclass
import re
from typing import Any


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.details = details


@dataclass
class Device:
    id: str
    name: str
    kind: str
    connection: str
    batteryPercent: int | None
    capabilities: dict[str, bool]
    settings: dict[str, Any]
    limits: dict[str, Any]
    status: dict[str, Any] | None = None
    desiredSettings: dict[str, Any] | None = None
    telemetry: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items()
                if key not in {"status", "desiredSettings", "telemetry"} or value is not None}


CAPABILITY_OF = {"lightingColor": "lighting", "lightingBrightness": "lighting", "lightingZones": "lighting",
                 "lightingEffect": "lighting", "lightingSpeed": "lighting", "dpi": "dpi", "pollingRate": "pollingRate",
                 "backlightMode": "backlight", "backlightLevel": "backlight"}
BACKLIGHT_MODES = ("off", "auto", "manual")
LIGHTING_EFFECTS = ("static", "breathing", "rainbow")
LIGHTING_SPEEDS = ("slow", "normal", "fast")
COLOR = re.compile(r"#[0-9a-fA-F]{6}")


def validate_settings_shape(values: Any) -> dict[str, Any]:
    """Validate stored intent without assuming a currently connected device."""
    if not isinstance(values, dict):
        raise ApiError(400, "invalid_settings", "Settings must be a JSON object.")
    for key, value in values.items():
        if key not in CAPABILITY_OF:
            raise ApiError(400, "invalid_settings", f"Unknown setting: {key}.")
        if key == "lightingColor":
            if not isinstance(value, str) or not COLOR.fullmatch(value):
                raise ApiError(400, "invalid_settings", "Color must be #RRGGBB.")
        elif key == "lightingZones":
            if (not isinstance(value, dict) or not 0 < len(value) <= 8
                    or not all(isinstance(zone, str) and isinstance(color, str) and COLOR.fullmatch(color)
                               for zone, color in value.items())):
                raise ApiError(400, "invalid_settings", "Zone colors must map zone names to #RRGGBB.")
        elif key == "lightingEffect":
            if value not in LIGHTING_EFFECTS:
                raise ApiError(400, "invalid_settings", "Lighting effect must be static, breathing or rainbow.")
        elif key == "lightingSpeed":
            if value not in LIGHTING_SPEEDS:
                raise ApiError(400, "invalid_settings", "Effect speed must be slow, normal or fast.")
        elif key == "backlightMode":
            if value not in BACKLIGHT_MODES:
                raise ApiError(400, "invalid_settings", "Backlight mode must be off, auto or manual.")
        else:
            if type(value) is not int:
                raise ApiError(400, "invalid_settings", f"{key} must be an integer.")
            if key == "lightingBrightness" and not 0 <= value <= 100:
                raise ApiError(400, "invalid_settings", "Brightness must be between 0 and 100.")
            if key in {"dpi", "pollingRate"} and value <= 0:
                raise ApiError(400, "invalid_settings", f"{key} must be positive.")
            if key == "backlightLevel" and value < 0:
                raise ApiError(400, "invalid_settings", "backlightLevel must not be negative.")
    return dict(values)


def validate_settings(device: Device, values: Any) -> dict[str, Any]:
    validated = validate_settings_shape(values)
    for key, value in validated.items():
        if not device.capabilities.get(CAPABILITY_OF[key], False):
            raise ApiError(400, "unsupported_setting", f"{key} is unavailable for this device.")
        if key == "lightingZones" and not set(value) <= set(device.limits.get("lightingZones", ())):
            raise ApiError(400, "invalid_settings", "Unknown lighting zone for this device.")
        if key == "lightingEffect" and value not in device.limits.get("lightingEffects", ("static",)):
            raise ApiError(400, "unsupported_setting", "This lighting effect is unavailable for this device.")
        if key == "lightingSpeed" and not device.limits.get("lightingSpeedEffects"):
            raise ApiError(400, "unsupported_setting", "Effect speed is unavailable for this device.")
        if key == "backlightLevel":
            limits = device.limits.get("backlightLevel")
            if not limits or not limits["min"] <= value <= limits["max"]:
                raise ApiError(400, "invalid_settings", "Backlight level is outside the device limits.")
        if key in {"dpi", "pollingRate"}:
            if key == "dpi":
                limits = device.limits["dpi"]
                if not limits["min"] <= value <= limits["max"] or (value - limits["min"]) % limits["step"]:
                    raise ApiError(400, "invalid_settings", "DPI is outside the device adapter limits.")
            if key == "pollingRate" and value not in device.limits["pollingRates"]:
                raise ApiError(400, "invalid_settings", "Polling rate is outside the device adapter limits.")
    return validated

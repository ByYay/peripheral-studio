"""Device button assignments: simple user actions bound to specific device controls.

The desktop host supplies the action runner (automation.windows.WindowsAutomation)
and a native file picker; simulation mode stores assignments without running them.
Device-side routing (the Aerox DPI button sending F23, MX Keys keys diverted to the app)
is applied through the provider; actions are executed only for controls that have an assignment.
"""

import json
import threading
from typing import Any
from urllib.parse import urlsplit

from automation.engine import Action, local_file

from .domain import ApiError
from .providers import G733_BUTTONS, MX_KEYS


MAX_STORE_BYTES = 256 * 1024
# Assignable controls per device and the runner trigger each one produces.
CONTROLS = {"g733": {button: f"device:g733:{button}" for button in G733_BUTTONS}, "mx-keys-mini": {key: f"device:mx-keys-mini:{key}" for key in MX_KEYS},
            "aerox-3": {"dpi": "key:F23"}}
MEDIA = {"play_pause": "MEDIA_PLAY_PAUSE", "next": "MEDIA_NEXT", "previous": "MEDIA_PREV",
         "volume_up": "VOLUME_UP", "volume_down": "VOLUME_DOWN", "mute": "VOLUME_MUTE"}
SHORTCUTS = {"copy": ("LCTRL", "C"), "paste": ("LCTRL", "V"), "undo": ("LCTRL", "Z"), "redo": ("LCTRL", "Y"),
             "screenshot": ("LWIN", "LSHIFT", "S"), "show_desktop": ("LWIN", "D"), "task_view": ("LWIN", "TAB"),
             "task_manager": ("LCTRL", "LSHIFT", "ESC")}
FIELDS = {"default": set(), "disabled": set(), "open": {"path"}, "url": {"url"}, "media": {"key"}, "shortcut": {"preset"}}


def validate_action(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict) or raw.get("type") not in FIELDS or set(raw) != {"type"} | FIELDS[raw["type"]]:
        raise ApiError(400, "invalid_action", "Unsupported action.")
    action = dict(raw)
    kind = action["type"]
    if kind == "open":
        if not isinstance(action["path"], str) or len(action["path"]) > 1024:
            raise ApiError(400, "invalid_action", "Choose a file to open.")
        try:
            action["path"] = str(local_file(action["path"]))
        except (OSError, ValueError):
            raise ApiError(400, "invalid_action", "The selected file is not available.") from None
    elif kind == "url":
        url = action["url"]
        parsed = urlsplit(url) if isinstance(url, str) else None
        if (parsed is None or len(url) > 2048 or parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or any(c.isspace() or ord(c) < 32 for c in url)):
            raise ApiError(400, "invalid_action", "Enter a web address starting with http:// or https://.")
    elif kind == "media" and action["key"] not in MEDIA:
        raise ApiError(400, "invalid_action", "Unsupported media key.")
    elif kind == "shortcut" and action["preset"] not in SHORTCUTS:
        raise ApiError(400, "invalid_action", "Unsupported shortcut.")
    return action


def runner_action(action: dict[str, str]) -> Action | None:
    kind = action["type"]
    if kind == "open":
        return Action("open", target=action["path"])
    if kind == "url":
        return Action("url", target=action["url"])
    if kind == "media":
        return Action("shortcut", keys=(MEDIA[action["key"]],))
    if kind == "shortcut":
        return Action("shortcut", keys=SHORTCUTS[action["preset"]])
    return None  # default and disabled are handled on the device itself


def device_mode(action: dict[str, str]) -> str:
    return action["type"] if action["type"] in {"default", "disabled"} else "action"


class AutomationCoordinator:
    def __init__(self, service, automation, path, pick_file=None):
        self._service = service
        self._automation = automation
        self._path = path
        self._pick_file = pick_file
        self._lock = threading.RLock()
        self._assignments: dict[str, dict[str, dict[str, str]]] = {}
        self._error: str | None = None
        watch = getattr(service.provider, "watch_controls", None)
        if automation is not None and watch is not None:
            watch(self._on_control)
        self._restore()

    def _restore(self) -> None:
        with self._service._profile_store_lock(self._path):
            if not self._path.exists():
                return
            try:
                if self._path.stat().st_size > MAX_STORE_BYTES:
                    raise ValueError("Assignment store exceeds 256 KiB.")
                stored = json.loads(self._path.read_text(encoding="utf-8"))
                if not isinstance(stored, dict):
                    raise ValueError("Invalid assignment store.")
                assignments = {}
                for device_id, controls in stored.items():
                    if device_id not in CONTROLS or not isinstance(controls, dict):
                        raise ValueError("Invalid assignment store.")
                    for control, action in controls.items():
                        if control not in CONTROLS[device_id]:
                            raise ValueError("Invalid assignment store.")
                        assignments.setdefault(device_id, {})[control] = dict(action)
            except (ValueError, TypeError) as exc:
                raise ValueError(f"Invalid assignment store at {self._path}; preserve or repair it before restarting.") from exc
        # Files may have moved since; keep such assignments visible but inactive.
        for controls in assignments.values():
            for control, action in controls.items():
                try:
                    controls[control] = validate_action(action)
                except ApiError as exc:
                    self._error = f"{control}: {exc}"
        self._assignments = assignments
        self._configure_runner()
        if self._automation is not None:
            # Device routing is volatile (lost when a device powers off); re-apply in the background.
            threading.Thread(target=self._apply_all_devices, name="assignment-restore", daemon=True).start()

    def _apply_all_devices(self) -> None:
        for device_id in list(self._assignments):
            self.reapply(device_id)

    def reapply(self, device_id: str) -> None:
        """Send a device's routing again, e.g. after it powered off and came back."""
        if self._automation is None:
            return
        for control, action in list(self._assignments.get(device_id, {}).items()):
            try:
                self._apply_device(device_id, control, action, wait=False)
            except (ApiError, OSError, RuntimeError, ValueError) as exc:
                self._error = f"{control}: {exc}"

    def _apply_device(self, device_id: str, control: str, action: dict[str, str], **options) -> None:
        assign = getattr(self._service.provider, "assign_control", None)
        if assign is not None:
            assign(device_id, control, device_mode(action), **options)

    def _on_control(self, device_id: str, control: str) -> None:
        """A device reported a press of an app-routed control (e.g. a diverted MX Keys key)."""
        action = self._assignments.get(device_id, {}).get(control)
        if action is None or runner_action(action) is None or self._automation.status().get("busy"):
            return  # "disabled" keys do nothing; presses during a running action are dropped
        try:
            self._automation.execute(CONTROLS[device_id][control])
        except (PermissionError, RuntimeError, ValueError) as exc:
            self._error = f"{control}: {exc}"

    def _bindings(self) -> dict[str, Action]:
        bindings = {}
        for device_id, controls in self._assignments.items():
            for control, action in controls.items():
                runnable = runner_action(action)
                if runnable is not None:
                    bindings[CONTROLS[device_id][control]] = runnable
        return bindings

    def _configure_runner(self) -> None:
        if self._automation is None:
            return
        bindings = self._bindings()
        try:
            self._automation.configure("devices" if bindings else None, bindings)
            if bindings:
                self._automation.enable()
            else:
                self._automation.disable()
        except (OSError, RuntimeError, ValueError) as exc:
            self._error = str(exc)

    def state(self) -> dict[str, Any]:
        with self._lock:
            status = self._automation.status() if self._automation is not None else {}
            return {"available": self._automation is not None, "error": self._error or status.get("error"),
                    "filePicker": self._pick_file is not None,
                    "media": list(MEDIA), "shortcuts": list(SHORTCUTS),
                    "devices": {device_id: {"controls": list(controls),
                                            "assignments": {control: dict(action) for control, action
                                                            in self._assignments.get(device_id, {}).items()}}
                                for device_id, controls in CONTROLS.items()}}

    def save(self, device_id: str, control: str, body: Any) -> dict[str, Any]:
        if control not in CONTROLS.get(device_id, {}):
            raise ApiError(404, "not_found", "This control cannot be assigned.")
        action = validate_action(body)
        with self._lock, self._service._profile_store_lock(self._path):
            if self._automation is not None:
                try:
                    self._apply_device(device_id, control, action)
                except ApiError:
                    raise
                except (OSError, RuntimeError, ValueError) as exc:
                    raise ApiError(409, "hardware_apply_failed", str(exc)) from None
            assignments = {key: dict(value) for key, value in self._assignments.items()}
            if action["type"] == "default":
                assignments.get(device_id, {}).pop(control, None)
            else:
                assignments.setdefault(device_id, {})[control] = action
            self._service._save({key: value for key, value in assignments.items() if value}, self._path)
            self._assignments, self._error = assignments, None
            self._configure_runner()
        return self.state()

    def clear(self) -> None:
        """Delete every assignment; assigned controls get their own function back."""
        with self._lock, self._service._profile_store_lock(self._path):
            errors = []
            if self._automation is not None:
                for device_id, controls in self._assignments.items():
                    for control in controls:
                        try:
                            # Without wait a sleeping keyboard gets its keys back when it wakes up.
                            self._apply_device(device_id, control, {"type": "default"}, wait=False)
                        except (ApiError, OSError, RuntimeError, ValueError) as exc:
                            errors.append(f"{control}: {exc}")
            self._path.unlink(missing_ok=True)
            self._assignments, self._error = {}, errors[0] if errors else None
            self._configure_runner()

    def pick_file(self) -> dict[str, Any]:
        if self._pick_file is None:
            raise ApiError(409, "file_picker_unavailable", "Choosing files is available only in the desktop app.")
        path = self._pick_file()
        return {"path": path if isinstance(path, str) and path else None}

    def close(self) -> None:
        if self._automation is not None:
            self._automation.close()

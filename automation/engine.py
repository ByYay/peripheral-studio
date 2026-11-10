from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit


MAX_EVENTS = 1000
MAX_DURATION = 60.0


@dataclass(frozen=True)
class InputEvent:
    kind: str  # key_down, key_up, button_down, button_up, wait
    value: str = ""
    delay: float = 0.0

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or not isinstance(self.value, str):
            raise ValueError("Event identifiers must be strings")
        if isinstance(self.delay, bool) or not isinstance(self.delay, (int, float)):
            raise ValueError("Delay must be a number")


@dataclass(frozen=True)
class Action:
    kind: str  # shortcut, macro, application, url, script, profile
    target: str = ""
    keys: tuple[str, ...] = ()
    events: tuple[InputEvent, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or not isinstance(self.target, str):
            raise ValueError("Action identifiers must be strings")
        if not isinstance(self.keys, tuple) or any(not isinstance(k, str) for k in self.keys):
            raise ValueError("Keys must be an immutable tuple of strings")
        if not isinstance(self.events, tuple) or any(not isinstance(e, InputEvent) for e in self.events):
            raise ValueError("Events must be an immutable tuple of InputEvent")

    @classmethod
    def from_dict(cls, raw: object) -> Action:
        if not isinstance(raw, dict) or not set(raw) <= {"kind", "target", "keys", "events"}:
            raise ValueError("Invalid action object")
        keys, events = raw.get("keys", []), raw.get("events", [])
        if not isinstance(keys, list) or len(keys) > 8 or not isinstance(events, list) or len(events) > MAX_EVENTS:
            raise ValueError("Invalid action lists")
        try:
            return cls(raw.get("kind"), raw.get("target", ""), tuple(keys),
                       tuple(InputEvent(**event) for event in events))
        except TypeError as exc:
            raise ValueError("Invalid action event") from exc


def validate_events(events: tuple[InputEvent, ...]) -> None:
    if not events or len(events) > MAX_EVENTS:
        raise ValueError("Macro must contain 1..1000 events")
    held: set[tuple[str, str]] = set()
    duration = 0.0
    for event in events:
        if not 0 <= event.delay <= MAX_DURATION:
            raise ValueError("Invalid event delay")
        duration += event.delay
        if event.kind == "wait":
            if event.value:
                raise ValueError("Wait events cannot have a value")
            continue
        if event.kind not in {"key_down", "key_up", "button_down", "button_up"}:
            raise ValueError("Unsupported input event")
        if not event.value or len(event.value) > 64:
            raise ValueError("Invalid input identifier")
        family, direction = event.kind.split("_")
        identity = (family, event.value)
        if direction == "down":
            if identity in held:
                raise ValueError("Repeated press without release")
            held.add(identity)
        else:
            if identity not in held:
                raise ValueError("Release without press")
            held.remove(identity)
    if held or duration > MAX_DURATION:
        raise ValueError("Macro has held inputs or exceeds 60 seconds")


def local_file(target: str, suffix: str | None = None) -> Path:
    # Reject UNC, device paths, ADS and relative paths before resolving.
    path = Path(target)
    if (not path.is_absolute() or target.startswith(("\\\\", "//"))
            or ":" in target[2:] or "\x00" in target):
        raise ValueError("An absolute local file is required")
    path = path.resolve(strict=True)
    if (not path.is_file() or (suffix is not None and path.suffix.lower() != suffix)
            or str(path).startswith(("\\\\", "//"))):
        raise ValueError(f"Expected a local {suffix} file" if suffix else "Expected a local file")
    return path


class Recorder:
    """Explicit recording session; feed physical events only, never injected events.

    Frontend must show recording state and provide a stop control. No global
    keyboard hook or background capture is installed by this module.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self.active = False
        self.events: list[InputEvent] = []

    def start(self) -> None:
        if self.active:
            raise RuntimeError("Already recording")
        self.events = []
        self.started = self.previous = self.clock()
        self.active = True

    def feed(self, kind: str, value: str, *, injected: bool = False) -> None:
        if not self.active:
            raise RuntimeError("Recording is inactive")
        if injected:
            return
        now = self.clock()
        if len(self.events) >= MAX_EVENTS or now - self.started > MAX_DURATION:
            self.cancel()
            raise ValueError("Recording limit exceeded; recording discarded")
        if kind not in {"key_down", "key_up", "button_down", "button_up"} or not value or len(value) > 64:
            raise ValueError("Invalid physical input")
        self.events.append(InputEvent(kind, value, now - self.previous))
        self.previous = now

    def stop(self) -> tuple[InputEvent, ...]:
        if not self.active:
            raise RuntimeError("Recording is inactive")
        events = tuple(self.events)
        self.cancel()
        validate_events(events)
        return events

    def cancel(self) -> None:
        self.active = False
        self.events = []


@dataclass(frozen=True)
class PlaybackPlan:
    action: Action
    events: tuple[InputEvent, ...] = ()
    execution_enabled: bool = False
    reason: str = "MVP planning only; execution requires a trusted host implementation"


class Engine:
    """Planning-only editor contract; never installs hooks or executes actions.

    The backend owns profile IDs, activation and persistence. Call switch() when
    the backend confirms activation. Device Engineer supplies verified assignable
    controls and supported input identifiers; empty sets enable no assignments.
    Frontend must show recording state, conflicts and disabled execution status.
    No approval can be granted through imported action data.
    """

    def __init__(self, controls: set[str], supported_keys: set[str], supported_buttons: set[str]):
        self.controls = frozenset(controls)
        self.supported_keys = frozenset(supported_keys)
        self.supported_buttons = frozenset(supported_buttons)
        self.bindings: dict[str, dict[str, Action]] = {}
        self.active: str | None = None

    def validate_action(self, action: Action) -> None:
        if not isinstance(action, Action):
            raise ValueError("Expected an Action")
        if action.kind == "macro":
            if action.target or action.keys:
                raise ValueError("Macro accepts only events")
            validate_events(action.events)
            for event in action.events:
                allowed = self.supported_keys if event.kind.startswith("key_") else self.supported_buttons
                if event.kind != "wait" and event.value not in allowed:
                    raise ValueError("Unsupported input")
        elif action.kind in {"shortcut", "remap"}:
            if action.kind == "remap" and action.target:
                if action.keys or action.events or not action.target.startswith("button:") or action.target[7:] not in self.supported_buttons:
                    raise ValueError("Remap requires one key or one button target")
                return
            if action.target or action.events:
                raise ValueError("Shortcut accepts only keys")
            if action.kind == "remap" and len(action.keys) != 1:
                raise ValueError("Remap requires exactly one key")
            if not 1 <= len(action.keys) <= 8 or len(set(action.keys)) != len(action.keys):
                raise ValueError("Shortcut requires 1..8 distinct keys")
            if any(key not in self.supported_keys for key in action.keys):
                raise ValueError("Unsupported key")
        elif action.kind in {"url", "application", "script", "profile", "open"}:
            if action.keys or action.events or not isinstance(action.target, str):
                raise ValueError("Action accepts only a target")
            if action.kind == "url":
                parsed = urlsplit(action.target)
                if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or any(c.isspace() or ord(c) < 32 for c in action.target):
                    raise ValueError("Only HTTP(S) URLs without credentials are supported")
                try:
                    parsed.port
                except ValueError as exc:
                    raise ValueError("Invalid URL port") from exc
            elif action.kind in {"application", "script"}:
                local_file(action.target, ".exe" if action.kind == "application" else ".py")
            elif action.kind == "open":
                local_file(action.target)  # any local file, opened like a double-click
            elif not action.target.strip() or len(action.target) > 100:
                raise ValueError("Invalid backend profile ID")
        else:
            raise ValueError("Unsupported action")

    def assign(self, profile: str, control: str, action: Action, *, replace: bool = False) -> None:
        self._profile_id(profile)
        if control not in self.controls:
            raise ValueError("Device does not expose this assignable control")
        self.validate_action(action)
        bindings = self.bindings.get(profile, {})
        if control in bindings and not replace:
            raise ValueError("Binding conflict; explicit replacement required")
        self.bindings.setdefault(profile, {})[control] = action

    def unassign(self, profile: str, control: str) -> None:
        self.bindings.get(profile, {}).pop(control, None)

    def conflicts(self, profile: str, control: str, action: Action) -> list[str]:
        result = []
        if control not in self.controls:
            result.append("unsupported_control")
        bindings = self.bindings.get(profile, {})
        if control in bindings:
            result.append("control_already_assigned")
        if action in bindings.values():
            result.append("duplicate_action")  # warning; duplicates may be intentional
        return result

    @staticmethod
    def _profile_id(profile: str) -> None:
        if not isinstance(profile, str) or not profile.strip() or len(profile) > 100:
            raise ValueError("Invalid backend profile ID")

    def switch(self, profile: str) -> None:
        """Reflect backend-confirmed activation, without mutating backend state."""
        self._profile_id(profile)
        self.active = profile

    def plan(self, control: str, *, injected: bool = False) -> PlaybackPlan | None:
        if injected or self.active is None:
            return None
        action = self.bindings.get(self.active, {}).get(control)
        if action is None:
            return None
        self.validate_action(action)
        events = action.events
        if action.kind == "shortcut":
            events = tuple(InputEvent("key_down", key) for key in action.keys) + tuple(InputEvent("key_up", key) for key in reversed(action.keys))
        return PlaybackPlan(action, events)

    def trigger(self, control: str) -> None:
        raise PermissionError("Action execution is disabled in the MVP")

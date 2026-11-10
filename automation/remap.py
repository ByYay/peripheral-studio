"""Paired, synchronous global remap state for a Windows hook thread."""

from .engine import InputEvent
from .win32 import MODIFIERS


class Remapper:
    def __init__(self, inputs):
        self.inputs = inputs
        self.held = {}

    @staticmethod
    def target(action):
        if action.keys:
            return "key", action.keys[0]
        return "button", action.target.removeprefix("button:")

    @classmethod
    def validate(cls, control, action):
        family, value = cls.target(action)
        if value in MODIFIERS or value == "F12":
            raise ValueError("Modifier keys and emergency stop cannot be remap targets")
        if family == "button" and value not in {"middle", "x1", "x2"}:
            raise ValueError("Remapping primary mouse buttons is disabled")
        if control == f"{family}:{value}":
            raise ValueError("Remap source and target must differ")

    def receive(self, control, action, down):
        """Return True only for a successfully remapped source sequence."""
        if control in self.held:
            event = self.held[control]
            if down:
                return True  # consume OS auto-repeat without duplicate target presses
            self.inputs.emit(InputEvent(event.kind.replace("_down", "_up"), event.value))
            del self.held[control]
            return True
        if not down or action is None:
            return False
        family, value = self.target(action)
        event = InputEvent(f"{family}_down", value)
        if self.inputs.modifiers_down() or self.inputs.input_down(event):
            return False
        if any(item == event for item in self.held.values()):
            return False
        self.inputs.emit(event)
        self.held[control] = event
        return True

    def release(self):
        errors = []
        for control, event in tuple(self.held.items()):
            try:
                self.inputs.emit(InputEvent(event.kind.replace("_down", "_up"), event.value))
                del self.held[control]
            except OSError as exc:
                errors.append(str(exc))
        if errors:
            raise OSError("Failed to release remapped inputs: " + "; ".join(errors))

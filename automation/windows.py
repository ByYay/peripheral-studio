"""Opt-in global Windows input actions and foreground-only macro recording."""

import hashlib
import os
import subprocess
import sys
from threading import Event, Lock, Thread, Timer

from .engine import Action, Engine, InputEvent, MAX_DURATION, Recorder, local_file
from .win32 import BUTTONS, CONTROL_IDS, KEYS, Win32Input
from .remap import Remapper


MAX_SCRIPT_BYTES = 64 * 1024
# Triggers reported by device listeners (diverted MX Keys keys, G733 buttons; see backend CONTROLS);
# the Windows hooks never produce them, they only reach the runner through execute().
DEVICE_CONTROLS = frozenset([f"device:mx-keys-mini:{key}" for key in ("F4", "F5", "F6", "F7", "F8", "F9", "F10", "F11", "F12", "INS")]
                            + ["device:g733:g1", "device:g733:g2"])


class WindowsAutomation:
    """Trusted desktop service. F12 stops input actions and recording.

    Hook triggers run on release and preserve the original Windows input. They
    cannot identify the originating hardware. No hooks start on construction.
    """

    controls = CONTROL_IDS | DEVICE_CONTROLS
    keys = frozenset(KEYS)
    buttons = frozenset(BUTTONS)

    def __init__(self, open_url, activate_profile=None, request_script_approval=None, *, python_executable=None):
        self._input = Win32Input()
        self._remapper = Remapper(self._input)
        self._open_url = open_url
        self._activate_profile = activate_profile
        self._request_script_approval = request_script_approval
        self._python = python_executable or (None if getattr(sys, "frozen", False) else sys.executable)
        self._engine = self._new_engine()
        self._state = Lock()
        self._lifecycle = Lock()
        self._enabled = False
        self._closed = False
        self._worker = None
        self._cancel = Event()
        self._error = None
        self._approved = set()
        self._recorder = Recorder()
        self._record_window = None
        self._timer = None
        self._pressed = set()
        self._emergency_stop = False

    def _new_engine(self):
        return Engine(self.controls, self.keys, self.buttons)

    def _local(self, target, suffix):
        path = local_file(target, suffix)
        if not self._input.fixed_drive(path):
            raise ValueError("Select a file on a fixed local drive")
        return path

    def _validated_engine(self, profile_id, bindings):
        if not isinstance(bindings, dict) or (profile_id is None and bindings):
            raise ValueError("Bindings require an active profile")
        engine = self._new_engine()
        if profile_id is not None:
            engine.switch(profile_id)
        for control, action in bindings.items():
            engine.assign(profile_id, control, action)
            if action.kind == "remap":
                Remapper.validate(control, action)
            if action.kind in {"script", "application"}:
                self._local(action.target, ".py" if action.kind == "script" else ".exe")
            if action.kind == "open":
                self._local(action.target, None)
        return engine

    def validate_bindings(self, profile_id, bindings):
        self._validated_engine(profile_id, bindings)

    def configure(self, profile_id, bindings):
        engine = self._validated_engine(profile_id, bindings)
        with self._lifecycle:
            with self._state:
                if self._closed:
                    raise RuntimeError("Automation is closed")
                self._deactivate()
                self._engine = engine
                self._emergency_stop = any(action.kind == "remap" for action in bindings.values())
            self._input.stop()

    def enable(self):
        with self._lifecycle:
            with self._state:
                if self._closed:
                    raise RuntimeError("Automation is closed")
                if self._enabled and self._input.running:
                    return
                if self._worker is not None:
                    raise RuntimeError("Previous action is still stopping")
                self._deactivate()
            self._input.stop()
            self._input.start(self._receive)
            with self._state:
                self._enabled = True
                self._error = None

    def _deactivate(self):
        self._enabled = False
        self._cancel.set()
        self._approved.clear()
        self._pressed.clear()
        self._cancel_recording()
        try:
            self._remapper.release()
        except OSError as exc:
            self._error = str(exc)

    def refresh_hooks(self):
        """After the computer wakes up: install the hooks again; Windows may have dropped them."""
        with self._state:
            if not self._enabled or self._closed:
                return
            running = self._input.running
        if running:
            self._input.rehook()
        else:
            self.enable()  # the hook thread ended (e.g. an error on resume): start it again

    def disable(self):
        with self._lifecycle:
            with self._state:
                self._deactivate()
            self._input.stop()

    def close(self):
        with self._lifecycle:
            with self._state:
                self._closed = True
                self._deactivate()
                worker = self._worker
            self._input.stop()
        # The native approval callback may be waiting on the UI thread. Do
        # not join from that thread indefinitely. Cancellation prevents launch.
        if worker is not None:
            worker.join(1)

    def status(self):
        with self._state:
            return {"enabled": self._enabled and self._input.running,
                    "recording": self._recorder.active, "busy": self._worker is not None,
                    "error": self._error, "scope": "windows-global",
                    "suppressesOriginal": any(a.kind == "remap" for a in self._engine.bindings.get(self._engine.active, {}).values()),
                    "emergencyStop": "F12", "scriptAvailable": bool(self._python and self._request_script_approval)}

    def start_recording(self):
        with self._state:
            self._require_enabled()
            if self._worker is not None:
                raise RuntimeError("An action is running")
            window = self._input.foreground()
            if not window or not self._input.owned_window(window):
                raise RuntimeError("Focus the app window before recording")
            self._recorder.start()
            self._record_window = window
            timer = Timer(MAX_DURATION, self._record_timeout)
            timer.daemon = True
            self._timer = timer
            timer.start()

    def _record_timeout(self):
        with self._state:
            from threading import current_thread
            if self._timer is current_thread():
                self._cancel_recording()
                self._error = "Recording exceeded 60 seconds and was discarded"

    def stop_recording(self):
        with self._state:
            try:
                if self._recorder.active and self._record_window != self._input.foreground():
                    raise RuntimeError("Recording window lost focus")
                return self._recorder.stop()
            finally:
                self._cancel_recording()

    def _cancel_recording(self):
        self._recorder.cancel()
        self._record_window = None
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def cancel_recording(self):
        with self._state:
            self._cancel_recording()

    def _require_enabled(self):
        if not self._enabled or not self._input.running:
            raise PermissionError("Enable Windows automation first")

    def _receive(self, family, value, down):
        # F12 is an emergency stop only while remaps suppress real input; plain
        # device-button actions must not be switched off by ordinary F12 use.
        emergency = family == "key" and value == "F12" and down and self._emergency_stop
        # Never wait on the state lock from a low-level hook callback.
        if emergency:
            self._enabled = False
            self._cancel.set()
            self._input.request_stop()
        if not self._state.acquire(blocking=False):
            if emergency or f"{family}:{value}" in self._remapper.held:
                Thread(target=self.disable, daemon=True).start()
            return False
        try:
            control = f"{family}:{value}"
            if emergency:
                self._deactivate()
                self._input.request_stop()
                return
            if not self._enabled:
                return
            action = self._engine.bindings.get(self._engine.active, {}).get(control)
            if not self._recorder.active and (control in self._remapper.held or action is not None and action.kind == "remap"):
                return self._remapper.receive(control, action, down)
            was_pressed = control in self._pressed
            if down:
                self._pressed.add(control)
            else:
                self._pressed.discard(control)
            if self._recorder.active:
                if self._record_window != self._input.foreground():
                    self._cancel_recording()
                    self._error = "Recording window lost focus"
                elif down != was_pressed and value != "F12":
                    self._recorder.feed(f"{family}_{'down' if down else 'up'}", value)
                return
            if not down and was_pressed and not self._input.modifiers_down():
                if control in self._engine.bindings.get(self._engine.active, {}):
                    self._queue(control)
        except Exception as exc:
            if self._recorder.active:
                self._cancel_recording()
            self._error = str(exc)
            if self._remapper.held:
                self._enabled = False
                self._cancel.set()
                Thread(target=self.disable, daemon=True).start()
        finally:
            self._state.release()

    def execute(self, control):
        with self._state:
            self._require_enabled()
            self._queue(control)

    def _queue(self, control):
        if self._worker is not None or self._recorder.active:
            raise RuntimeError("Automation is busy")
        profile = self._engine.active
        action = self._engine.bindings.get(profile, {}).get(control)
        if action is None:
            raise ValueError("No saved assignment for this control")
        if action.kind == "remap":
            raise ValueError("Remaps operate only on physical press/release events")
        self._error = None
        self._cancel = Event()
        window = self._input.foreground()
        worker = Thread(target=self._run, args=(self._engine, profile, control, action, window, self._cancel), daemon=True)
        self._worker = worker
        try:
            worker.start()
        except RuntimeError:
            self._worker = None
            raise

    def _run(self, engine, profile, control, action, window, cancelled):
        try:
            plan = engine.plan(control)
            if cancelled.is_set():
                return
            if plan.events:
                self._play(plan.events, window, cancelled)
            elif action.kind == "script":
                self._script(profile, control, action, cancelled)
            elif action.kind == "application":
                path = self._local(action.target, ".exe")
                with self._state:
                    if not cancelled.is_set():
                        subprocess.Popen([str(path)], executable=str(path), cwd=path.parent, shell=False)
            elif action.kind == "open":
                path = self._local(action.target, None)
                with self._state:
                    if not cancelled.is_set():
                        os.startfile(str(path))  # Same as a double-click in Explorer.
            elif action.kind == "url":
                if not cancelled.is_set():
                    self._open_url(action.target)
            elif action.kind == "profile":
                if self._activate_profile is None:
                    raise RuntimeError("Profile activation is unavailable")
                if not cancelled.is_set():
                    self._activate_profile(action.target)
        except Exception as exc:
            with self._state:
                self._error = str(exc)
        finally:
            with self._state:
                self._worker = None

    def _play(self, events, window, cancelled):
        held = []
        release_errors = []
        try:
            if not window or self._input.modifiers_down():
                raise RuntimeError("Playback requires a foreground window and released modifiers")
            for event in events:
                if cancelled.wait(event.delay):
                    return
                if self._input.foreground() != window:
                    raise RuntimeError("Playback stopped because the active window changed")
                if event.kind == "wait":
                    continue
                if event.kind.endswith("_down"):
                    if self._input.input_down(event):
                        raise RuntimeError("Playback stopped because an input is already held")
                    self._input.emit(event)
                    held.append(event)
                else:
                    self._input.emit(event)
                    held.remove(next(item for item in held if item.value == event.value and item.kind[:-4] == event.kind[:-2]))
        finally:
            for event in reversed(held):
                try:
                    self._input.emit(InputEvent(event.kind.replace("_down", "_up"), event.value))
                except OSError as exc:
                    release_errors.append(str(exc))
            if release_errors:
                raise OSError("Could not release playback inputs: " + "; ".join(release_errors))

    def _script(self, profile, control, action, cancelled):
        if self._python is None or self._request_script_approval is None:
            raise PermissionError("Script execution requires a local Python interpreter and native approval")
        path = self._local(action.target, ".py")
        interpreter = self._local(str(self._python), ".exe")
        with path.open("rb") as source:
            content = source.read(MAX_SCRIPT_BYTES + 1)
        if len(content) > MAX_SCRIPT_BYTES:
            raise ValueError("Python scripts are limited to 64 KiB")
        # A single unambiguous encoding is used both for review and execution.
        source_text = content.decode("utf-8-sig")
        compile(source_text, str(path), "exec")
        digest = hashlib.sha256(content).hexdigest()
        identity = (profile, control, str(path), digest)
        with self._state:
            approved = identity in self._approved
        if not approved:
            review = {"path": str(path), "sha256": digest, "content": source_text}
            if cancelled.is_set() or self._request_script_approval(review) is not True:
                raise PermissionError("Script execution declined")
        with self._state:
            if cancelled.is_set():
                return
            self._approved.add(identity)
            process = subprocess.Popen(
                [str(interpreter), "-I", "-S", "-X", "utf8", "-c",
                 "import sys; exec(compile(sys.stdin.buffer.read().decode('utf-8'), sys.argv[1], 'exec'))", str(path)],
                executable=str(interpreter), cwd=interpreter.parent, shell=False,
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        data = source_text.encode("utf-8")
        try:
            for _ in range(600):
                if cancelled.is_set():
                    return
                try:
                    process.communicate(input=data, timeout=0.1)
                except subprocess.TimeoutExpired:
                    data = None  # communicate retains the original bytes across timeouts.
                    continue
                if process.returncode:
                    raise RuntimeError(f"Python script exited with code {process.returncode}")
                return
            raise TimeoutError("Python script exceeded 60 seconds")
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()

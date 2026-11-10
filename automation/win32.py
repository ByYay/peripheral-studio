"""Thin Win32 input adapter. Importing this module installs no hooks."""

import ctypes as c
from ctypes import wintypes as w
import os
from threading import Event, Thread

from .engine import InputEvent


KEYS = {chr(vk): vk for vk in (*range(0x30, 0x3A), *range(0x41, 0x5B))}
KEYS.update({f"F{i}": 0x6F + i for i in range(1, 25)})
KEYS.update(ENTER=0x0D, ESC=0x1B, TAB=9, SPACE=0x20, BACKSPACE=8,
            LEFT=0x25, UP=0x26, RIGHT=0x27, DOWN=0x28, DELETE=0x2E,
            LSHIFT=0xA0, RSHIFT=0xA1, LCTRL=0xA2, RCTRL=0xA3,
            LALT=0xA4, RALT=0xA5, LWIN=0x5B, RWIN=0x5C,
            VOLUME_MUTE=0xAD, VOLUME_DOWN=0xAE, VOLUME_UP=0xAF,
            MEDIA_NEXT=0xB0, MEDIA_PREV=0xB1, MEDIA_PLAY_PAUSE=0xB3)
MEDIA_KEYS = frozenset({"VOLUME_MUTE", "VOLUME_DOWN", "VOLUME_UP", "MEDIA_NEXT", "MEDIA_PREV", "MEDIA_PLAY_PAUSE"})
BUTTONS = {"left": (2, 4, 0), "right": (8, 16, 0), "middle": (32, 64, 0),
           "x1": (128, 256, 1), "x2": (128, 256, 2)}
MODIFIERS = frozenset({"LSHIFT", "RSHIFT", "LCTRL", "RCTRL", "LALT", "RALT", "LWIN", "RWIN"})
CONTROL_IDS = frozenset({f"key:{key}" for key in KEYS.keys() - MODIFIERS - {"F12"}}
                        | {"button:middle", "button:x1", "button:x2"})


class Keyboard(c.Structure):
    _fields_ = [("vk", w.WORD), ("scan", w.WORD), ("flags", w.DWORD),
                ("time", w.DWORD), ("extra", c.c_size_t)]


class Mouse(c.Structure):
    _fields_ = [("x", w.LONG), ("y", w.LONG), ("data", w.DWORD),
                ("flags", w.DWORD), ("time", w.DWORD), ("extra", c.c_size_t)]


class InputData(c.Union):
    _fields_ = [("keyboard", Keyboard), ("mouse", Mouse)]


class NativeInput(c.Structure):
    _anonymous_ = ("data",)
    _fields_ = [("type", w.DWORD), ("data", InputData)]


class KeyHook(c.Structure):
    _fields_ = [("vk", w.DWORD), ("scan", w.DWORD), ("flags", w.DWORD),
                ("time", w.DWORD), ("extra", c.c_size_t)]


class MouseHook(c.Structure):
    _fields_ = [("point", w.POINT), ("data", w.DWORD), ("flags", w.DWORD),
                ("time", w.DWORD), ("extra", c.c_size_t)]


REHOOK = 0x8001  # WM_APP + 1: ask the hook thread to install its hooks again


class Win32Input:
    def __init__(self):
        if os.name != "nt":
            raise OSError("Windows input is unavailable on this platform")
        self.user = c.WinDLL("user32", use_last_error=True)
        self.kernel = c.WinDLL("kernel32", use_last_error=True)
        self.callback_type = c.WINFUNCTYPE(c.c_ssize_t, c.c_int, w.WPARAM, w.LPARAM)
        for lib, name, result, args in (
            (self.user, "SendInput", w.UINT, (w.UINT, c.POINTER(NativeInput), c.c_int)),
            (self.user, "GetAsyncKeyState", c.c_short, (c.c_int,)),
            (self.user, "GetForegroundWindow", w.HWND, ()),
            (self.user, "GetWindowThreadProcessId", w.DWORD, (w.HWND, c.POINTER(w.DWORD))),
            (self.user, "SetWindowsHookExW", w.HANDLE, (c.c_int, self.callback_type, w.HMODULE, w.DWORD)),
            (self.user, "CallNextHookEx", c.c_ssize_t, (w.HANDLE, c.c_int, w.WPARAM, w.LPARAM)),
            (self.user, "UnhookWindowsHookEx", w.BOOL, (w.HANDLE,)),
            (self.user, "PeekMessageW", w.BOOL, (c.POINTER(w.MSG), w.HWND, w.UINT, w.UINT, w.UINT)),
            (self.user, "GetMessageW", c.c_int, (c.POINTER(w.MSG), w.HWND, w.UINT, w.UINT)),
            (self.user, "PostThreadMessageW", w.BOOL, (w.DWORD, w.UINT, w.WPARAM, w.LPARAM)),
            (self.kernel, "GetModuleHandleW", w.HMODULE, (w.LPCWSTR,)),
            (self.kernel, "GetCurrentThreadId", w.DWORD, ()),
            (self.kernel, "GetDriveTypeW", w.UINT, (w.LPCWSTR,)),
        ):
            function = getattr(lib, name)
            function.restype = result
            function.argtypes = args
        self._thread = None
        self.installs = 0  # hook installations, including reinstalls

    def foreground(self):
        return self.user.GetForegroundWindow()

    def owned_window(self, window):
        pid = w.DWORD()
        self.user.GetWindowThreadProcessId(window, c.byref(pid))
        return pid.value == os.getpid()

    def fixed_drive(self, path):
        return self.kernel.GetDriveTypeW(path.anchor) == 3

    def modifiers_down(self):
        return any(self.user.GetAsyncKeyState(KEYS[key]) < 0 for key in MODIFIERS)

    def input_down(self, event):
        mouse_vks = {"left": 1, "right": 2, "middle": 4, "x1": 5, "x2": 6}
        vk = KEYS[event.value] if event.kind.startswith("key_") else mouse_vks[event.value]
        return self.user.GetAsyncKeyState(vk) < 0

    def emit(self, event: InputEvent):
        up = event.kind.endswith("_up")
        packet = NativeInput()
        if event.kind.startswith("key_"):
            packet.type = 1
            extended = event.value in {"RIGHT", "LEFT", "UP", "DOWN", "DELETE", "RCTRL", "RALT", "LWIN", "RWIN"} | MEDIA_KEYS
            packet.keyboard = Keyboard(KEYS[event.value], 0, int(extended) | (2 if up else 0), 0, 0)
        else:
            down_flag, up_flag, data = BUTTONS[event.value]
            packet.mouse = Mouse(0, 0, data, up_flag if up else down_flag, 0, 0)
        if self.user.SendInput(1, c.byref(packet), c.sizeof(packet)) != 1:
            raise OSError("Windows refused input injection (possibly an elevated target)")

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self, receive):
        if self.running:
            raise RuntimeError("Windows input hooks are already active")
        ready, stop = Event(), Event()
        self._stop = stop
        self._error = None
        self._thread_id = None
        self._thread = Thread(target=self._listen, args=(receive, ready, stop), daemon=True)
        self._thread.start()
        if not ready.wait(3):
            stop.set()
            self.stop()
            raise RuntimeError("Windows input hooks did not start")
        if self._error:
            self._thread.join(3)
            raise self._error

    def stop(self):
        if self.running:
            self._stop.set()
            if self._thread_id is not None:
                self.user.PostThreadMessageW(self._thread_id, 0x0012, 0, 0)
            self._thread.join(3)
            if self.running:
                raise RuntimeError("Windows input hooks did not stop")

    def request_stop(self):
        """Nonblocking stop request, also safe from the hook thread."""
        self._stop.set()
        if self._thread_id is not None:
            self.user.PostThreadMessageW(self._thread_id, 0x0012, 0, 0)

    def rehook(self):
        """Nonblocking: replace the hooks, which Windows silently removes after sleep or a hook
        that missed its timeout (the app would no longer see input)."""
        if self.running and self._thread_id is not None:
            self.user.PostThreadMessageW(self._thread_id, REHOOK, 0, 0)

    def _listen(self, receive, ready, stop):
        key_names = {code: name for name, code in KEYS.items()}
        mouse_messages = {0x201: ("left", True), 0x202: ("left", False),
                          0x204: ("right", True), 0x205: ("right", False),
                          0x207: ("middle", True), 0x208: ("middle", False)}

        @self.callback_type
        def keyboard(code, message, address):
            if code == 0 and not stop.is_set():
                data = c.cast(address, c.POINTER(KeyHook)).contents
                key = key_names.get(data.vk)
                if key and not data.flags & 0x10:
                    if receive("key", key, not data.flags & 0x80):
                        return 1
            return self.user.CallNextHookEx(None, code, message, address)

        @self.callback_type
        def mouse(code, message, address):
            if code == 0 and not stop.is_set() and (message in mouse_messages or message in {0x20B, 0x20C}):
                data = c.cast(address, c.POINTER(MouseHook)).contents
                if not data.flags & 1:
                    if message in mouse_messages:
                        button, down = mouse_messages[message]
                        if receive("button", button, down):
                            return 1
                    elif data.data >> 16 in {1, 2}:
                        if receive("button", f"x{data.data >> 16}", message == 0x20B):
                            return 1
            return self.user.CallNextHookEx(None, code, message, address)

        hooks = []

        def install():
            for hook_id, callback in ((13, keyboard), (14, mouse)):
                hook = self.user.SetWindowsHookExW(hook_id, callback, self.kernel.GetModuleHandleW(None), 0)
                if not hook:
                    raise c.WinError(c.get_last_error())
                hooks.append(hook)
            self.installs += 1

        try:
            message = w.MSG()
            self.user.PeekMessageW(c.byref(message), None, 0, 0, 0)
            self._thread_id = self.kernel.GetCurrentThreadId()
            install()
            ready.set()
            while not stop.is_set():
                result = self.user.GetMessageW(c.byref(message), None, 0, 0)
                if result == 0:
                    break
                if result == -1:
                    raise c.WinError(c.get_last_error())
                if message.message == REHOOK:
                    # Old hooks first: both chains at once would report one press twice.
                    while hooks:
                        self.user.UnhookWindowsHookEx(hooks.pop())
                    install()
        except OSError as exc:
            self._error = exc
        finally:
            for hook in reversed(hooks):
                self.user.UnhookWindowsHookEx(hook)
            ready.set()

"""Automatic profiles: the rule (time range or running application) that decides which applies now."""

from datetime import datetime
import os
import re
from typing import Any

from .domain import ApiError

TIME = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
APP = re.compile(r'^[^\\/:*?"<>|\x00-\x1f]{1,251}\.exe$', re.IGNORECASE)
MAX_APPS = 16


def validate_trigger(trigger: Any) -> dict[str, Any]:
    """{type: 'time', start: 'HH:MM', end: 'HH:MM'} (may pass midnight) or {type: 'app', apps: ['x.exe']}."""
    if isinstance(trigger, dict) and trigger.get("type") == "time" and set(trigger) == {"type", "start", "end"}:
        if not all(isinstance(trigger[key], str) and TIME.match(trigger[key]) for key in ("start", "end")):
            raise ApiError(400, "invalid_trigger", "Times must be HH:MM (00:00-23:59).")
        if trigger["start"] == trigger["end"]:
            raise ApiError(400, "invalid_trigger", "Start and end times must differ.")
        return dict(trigger)
    if isinstance(trigger, dict) and trigger.get("type") == "app" and set(trigger) == {"type", "apps"}:
        apps = trigger["apps"]
        if not isinstance(apps, list) or not 1 <= len(apps) <= MAX_APPS or not all(isinstance(app, str) for app in apps):
            raise ApiError(400, "invalid_trigger", f"Choose 1-{MAX_APPS} applications.")
        # A full path is accepted; Windows reports running programs by file name.
        names = [re.split(r"[\\/]", app.strip())[-1].lower() for app in apps]
        if not all(APP.match(name) for name in names):
            raise ApiError(400, "invalid_trigger", "Applications must be .exe program files.")
        return {"type": "app", "apps": list(dict.fromkeys(names))}
    raise ApiError(400, "invalid_trigger", "A rule is a time range or a list of applications.")


def _minutes(text: str) -> int:
    hours, minutes = text.split(":")
    return int(hours) * 60 + int(minutes)


def holds(trigger: dict[str, Any], now: datetime, apps: frozenset[str]) -> bool:
    if trigger["type"] == "app":
        return any(app in apps for app in trigger["apps"])
    start, end, minute = _minutes(trigger["start"]), _minutes(trigger["end"]), now.hour * 60 + now.minute
    return start <= minute < end if start < end else minute >= start or minute < end


def active(profiles: list[dict[str, Any]], now: datetime, apps: frozenset[str]) -> list[str]:
    """Automatic profiles whose rule holds, strongest first: an application rule beats a time
    rule (it is what the user is doing now); among the same kind the earlier profile wins."""
    automatic = [p for p in profiles if p.get("kind") == "automatic" and holds(p["trigger"], now, apps)]
    return [p["id"] for p in sorted(automatic, key=lambda p: p["trigger"]["type"] != "app")]


def needs_apps(profiles: list[dict[str, Any]]) -> bool:
    return any(p.get("kind") == "automatic" and p["trigger"]["type"] == "app" for p in profiles)


if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    class _Entry(ctypes.Structure):  # PROCESSENTRY32W
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", wintypes.LONG), ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    _kernel32.Process32FirstW.argtypes = _kernel32.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(_Entry))
    _kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)


def running_apps() -> frozenset[str]:
    """File names (lower case) of the running programs."""
    if os.name != "nt":
        return frozenset()
    snapshot = _kernel32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if snapshot in (None, wintypes.HANDLE(-1).value):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        entry, names = _Entry(), set()
        entry.dwSize = ctypes.sizeof(_Entry)
        found = _kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while found:
            names.add(entry.szExeFile.lower())
            found = _kernel32.Process32NextW(snapshot, ctypes.byref(entry))
        return frozenset(names)
    finally:
        _kernel32.CloseHandle(snapshot)

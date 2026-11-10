"""Application service and atomic on-disk profile repository."""

from copy import deepcopy
from contextlib import contextmanager
from datetime import datetime
import errno
import json
import logging
import os
from pathlib import Path
import tempfile
import threading
import time
from typing import Any
import uuid

from . import profile_rules
from .domain import ApiError, validate_settings, validate_settings_shape
from .providers import DeviceProvider, HardwareProvider, SimulatedProvider


LANGUAGES = ("tr", "en")
LOGGER = logging.getLogger(__name__)
# Seconds between checks for devices that appeared or came back (hardware mode).
RESTORE_INTERVAL = 20
# Seconds between checks of the automatic profiles' rules (time ranges, running programs).
RULES_INTERVAL = 5
DEFAULT_PROFILE = {"tr": "Varsayılan", "en": "Default"}


def default_data_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local" / "share")) / "UnifiedPeripheralManager"


def system_language() -> str:
    """Turkish only when the Windows display language is Turkish."""
    if os.name == "nt":
        import ctypes
        try:
            return "tr" if ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF == 0x1F else "en"
        except (AttributeError, OSError):
            pass
    return "en"


def saved_language(directory: str | Path) -> str | None:
    """Read the stored choice; a missing or damaged preference is never fatal."""
    path = Path(directory) / "preferences.json"
    try:
        if path.stat().st_size <= 64 * 1024:
            stored = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(stored, dict) and stored.get("language") in LANGUAGES:
                return stored["language"]
    except (OSError, ValueError):
        pass
    return None


class DeviceService:
    def __init__(self, data_dir: str | Path | None = None, provider: DeviceProvider | None = None,
                 *, mode: str = "simulation", automation_service=None, pick_file=None, autostart=None):
        if mode not in {"simulation", "hardware"}:
            raise ValueError("Mode must be simulation or hardware.")
        self.mode = mode
        self._autostart = autostart  # desktop.autostart.Autostart in the desktop app
        self.provider = provider if provider is not None else (HardwareProvider() if mode == "hardware" else SimulatedProvider())
        self._lock = threading.RLock()
        directory = Path(data_dir) if data_dir is not None else default_data_dir()
        self._path = directory / ("profiles.hardware.json" if mode == "hardware" else "profiles.json")
        self._state_path = directory / ("profile-state.hardware.json" if mode == "hardware" else "profile-state.json")
        self._desired_path = directory / "hardware-settings.json"
        self._preferences_path = directory / "preferences.json"
        self._language = saved_language(directory)
        self._preference_listeners = []
        self._closed = False
        self._profiles: list[dict[str, Any]] = []
        # One main profile is chosen; an automatic profile applies while its rule holds, and when
        # no rule holds the chosen main profile applies again (never "the previous one").
        self._main_id: str | None = None
        self._applied_id: str | None = None  # the profile the devices were last given
        self._manual: str | None = None  # an automatic profile chosen by hand, until a rule starts or ends
        self._dismissed: set[str] = set()  # automatic profiles set aside by choosing a main one, until their rule ends
        self._holding: list[str] | None = None  # automatic profiles whose rule held at the last check
        self._pending: set[str] = set()  # devices still to receive a profile's settings (hardware)
        self._rules_thread = None
        self._rules_wake = threading.Event()
        with self._profile_store_lock():
            self._refresh_profiles()
        self._read_state()
        from .automation_service import AutomationCoordinator
        self.automation = AutomationCoordinator(self, automation_service, directory / "assignments.json", pick_file)
        self._restored: set[str] = set()
        self._stopping = threading.Event()
        self._recheck = threading.Event()
        self.devices_revision = 0  # changes when devices came or went, so a window refreshes at once
        self._device_listeners = []
        self._watch_rules_if_needed()
        if mode == "hardware":
            threading.Thread(target=self._watch_devices, name="restore-settings", daemon=True).start()
            watch = getattr(self.provider, "watch_presence", None)
            if watch is not None:
                watch(self.devices_changed)

    @contextmanager
    def _profile_store_lock(self, path=None):
        # Lock a stable companion file, never the atomically replaced JSON inode.
        path = path or self._path
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.with_suffix(".lock").open("a+b") as stream:
            if os.name == "nt":
                import msvcrt
                if stream.tell() == 0:
                    stream.write(b"\0")
                    stream.flush()
                stream.seek(0)
                acquire = lambda: msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                release = lambda: msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                acquire = lambda: fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                release = lambda: fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            deadline = time.monotonic() + 2
            while True:
                try:
                    acquire()
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                        raise
                    if time.monotonic() >= deadline:
                        raise ApiError(503, "profile_store_busy", "Profile storage is busy; retry shortly.") from exc
                    time.sleep(0.02)
            try:
                yield
            finally:
                release()

    def _refresh_profiles(self):
        if self._path.exists():
            try:
                if self._path.stat().st_size > 1024 * 1024:
                    raise ValueError("Profile store exceeds 1 MiB.")
                loaded = json.loads(self._path.read_text(encoding="utf-8"))
                self._validate_store(loaded)
                self._profiles = loaded
            except (ValueError, ApiError, KeyError, TypeError) as exc:
                raise ValueError(f"Invalid profile store at {self._path}; preserve or repair it before restarting.") from exc
        else:
            self._profiles = []
        self._forget_missing()

    def _validate_store(self, profiles: Any) -> None:
        if not isinstance(profiles, list) or len(profiles) > 100:
            raise ValueError("Invalid profile list.")
        seen = set()
        for profile in profiles:
            if not isinstance(profile, dict) or not {"id", "name", "settings"} <= set(profile) <= {"id", "name", "kind", "settings", "trigger"}:
                raise ValueError("Invalid profile.")
            if not isinstance(profile["id"], str) or not profile["id"] or profile["id"] in seen:
                raise ValueError("Invalid profile ID.")
            profile["id"].encode("utf-8")
            seen.add(profile["id"])
            self._name(profile["name"])
            self._validate_snapshot(profile["settings"])
            profile.setdefault("kind", "main")  # profiles saved before automatic profiles existed
            if profile["kind"] not in ("main", "automatic") or (profile["kind"] == "automatic") != ("trigger" in profile):
                raise ValueError("Invalid profile kind.")
            if "trigger" in profile:
                profile["trigger"] = profile_rules.validate_trigger(profile["trigger"])

    def _profile(self, profile_id) -> dict[str, Any] | None:
        return next((p for p in self._profiles if p["id"] == profile_id), None)

    def _forget_missing(self) -> None:
        """Drop references to profiles another instance deleted."""
        main = self._profile(self._main_id)
        if main is None or main["kind"] != "main":
            self._main_id = None
        if self._profile(self._applied_id) is None:
            self._applied_id = None
        if self._profile(self._manual) is None:
            self._manual = None
        self._dismissed = {i for i in self._dismissed if self._profile(i) is not None}

    def _read_state(self) -> None:
        """The chosen main profile and the profile last applied; damage only forgets them."""
        try:
            if self._state_path.stat().st_size <= 64 * 1024:
                stored = json.loads(self._state_path.read_text(encoding="utf-8"))
                if isinstance(stored, dict):
                    self._main_id, self._applied_id = stored.get("mainProfileId"), stored.get("appliedProfileId")
        except (OSError, ValueError):
            pass
        if not isinstance(self._main_id, str):
            self._main_id = None
        if not isinstance(self._applied_id, str):
            self._applied_id = None
        self._forget_missing()

    def _write_state(self) -> None:
        with self._profile_store_lock(self._state_path):
            self._save({"mainProfileId": self._main_id, "appliedProfileId": self._applied_id}, self._state_path)

    def profile_state(self) -> dict[str, Any]:
        with self._lock:
            active = self._applied_id
            by = None if active is None else "main" if active == self._main_id else "manual" if active == self._manual else "rule"
            return {"mainProfileId": self._main_id, "activeProfileId": active, "activeBy": by}

    def _watch_rules_if_needed(self) -> None:
        """Check the rules only while an automatic profile exists."""
        with self._lock:
            if self._rules_thread is None and any(p["kind"] == "automatic" for p in self._profiles):
                self._rules_thread = threading.Thread(target=self._watch_rules, name="profile-rules", daemon=True)
                self._rules_thread.start()

    def _watch_rules(self) -> None:
        while not self._stopping.is_set():
            try:
                self.check_rules()
            except Exception:
                LOGGER.exception("Checking automatic profiles failed")
            self._rules_wake.wait(RULES_INTERVAL)
            self._rules_wake.clear()

    def check_rules(self, now: datetime | None = None, apps: frozenset[str] | None = None) -> None:
        """Apply the profile that should be active now (the clock and running programs decide)."""
        with self._lock:
            profiles = deepcopy(self._profiles)
        if apps is None:
            apps = profile_rules.running_apps() if profile_rules.needs_apps(profiles) else frozenset()
        holding = profile_rules.active(profiles, now or datetime.now(), apps)
        with self._lock:
            holding = [i for i in holding if self._profile(i) is not None]  # deleted meanwhile
            if self._holding is not None and set(holding) != set(self._holding):
                self._manual = None  # a rule started or ended: the rules decide again
            self._holding = holding
            self._dismissed &= set(holding)  # set aside only until its rule ends
            self._settle()

    def _settle(self) -> None:
        """Give the devices the profile that should be active, if they do not have it."""
        holding = [i for i in self._holding or () if i not in self._dismissed]
        target = self._manual or (holding[0] if holding else None) or self._main_id
        if target is not None and target != self._applied_id:
            self._switch(target)

    def _switch(self, profile_id: str) -> None:
        profile = self._profile(profile_id)
        snapshot = deepcopy(profile["settings"])
        if self.mode == "hardware":
            # Devices that are off or asleep get these settings when they come back.
            with self._profile_store_lock(self._desired_path):
                desired = self._read_desired()
                for device_id, values in snapshot.items():
                    if values:
                        desired.setdefault(device_id, {}).update(values)
                self._save(desired, self._desired_path)
            self._pending |= {device_id for device_id, values in snapshot.items() if values}
            self._recheck.set()
        else:
            self.provider.apply(self._validate_snapshot(snapshot))
        self._applied_id = profile_id
        self._write_state()
        self.devices_revision += 1  # an open window shows the new settings and active profile
        LOGGER.info("Profile %s applied", profile_id)

    def _current_snapshot(self) -> dict[str, dict[str, Any]]:
        devices = self.provider.devices()
        if self.mode != "hardware":
            return {d.id: d.settings for d in devices}
        # The mouse cannot report its settings, and a device that is off reports none: the
        # settings this app last gave each device count.
        with self._profile_store_lock(self._desired_path):
            desired = self._read_desired()
        return {d.id: {**d.settings, **desired.get(d.id, {})} for d in devices}

    @staticmethod
    def _name(name: Any) -> str:
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 64 or any(ord(c) < 32 for c in name):
            raise ApiError(400, "invalid_name", "Profile name must contain 1–64 visible characters.")
        try:
            name.encode("utf-8")
        except UnicodeEncodeError:
            raise ApiError(400, "invalid_name", "Profile name must contain valid Unicode characters.") from None
        return name.strip()

    def _validate_snapshot(self, settings: Any) -> dict[str, dict[str, Any]]:
        if self.mode == "hardware":
            if not isinstance(settings, dict) or not all(isinstance(key, str) and key for key in settings):
                raise ValueError("Invalid hardware settings snapshot.")
            for values in settings.values():
                validate_settings_shape(values)
            return settings
        devices = self.provider.devices()
        if not isinstance(settings, dict) or set(settings) != {d.id for d in devices}:
            raise ValueError("Profile must include every known device.")
        for device in devices:
            # Older snapshots may predate settings added later (e.g. backlight); unknown ones fail.
            validate_settings(device, settings[device.id])
        return settings

    def devices(self) -> list[dict[str, Any]]:
        with self._lock:
            devices = [d.to_dict() for d in self.provider.devices()]
            if self.mode == "hardware":
                with self._profile_store_lock(self._desired_path):
                    desired = self._read_desired()
                for device in devices:
                    device["desiredSettings"] = deepcopy(desired.get(device["id"], {}))
            return devices

    def _read_desired(self):
        if not self._desired_path.exists():
            return {}
        if self._desired_path.stat().st_size > 1024 * 1024:
            raise ValueError("Hardware settings store exceeds 1 MiB.")
        return self._validate_snapshot(json.loads(self._desired_path.read_text(encoding="utf-8")))

    def _apply_hardware(self, updates):
        devices = {device.id: device for device in self.provider.devices()}
        pending = {key: values for key, values in updates.items() if values}
        for device_id, values in pending.items():
            device = devices.get(device_id)
            if device is None or device.connection != "connected":
                raise ApiError(409, "device_disconnected", f"Device {device_id} is not connected.")
            validate_settings(device, values)
        applied = []
        for device_id, values in pending.items():
            try:
                self.provider.apply({device_id: deepcopy(values)})
                applied.append(device_id)
                with self._profile_store_lock(self._desired_path):
                    desired = self._read_desired()
                    desired.setdefault(device_id, {}).update(values)
                    self._save(desired, self._desired_path)
            except (ApiError, OSError, RuntimeError, ValueError) as exc:
                raise ApiError(409, "hardware_apply_failed", str(exc),
                               {"appliedDeviceIds": applied, "failedDeviceId": device_id,
                                "devices": [d.to_dict() for d in self.provider.devices()],
                                "partialApplyPossible": True}) from exc

    def _watch_devices(self):
        """Apply the saved settings at startup and keep them applied: the mouse and headset
        forget them when they power off (they are not stored on the devices), and nothing else
        applies them when the app starts."""
        while not self._stopping.is_set():
            try:
                self.restore_settings()
            except Exception:
                LOGGER.exception("Restoring saved settings failed")
            self._recheck.wait(RESTORE_INTERVAL)
            self._recheck.clear()

    def devices_changed(self, device_ids) -> None:
        """Devices appeared, came back or changed connection (seconds after it happened): restore
        their settings now and let the window and the tray update."""
        self._restored -= set(device_ids)
        self.devices_revision += 1
        self.recheck_devices()
        for listener in list(self._device_listeners):
            try:
                listener()
            except Exception:
                LOGGER.exception("Device listener failed")

    def add_device_listener(self, listener) -> None:
        self._device_listeners.append(listener)

    def recheck_devices(self) -> None:
        """Check devices now instead of at the next interval (e.g. after the computer woke up)."""
        forget = getattr(self.provider, "forget_readings", None)
        if forget is not None:
            forget()  # readings from before sleep are stale
        self._recheck.set()

    def restore_settings(self) -> None:
        """Send the saved settings to a device that appeared or came back, and to a device that
        reports different settings (e.g. the headset was switched off and on between checks)."""
        ready = {d.id: d for d in self.provider.devices()
                 if d.connection == "connected" and (d.status or {}).get("code") == "connected"}
        self._restored &= set(ready)  # a device that went away is restored again when it returns
        for device_id, device in ready.items():
            if self._stopping.is_set():
                return
            returned = device_id not in self._restored
            with self._lock:
                with self._profile_store_lock(self._desired_path):
                    saved = self._read_desired().get(device_id, {})
                switched = device_id in self._pending  # a profile was activated
                reports = (device.telemetry or {}).get("writeVerification") == "readback"
                # A device that cannot report its settings only gets them again when it returns.
                differs = bool(saved) and (switched or not (reports and self._matches(device.settings, saved)))
                if not returned and not switched and not (reports and differs):
                    continue
                try:
                    if differs:
                        self.provider.apply({device_id: deepcopy(validate_settings(device, saved))})
                        self.devices_revision += 1
                except (ApiError, OSError, RuntimeError, ValueError) as exc:
                    LOGGER.warning("Saved settings for %s not applied yet: %s", device_id, exc)
                    continue  # retried on the next check
                self._pending.discard(device_id)
            if returned:
                self.automation.reapply(device_id)
            self._restored.add(device_id)

    @staticmethod
    def _matches(current, saved) -> bool:
        """Saved settings that matter for the saved mode equal the reported ones."""
        unused = set()
        effect = saved.get("lightingEffect", "static")
        if effect == "static":
            unused.add("lightingSpeed")
        if effect == "rainbow":
            unused |= {"lightingColor", "lightingZones"}  # the cycle shows every color
        if saved.get("backlightMode") != "manual":
            unused.add("backlightLevel")  # automatic mode picks the level itself
        return all(current.get(key) == value for key, value in saved.items() if key not in unused)
    def update(self, device_id: str, values: Any) -> dict[str, Any]:
        with self._lock:
            device = next((d for d in self.provider.devices() if d.id == device_id), None)
            if device is None:
                raise ApiError(404, "device_not_found", "Device not found.")
            validated = validate_settings(device, values)
            if self.mode == "hardware":
                self._apply_hardware({device_id: validated})
            else:
                self.provider.apply({device_id: validated})
            self._remember(device_id, validated)
            return next(d for d in self.devices() if d["id"] == device_id)

    def _remember(self, device_id: str, values: dict[str, Any]) -> None:
        """A change made while a profile is active belongs to that profile."""
        if self._applied_id is None or not values:
            return
        with self._profile_store_lock():
            self._refresh_profiles()
            profile = self._profile(self._applied_id)
            if profile is not None:
                profile["settings"].setdefault(device_id, {}).update(deepcopy(values))
                self._save(self._profiles)

    def profiles(self) -> list[dict[str, Any]]:
        with self._lock, self._profile_store_lock():
            self._refresh_profiles()
            return deepcopy(self._profiles)

    def _unique_name(self, name: str, profile_id: str | None = None) -> None:
        if any(p["name"].casefold() == name.casefold() and p["id"] != profile_id for p in self._profiles):
            raise ApiError(409, "duplicate_profile", "A profile with this name already exists.")

    def create_profile(self, body: Any) -> dict[str, Any]:
        """A main profile (only a name) or an automatic one (kind 'automatic' and its trigger),
        holding the current settings. The new profile becomes the active one: a main profile is
        chosen, an automatic one is active by hand so the changes made next configure it."""
        if not isinstance(body, dict) or "name" not in body or not set(body) <= {"name", "kind", "trigger"}:
            raise ApiError(400, "invalid_profile", "Supply a profile name, and for an automatic profile its rule.")
        kind = body.get("kind", "main")
        if kind not in ("main", "automatic") or (kind == "automatic") != ("trigger" in body):
            raise ApiError(400, "invalid_profile", "Supply a profile name, and for an automatic profile its rule.")
        name = self._name(body["name"])
        trigger = profile_rules.validate_trigger(body["trigger"]) if kind == "automatic" else None
        with self._lock, self._profile_store_lock():
            self._refresh_profiles()
            # An automatic profile needs a main profile to return to: the current settings become one.
            default = kind == "automatic" and self._main_id is None
            if len(self._profiles) + 1 + default > 100:
                raise ApiError(409, "profile_limit", "The 100-profile limit has been reached.")
            self._unique_name(name)
            snapshot = self._current_snapshot()
            profiles = list(self._profiles)
            if default:
                base, number = DEFAULT_PROFILE[self.language()], 1
                taken = {p["name"].casefold() for p in profiles} | {name.casefold()}
                while (fallback := base if number == 1 else f"{base} {number}").casefold() in taken:
                    number += 1
                main = {"id": uuid.uuid4().hex, "name": fallback, "kind": "main", "settings": deepcopy(snapshot)}
                profiles.append(main)
            profile = {"id": uuid.uuid4().hex, "name": name, "kind": kind, "settings": snapshot}
            if trigger is not None:
                profile["trigger"] = trigger
            profiles.append(profile)
            self._save(profiles)
            self._profiles = profiles
            if default:
                self._main_id = main["id"]
            if kind == "main":
                self._choose_main(profile["id"])
            else:
                self._manual = profile["id"]
            self._applied_id = profile["id"]  # it holds the current settings: nothing to send
            self._write_state()
            self._watch_rules_if_needed()
            self._rules_wake.set()
            return deepcopy(profile)

    def update_profile(self, profile_id: str, body: Any) -> dict[str, Any]:
        """Rename a profile or change an automatic profile's rule."""
        if not isinstance(body, dict) or not body or not set(body) <= {"name", "trigger"}:
            raise ApiError(400, "invalid_profile", "Supply a new name and/or rule.")
        name = self._name(body["name"]) if "name" in body else None
        with self._lock, self._profile_store_lock():
            self._refresh_profiles()
            profile = self._profile(profile_id)
            if profile is None:
                raise ApiError(404, "profile_not_found", "Profile not found.")
            if "trigger" in body and profile["kind"] != "automatic":
                raise ApiError(400, "invalid_profile", "Only automatic profiles have a rule.")
            trigger = profile_rules.validate_trigger(body["trigger"]) if "trigger" in body else None
            if name is not None:
                self._unique_name(name, profile_id)
                profile["name"] = name
            if trigger is not None:
                profile["trigger"] = trigger
            self._save(self._profiles)
            self._rules_wake.set()  # the new rule may hold now
            return deepcopy(profile)

    def _choose_main(self, profile_id: str) -> None:
        self._main_id = profile_id
        self._manual = None
        # Choosing a main profile sets aside the automatic ones active now, until their rule ends.
        self._dismissed = set(self._holding or ())

    def activate(self, profile_id: str) -> dict[str, Any]:
        """Choose a main profile, or apply an automatic one by hand (until a rule starts or ends)."""
        with self._lock:
            with self._profile_store_lock():
                self._refresh_profiles()
                profile = self._profile(profile_id)
                if profile is None:
                    raise ApiError(404, "profile_not_found", "Profile not found.")
                self._validate_snapshot(profile["settings"])
                if profile["kind"] == "main":
                    self._choose_main(profile_id)
                else:
                    self._manual = profile_id
                self._switch(profile_id)  # again even when active: the devices may have drifted
            if self.mode == "hardware":
                self.restore_settings()  # devices that are off get it when they come back
            return {"devices": self.devices(), "profile": deepcopy(profile), "profileState": self.profile_state()}

    def delete_profile(self, profile_id: str) -> list[dict[str, Any]]:
        with self._lock, self._profile_store_lock():
            self._refresh_profiles()
            profiles = [p for p in self._profiles if p["id"] != profile_id]
            if len(profiles) == len(self._profiles):
                raise ApiError(404, "profile_not_found", "Profile not found.")
            if profile_id == self._main_id and profiles:
                raise ApiError(409, "main_profile_in_use", "Choose another main profile before deleting this one.")
            self._save(profiles)
            self._profiles = profiles
            if self._applied_id == profile_id:
                self._applied_id = None
            self._forget_missing()
            self._holding = [i for i in self._holding or () if i != profile_id]
            self._settle()  # an active automatic profile was deleted: back to the main profile
            self._write_state()
            return deepcopy(profiles)

    def clear_data(self) -> dict[str, Any]:
        """Delete profiles, saved device settings and key assignments; the language choice stays.

        Devices keep what they show now (nothing restores it later); assigned controls get
        their own function back.
        """
        self.automation.clear()
        with self._lock:
            with self._profile_store_lock():
                self._path.unlink(missing_ok=True)
                self._profiles = []
                self._forget_missing()
                self._holding, self._pending = None, set()
            with self._profile_store_lock(self._state_path):
                self._state_path.unlink(missing_ok=True)
            with self._profile_store_lock(self._desired_path):
                self._desired_path.unlink(missing_ok=True)
        return {"profiles": [], "assignments": self.automation.state(), "profileState": self.profile_state()}

    def language(self) -> str:
        return self._language or system_language()

    def preferences(self) -> dict[str, Any]:
        preferences = {"language": self.language()}
        # Missing when the app cannot be started by Windows (simulation tests, a source checkout).
        if self._autostart is not None and self._autostart.available:
            preferences["startWithWindows"] = self._autostart.enabled()
        return preferences

    def update_preferences(self, body: Any) -> dict[str, Any]:
        if not isinstance(body, dict) or not body or not set(body) <= {"language", "startWithWindows"}:
            raise ApiError(400, "invalid_preferences", "Supply language and/or startWithWindows.")
        if "language" in body and body["language"] not in LANGUAGES:
            raise ApiError(400, "invalid_preferences", "Language must be tr or en.")
        if "startWithWindows" in body:
            if type(body["startWithWindows"]) is not bool:
                raise ApiError(400, "invalid_preferences", "startWithWindows must be true or false.")
            if self._autostart is None or not self._autostart.available:
                raise ApiError(409, "autostart_unavailable", "Starting with Windows is available only in the installed app.")
            try:
                self._autostart.set(body["startWithWindows"])
            except OSError as exc:
                raise ApiError(500, "autostart_failed", "The Windows startup entry could not be changed.") from exc
        if "language" in body:
            self._set_language(body["language"])
        return self.preferences()

    def _set_language(self, language: str) -> None:
        with self._lock, self._profile_store_lock(self._preferences_path):
            self._save({"language": language}, self._preferences_path)
            self._language = language
            listeners = list(self._preference_listeners)
        for listener in listeners:
            try:
                listener(language)
            except Exception:
                # Native tray/title refresh must not fail the saved preference.
                LOGGER.exception("Language listener failed")

    def add_preference_listener(self, listener) -> None:
        with self._lock:
            self._preference_listeners.append(listener)

    def _save(self, profiles, path=None) -> None:
        path = path or self._path
        temporary = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                             prefix=".profiles-", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(profiles, stream, ensure_ascii=False, indent=2, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except OSError as exc:
            raise ApiError(500, "persistence_failed", "Settings could not be saved to local storage.") from exc
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._stopping.set()
        self._recheck.set()
        self._rules_wake.set()
        try:
            self.automation.close()
        finally:
            close = getattr(self.provider, "close", None)
            if close is not None:
                close()

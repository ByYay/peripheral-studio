"""Start with Windows: a per-user Run entry for the packaged app (no administrator rights).

The registry is the only record of the choice, so turning it off in Task Manager's
Startup apps (StartupApproved) is shown as off here too. The app starts in the tray.
"""

from pathlib import Path
import sys

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
NAME = "UnifiedPeripheralManager"
APPROVED = b"\x02" + bytes(11)  # Task Manager's "enabled" record


class Autostart:
    def __init__(self, executable=None, run_key=RUN_KEY, approved_key=APPROVED_KEY):
        # Windows can only start the packaged EXE; a source checkout has nothing to register.
        if executable is None and getattr(sys, "frozen", False):
            executable = sys.executable
        self.executable = Path(executable) if executable else None
        self._run_key, self._approved_key = run_key, approved_key

    @property
    def available(self) -> bool:
        return self.executable is not None

    def _command(self) -> str:
        return f'"{self.executable}"'

    def _read(self, key):
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as handle:
                return winreg.QueryValueEx(handle, NAME)[0]
        except FileNotFoundError:
            return None

    def enabled(self) -> bool:
        if not self.available or self._read(self._run_key) is None:
            return False
        approved = self._read(self._approved_key)
        # Task Manager stores an odd first byte (0x03) for a disabled entry.
        return not (isinstance(approved, bytes) and approved[:1] and approved[0] & 0x01)

    def set(self, enabled: bool) -> None:
        if not self.available:
            raise RuntimeError("Starting with Windows is available only in the installed app.")
        import winreg
        if enabled:
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, self._run_key) as handle:
                winreg.SetValueEx(handle, NAME, 0, winreg.REG_SZ, self._command())
            if self._read(self._approved_key) is not None:
                with winreg.CreateKey(winreg.HKEY_CURRENT_USER, self._approved_key) as handle:
                    winreg.SetValueEx(handle, NAME, 0, winreg.REG_BINARY, APPROVED)
        else:
            for key in (self._run_key, self._approved_key):
                try:
                    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_SET_VALUE) as handle:
                        winreg.DeleteValue(handle, NAME)
                except FileNotFoundError:
                    pass

    def refresh(self) -> None:
        """Point an existing entry at this EXE again if the app was moved."""
        stored = self._read(self._run_key) if self.available else None
        if stored is not None and stored != self._command():
            import winreg
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, self._run_key) as handle:
                winreg.SetValueEx(handle, NAME, 0, winreg.REG_SZ, self._command())

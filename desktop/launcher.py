"""Serve the built UI in a private Edge WebView2 window.

Run with ``python -m desktop``; use ``--simulation`` for explicit demo mode.
The app starts in the background with only its tray icon: clicking the icon (or launching it
again) opens the window, closing the window also closes its WebView2 and the tray Quit item
ends the app.
"""

from __future__ import annotations

import importlib
import argparse
import logging
import os
from pathlib import Path
import secrets
import sys
from threading import Thread

from .applog import AppLog
from .lifecycle import WindowLifecycle
from .power import WakeWatcher, register_restart
from .single_instance import SingleInstance
from .tray import TrayController
from .actions import open_url, request_script_approval, script_interpreter
from .autostart import Autostart
from .strings import text


ICON = Path(__file__).parent / "assets" / "upm.ico"  # drawn by packaging/make_icon.py
LOGGER = logging.getLogger(__name__)


class LaunchError(RuntimeError):
    """An actionable desktop startup failure; str() stays English for logs."""

    def __init__(self, key: str):
        super().__init__(text("en", key))
        self.key = key


def window_title(mode: str, language: str) -> str:
    return "Unified Peripheral Manager" + (text(language, "title.simulation") if mode == "simulation" else "")


def run(*, static_dir: Path | None = None, data_dir: Path | None = None,
        mode: str = "hardware") -> None:
    if mode not in {"hardware", "simulation"}:
        raise LaunchError("error.mode")
    if sys.platform != "win32":
        raise LaunchError("error.platform")

    static_dir = static_dir or Path(__file__).resolve().parents[1] / "frontend" / "dist"
    if not (static_dir / "index.html").is_file():
        raise LaunchError("error.frontend")

    try:
        webview = importlib.import_module("webview")
    except ImportError as exc:
        raise LaunchError("error.dependencies") from exc

    if data_dir is None:
        local_data = os.environ.get("LOCALAPPDATA")
        base = Path(local_data) if local_data else Path.home() / "AppData" / "Local"
        data_dir = base / "UnifiedPeripheralManager"

    instance = SingleInstance(data_dir, mode)
    if not instance.first:
        instance.notify_running()  # The running app shows its window; this launch ends.
        return

    logs = AppLog(data_dir / "logs")
    try:
        LOGGER.info("Started in %s mode", mode)
        _serve(webview, static_dir, data_dir, mode, instance)
    except Exception:
        LOGGER.exception("The app stopped after an error")
        raise
    finally:
        logs.close()


def _serve(webview, static_dir: Path, data_dir: Path, mode: str, instance) -> None:
    from backend.server import create_server

    if getattr(sys, "frozen", False):
        # Windows starts the app again after a crash or hang; a source checkout cannot be restarted.
        register_restart("--simulation" if mode == "simulation" else "")

    # On hybrid-graphics laptops prefer the discrete GPU for the 3D scene.
    browser_args = os.environ.get("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", "")
    if "--force_high_performance_gpu" not in browser_args:
        os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = f"{browser_args} --force_high_performance_gpu".strip()

    token = secrets.token_urlsafe(32)
    automation = None
    if mode == "hardware":
        from automation.windows import WindowsAutomation
        automation = WindowsAutomation(
            open_url=open_url,
            activate_profile=lambda identity: server.service.activate(identity),
            request_script_approval=lambda review: request_script_approval(lifecycle.window, review, server.service.language()),
            python_executable=script_interpreter(),
        )
    def pick_file():
        # Native Windows "Open" dialog; the browser side never sees the file system.
        window = lifecycle.window
        if window is None:
            return None
        dialog = getattr(getattr(webview, "FileDialog", None), "OPEN", None) or webview.OPEN_DIALOG
        chosen = window.create_file_dialog(dialog, allow_multiple=False)
        return chosen[0] if chosen else None

    autostart = Autostart()
    try:
        autostart.refresh()  # the EXE may have been moved since the entry was made
    except OSError:
        pass
    try:
        server = create_server(
            host="127.0.0.1", port=0, data_dir=data_dir, token=token,
            static_dir=static_dir.resolve(), mode=mode, automation_service=automation,
            pick_file=pick_file, autostart=autostart,
        )
    except BaseException:
        if automation is not None:
            automation.close()
        raise
    worker = Thread(target=server.serve_forever, name="upm-http", daemon=True)
    started = False
    tray = watcher = None
    try:
        worker.start()
        started = True
        port = server.server_address[1]

        def after_wake():
            # Windows may have dropped the input hooks; devices reconnect after sleep.
            if automation is not None:
                automation.refresh_hooks()
            server.service.recheck_devices()

        watcher = WakeWatcher(after_wake)
        watcher.start()

        def create_window():
            # A new window, with a new WebView2, each time the app is shown.
            return webview.create_window(
                window_title(mode, server.service.language()),
                url=f"http://127.0.0.1:{port}/#token={token}",
                width=1440,
                height=960,
                min_size=(960, 680),
                background_color="#0b0d12",
            )

        lifecycle = WindowLifecycle(create_window)
        instance.listen(lifecycle.show)
        tray = TrayController(lifecycle, server.service.devices, hardware=mode == "hardware",
                              read_language=server.service.language, autostart=autostart)

        def apply_language(language):
            tray.refresh_language()
            window = lifecycle.window
            if window is not None:
                window.set_title(window_title(mode, language))

        server.service.add_preference_listener(apply_language)
        server.service.add_device_listener(tray.check_now)  # low-battery alert for a device that came back
        tray.start()
        # Explicit selection prevents falling back to the legacy MSHTML engine.
        lifecycle.run(lambda: webview.start(gui="edgechromium", debug=False, private_mode=True, icon=str(ICON)))
    finally:
        if watcher is not None:
            watcher.stop()
        if tray is not None:
            tray.stop()
        if started:
            server.shutdown()
        server.server_close()
        if started:
            worker.join(timeout=5)
        server.service.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Unified Peripheral Manager desktop")
    parser.add_argument("--simulation", action="store_true", help="Use explicitly simulated devices")
    args = parser.parse_args(argv)
    try:
        run(mode="simulation" if args.simulation else "hardware")
    except LaunchError as exc:
        language = startup_language()
        report_error(text(language, exc.key), language)
        return 1
    except Exception as exc:
        # Preserve diagnostic detail without printing the session token or URL.
        language = startup_language()
        report_error(text(language, "error.startup", kind=type(exc).__name__), language)
        return 1
    return 0


def startup_language() -> str:
    """The saved UI language, else Windows; startup errors may precede the service."""
    try:
        from backend.service import default_data_dir, saved_language, system_language
        return saved_language(default_data_dir()) or system_language()
    except Exception:
        return "en"


def report_error(message: str, language: str = "en") -> None:
    if sys.stderr is not None:
        print(message, file=sys.stderr)
    elif sys.platform == "win32":
        # Windowed PyInstaller builds have no stderr; keep failures visible.
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, text(language, "error.title"), 0x10)

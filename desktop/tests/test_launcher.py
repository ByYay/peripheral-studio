import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen

from desktop import launcher


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "index.html").write_text("<!doctype html>", encoding="utf-8")
        self.webview = Mock()
        self.server = Mock(server_address=("127.0.0.1", 49123))
        self.factory = Mock(return_value=self.server)
        self.worker = Mock()
        self.backend_module = types.ModuleType("backend.server")
        self.backend_module.create_server = self.factory
        self.automation = Mock()
        self.tray = Mock()
        self.lifecycle = Mock(window=None)
        self.lifecycle.run.side_effect = self.show_once
        self.instance = Mock(first=True)
        self.watcher = Mock()

    def make_lifecycle(self, create_window):
        self.create_window = create_window
        return self.lifecycle

    def make_watcher(self, on_wake):
        self.on_wake = on_wake
        return self.watcher

    def show_once(self, start):
        # One tray click: the lifecycle creates a window and runs the GUI until it closes.
        self.lifecycle.window = self.create_window()
        start()

    def native(self):
        # Never install real input hooks or a real tray icon from unit tests.
        stack = contextlib.ExitStack()
        stack.enter_context(patch("automation.windows.WindowsAutomation", return_value=self.automation))
        stack.enter_context(patch.object(launcher, "TrayController", return_value=self.tray))
        stack.enter_context(patch.object(launcher, "WindowLifecycle", side_effect=self.make_lifecycle))
        stack.enter_context(patch.object(launcher, "SingleInstance", return_value=self.instance))
        stack.enter_context(patch.object(launcher, "WakeWatcher", side_effect=self.make_watcher))
        return stack

    def launch(self, mode="hardware"):
        with (
            self.native(),
            patch.object(sys, "platform", "win32"),
            patch.object(launcher.importlib, "import_module", return_value=self.webview),
            patch.dict(sys.modules, {"backend.server": self.backend_module}),
            patch.object(launcher, "Thread", return_value=self.worker),
            patch.object(launcher.secrets, "token_urlsafe", return_value="random-token"),
        ):
            launcher.run(static_dir=self.root, data_dir=self.root / "data", mode=mode)

    def test_loopback_token_private_webview_and_clean_shutdown(self):
        self.launch()
        kwargs = self.factory.call_args.kwargs
        pick_file = kwargs.pop("pick_file")
        self.assertIsInstance(kwargs.pop("autostart"), launcher.Autostart)
        self.assertEqual(kwargs, dict(
            host="127.0.0.1", port=0, data_dir=self.root / "data",
            token="random-token", static_dir=self.root.resolve(),
            mode="hardware", automation_service=self.automation,
        ))
        self.webview.create_window.return_value.create_file_dialog.return_value = ("C:\\Users\\me\\app.exe",)
        self.assertEqual(pick_file(), "C:\\Users\\me\\app.exe")
        self.webview.create_window.return_value.create_file_dialog.return_value = None
        self.assertIsNone(pick_file())
        self.assertEqual(
            self.webview.create_window.call_args.kwargs["url"],
            "http://127.0.0.1:49123/#token=random-token",
        )
        # The app runs from the tray; a click or another launch opens a window through the lifecycle.
        self.tray.start.assert_called_once()
        self.instance.listen.assert_called_once_with(self.lifecycle.show)
        self.lifecycle.run.assert_called_once()
        self.webview.start.assert_called_once_with(
            gui="edgechromium", debug=False, private_mode=True, icon=str(launcher.ICON),
        )
        self.assertTrue(launcher.ICON.is_file())
        self.tray.stop.assert_called_once()
        self.watcher.start.assert_called_once()
        self.watcher.stop.assert_called_once()
        self.on_wake()  # the computer woke up
        self.automation.refresh_hooks.assert_called_once()
        self.server.service.recheck_devices.assert_called_once()
        self.assertTrue((self.root / "data" / "logs" / "crash.log").is_file())
        self.server.shutdown.assert_called_once()
        self.server.server_close.assert_called_once()
        self.worker.join.assert_called_once_with(timeout=5)
        self.server.service.close.assert_called_once()

    def test_second_launch_shows_the_running_app_and_ends(self):
        self.instance.first = False
        self.launch()
        self.instance.notify_running.assert_called_once()
        self.factory.assert_not_called()  # no second server, hardware access or tray icon
        self.webview.start.assert_not_called()

    def test_language_change_updates_tray_and_window_title(self):
        self.server.service.language.return_value = "en"
        self.launch(mode="simulation")
        self.assertEqual(self.webview.create_window.call_args.args[0], "Unified Peripheral Manager — Simulation")
        listener = self.server.service.add_preference_listener.call_args.args[0]
        listener("tr")
        self.tray.refresh_language.assert_called_once()
        self.webview.create_window.return_value.set_title.assert_called_once_with(
            "Unified Peripheral Manager — Simülasyon")

    def test_startup_error_uses_saved_language(self):
        errors = io.StringIO()
        with (
            patch.object(launcher, "run", side_effect=launcher.LaunchError("error.frontend")),
            patch.object(launcher, "startup_language", return_value="tr"),
            contextlib.redirect_stderr(errors),
        ):
            self.assertEqual(launcher.main([]), 1)
        self.assertIn("Derlenmiş arayüz bulunamadı", errors.getvalue())

    def test_discrete_gpu_is_requested_without_dropping_existing_arguments(self):
        with patch.dict("os.environ", {"WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS": "--remote-debugging-port=1"}):
            self.launch()
            self.launch()
            import os
            self.assertEqual(os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"],
                             "--remote-debugging-port=1 --force_high_performance_gpu")

    def test_server_creation_failure_closes_automation(self):
        self.factory.side_effect = OSError("port unavailable")
        with self.assertRaisesRegex(OSError, "port unavailable"):
            self.launch()
        self.automation.close.assert_called_once()
        self.webview.create_window.assert_not_called()

    def test_window_creation_failure_stops_server(self):
        self.webview.create_window.side_effect = RuntimeError("window failed")
        with self.assertRaisesRegex(RuntimeError, "window failed"):
            self.launch()
        self.server.shutdown.assert_called_once()
        self.server.server_close.assert_called_once()

    def test_gui_failure_stops_server(self):
        self.webview.start.side_effect = RuntimeError("runtime unavailable")
        with self.assertRaisesRegex(RuntimeError, "runtime unavailable"):
            self.launch()
        self.server.shutdown.assert_called_once()
        self.server.server_close.assert_called_once()

    def test_thread_start_failure_closes_without_blocking_shutdown(self):
        self.worker.start.side_effect = RuntimeError("thread failed")
        with self.assertRaisesRegex(RuntimeError, "thread failed"):
            self.launch()
        self.server.shutdown.assert_not_called()
        self.server.server_close.assert_called_once()

    def test_missing_build_is_actionable(self):
        with patch.object(sys, "platform", "win32"):
            with self.assertRaisesRegex(launcher.LaunchError, "npm run build"):
                launcher.run(static_dir=self.root / "missing")

    def test_missing_dependency_is_actionable(self):
        with (
            patch.object(sys, "platform", "win32"),
            patch.object(launcher.importlib, "import_module", side_effect=ImportError),
        ):
            with self.assertRaisesRegex(launcher.LaunchError, "pip install"):
                launcher.run(static_dir=self.root)

    def test_non_windows_rejected_without_importing_gui(self):
        with patch.object(sys, "platform", "linux"):
            with self.assertRaisesRegex(launcher.LaunchError, "requires Windows"):
                launcher.run(static_dir=self.root)

    def test_main_reports_failure_without_leaking_url_token(self):
        errors = io.StringIO()
        with (
            patch.object(launcher, "run", side_effect=RuntimeError("secret-token")),
            contextlib.redirect_stderr(errors),
        ):
            self.assertEqual(launcher.main([]), 1)
        self.assertIn("RuntimeError", errors.getvalue())
        self.assertNotIn("secret-token", errors.getvalue())

    def test_real_server_serves_ui_and_authenticated_api(self):
        def inspect_running_server(**_kwargs):
            url = urlsplit(self.webview.create_window.call_args.kwargs["url"])
            base = f"{url.scheme}://{url.netloc}"
            token = parse_qs(url.fragment)["token"][0]
            with urlopen(base, timeout=3) as response:
                self.assertIn(b"<!doctype html>", response.read())
            with self.assertRaises(HTTPError) as caught:
                urlopen(base + "/api/v1/devices", timeout=3)
            self.assertEqual(caught.exception.code, 401)
            request = Request(base + "/api/v1/devices", headers={"X-UPM-Token": token})
            with urlopen(request, timeout=3) as response:
                self.assertEqual(len(json.load(response)["devices"]), 3)

        self.webview.start.side_effect = inspect_running_server
        with (
            self.native(),
            patch.object(sys, "platform", "win32"),
            patch.object(launcher.importlib, "import_module", return_value=self.webview),
        ):
            # Simulation keeps this real-server test independent of attached hardware.
            launcher.run(static_dir=self.root, data_dir=self.root / "integration-data", mode="simulation")
        self.webview.start.assert_called_once()

    def test_simulation_flag_selects_simulation_mode(self):
        with patch.object(launcher, "run") as run:
            self.assertEqual(launcher.main(["--simulation"]), 0)
        run.assert_called_once_with(mode="simulation")

    def test_windowed_build_failure_uses_native_dialog(self):
        import ctypes
        dialog = Mock()
        with (
            patch.object(sys, "platform", "win32"),
            patch.object(sys, "stderr", None),
            patch.object(ctypes, "windll", types.SimpleNamespace(
                user32=types.SimpleNamespace(MessageBoxW=dialog)), create=True),
        ):
            launcher.report_error("Missing frontend")
        self.assertEqual(dialog.call_args.args[1], "Missing frontend")

    def test_bundled_assets_are_resolved_relative_to_module(self):
        bundle = self.root / "bundle"
        assets = bundle / "frontend" / "dist"
        assets.mkdir(parents=True)
        (assets / "index.html").write_text("<!doctype html>")
        with (
            self.native(),
            patch.object(sys, "platform", "win32"),
            patch.object(launcher, "__file__", str(bundle / "desktop" / "launcher.py")),
            patch.object(launcher.importlib, "import_module", return_value=self.webview),
            patch.dict(sys.modules, {"backend.server": self.backend_module}),
            patch.object(launcher, "Thread", return_value=self.worker),
        ):
            launcher.run(data_dir=self.root / "data")
        self.assertEqual(self.factory.call_args.kwargs["static_dir"], assets.resolve())


if __name__ == "__main__":
    unittest.main()

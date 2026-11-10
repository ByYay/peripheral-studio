import sys
import threading
import types
import unittest
from unittest.mock import MagicMock, patch

from desktop.lifecycle import WindowLifecycle


class CloseReason:
    UserClosing, TaskManagerClosing, WindowsShutDown = "UserClosing", "TaskManagerClosing", "WindowsShutDown"


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        # Stand-ins for the .NET namespaces pythonnet provides inside the desktop app.
        forms = types.ModuleType("System.Windows.Forms")
        forms.CloseReason = CloseReason
        system = types.ModuleType("System")
        system.Action = lambda function: function
        core = types.ModuleType("Microsoft.Web.WebView2.Core")
        core.CoreWebView2MemoryUsageTargetLevel = types.SimpleNamespace(Normal="normal", Low="low")
        core.CoreWebView2ProcessFailedKind = types.SimpleNamespace(BrowserProcessExited=0, RenderProcessExited=1,
                                                                   RenderProcessUnresponsive=2, GpuProcessExited=6)
        modules = {"System": system, "System.Windows": types.ModuleType("System.Windows"),
                   "System.Windows.Forms": forms, "Microsoft": types.ModuleType("Microsoft"),
                   "Microsoft.Web": types.ModuleType("Microsoft.Web"),
                   "Microsoft.Web.WebView2": types.ModuleType("Microsoft.Web.WebView2"),
                   "Microsoft.Web.WebView2.Core": core}
        patcher = patch.dict(sys.modules, modules)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.created = []
        self.lifecycle = WindowLifecycle(self.create_window)

    def create_window(self):
        window = MagicMock()
        window.native.Invoke.side_effect = lambda action: action()
        self.created.append(window)
        return window

    def close(self, reason):
        self.lifecycle._closing(None, types.SimpleNamespace(CloseReason=reason, Cancel=False))

    def test_window_and_webview_exist_only_while_shown(self):
        running = threading.Event()
        closed = threading.Event()

        def start():  # webview.start: returns when the window closes
            running.set()
            closed.wait(5)
            closed.clear()

        loop = threading.Thread(target=self.lifecycle.run, args=(start,), daemon=True)
        loop.start()
        self.assertFalse(running.wait(0.3), "nothing is created before the first show request")
        self.lifecycle.show()  # tray click or another launch
        self.assertTrue(running.wait(3))
        first = self.created[0]
        self.lifecycle.show()  # already open: brought forward
        first.show.assert_called_once()
        first.restore.assert_called_once()
        running.clear()
        self.lifecycle._closed()  # the close button destroyed the window and its WebView2
        closed.set()
        self.assertFalse(running.wait(0.3), "a closed window is not reopened by itself")
        self.lifecycle.show()
        self.assertTrue(running.wait(3))
        self.assertEqual(len(self.created), 2)
        self.lifecycle.quit()
        self.created[1].destroy.assert_called_once()
        closed.set()
        loop.join(3)
        self.assertFalse(loop.is_alive())

    def test_failed_webview_processes_are_recovered(self):
        self.lifecycle.window = window = self.create_window()
        core = window.native.browser.webview.CoreWebView2
        handlers = []
        event = type("Event", (), {"__iadd__": lambda event, handler: handlers.append(handler) or event})()
        core.ProcessFailed = event
        self.lifecycle._loaded()
        self.lifecycle._loaded()  # later page loads of the same window subscribe only once
        self.assertEqual(len(handlers), 1)
        with self.assertLogs("desktop.lifecycle", "WARNING"):
            handlers[0](core, types.SimpleNamespace(ProcessFailedKind=1))  # the page's process ended
        core.Reload.assert_called_once()
        with self.assertLogs("desktop.lifecycle", "WARNING"):
            handlers[0](core, types.SimpleNamespace(ProcessFailedKind=0))  # the whole WebView2 ended
        self.assertTrue(self.lifecycle._reopen)
        for _ in range(100):  # the window is closed on a helper thread
            if window.destroy.called:
                break
            threading.Event().wait(0.01)
        window.destroy.assert_called_once()

    def test_a_broken_webview_is_replaced_by_a_new_window(self):
        starts = []

        def start():
            starts.append(1)
            if len(starts) == 1:
                self.lifecycle._reopen = True  # its browser process failed and the window closed
            else:
                self.lifecycle.quit()

        self.lifecycle.show()
        self.lifecycle.run(start)
        self.assertEqual(len(self.created), 2, "reopened without another tray click")

    def test_quit_from_the_tray_without_a_window_ends_the_loop(self):
        self.lifecycle.quit()
        self.lifecycle.run(lambda: self.fail("no window is opened after Quit"))
        self.assertEqual(self.created, [])

    def test_close_button_keeps_the_app_but_task_manager_and_shutdown_end_it(self):
        self.close(CloseReason.UserClosing)
        self.assertFalse(self.lifecycle.quitting)
        self.close(CloseReason.TaskManagerClosing)
        self.assertTrue(self.lifecycle.quitting)
        self.lifecycle.quitting = False
        self.close(CloseReason.WindowsShutDown)
        self.assertTrue(self.lifecycle.quitting)

    def test_minimized_page_drops_its_scene_then_is_suspended(self):
        self.lifecycle.window = window = self.create_window()
        control = window.native.browser.webview
        core = control.CoreWebView2
        with patch("desktop.lifecycle.Timer") as timer:
            self.lifecycle._pause()
        core.ExecuteScriptAsync.assert_called_once_with("window.dispatchEvent(new Event('upm:pause'))")
        core.TrySuspendAsync.assert_not_called()  # the page first gets time to release the scene
        self.assertEqual(timer.call_args.args[1], self.lifecycle._suspend)
        self.lifecycle._suspend()
        self.assertEqual((control.Visible, core.MemoryUsageTargetLevel), (False, "low"))
        core.TrySuspendAsync.assert_called_once()
        self.lifecycle._resume()
        self.assertEqual((control.Visible, core.MemoryUsageTargetLevel), (True, "normal"))
        core.ExecuteScriptAsync.assert_called_with("window.dispatchEvent(new Event('upm:resume'))")

    def test_window_restored_before_suspension_stays_active(self):
        self.lifecycle.window = window = self.create_window()
        control = window.native.browser.webview
        with patch("desktop.lifecycle.Timer"):
            self.lifecycle._pause()
        self.lifecycle._resume()
        self.lifecycle._suspend()  # the delayed suspension fires late
        control.CoreWebView2.TrySuspendAsync.assert_not_called()
        self.assertTrue(control.Visible)


if __name__ == "__main__":
    unittest.main()

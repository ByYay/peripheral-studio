"""Window lifetime: the app lives in the tray and the window, with its WebView2, exists only
while it is shown.

Clicking the tray icon or launching the app again creates the window; closing it destroys
the window and ends its browser processes; only the tray Quit item (or Task Manager and
Windows shutting down) ends the app. While the window is minimized, the page drops its 3D
scene and is suspended (WebView2 TrySuspendAsync with a low memory target). If a WebView2
process fails (e.g. after the computer wakes up), the page is reloaded or the window reopened.
Device monitoring, key listeners and battery notifications keep running in the Python service.
"""

import logging
from threading import Event, Thread, Timer

PAUSE_DELAY = 1.5  # seconds for the page to release its scene before it is suspended
LOGGER = logging.getLogger(__name__)


class WindowLifecycle:
    def __init__(self, create_window):
        self._create_window = create_window  # returns a new, not yet started pywebview window
        self.window = None
        self.quitting = False
        self.active = True
        self._wake = Event()
        self._reopen = False  # the WebView broke: open a fresh window once this one closes
        self._watched = None  # the window whose WebView2 failures are handled

    def run(self, start):
        """On the main thread: wait for a show request, create the window and run the GUI
        (start) until the window closes; repeat until Quit."""
        while True:
            while not self._wake.wait(1):  # a timeout keeps Ctrl+C working in development
                pass
            self._wake.clear()
            if self.quitting:
                return
            window = self._create_window()
            window.events.before_show += self._attach
            window.events.closed += self._closed
            window.events.loaded += self._loaded
            window.events.minimized += self._pause
            window.events.restored += self._resume
            window.events.maximized += self._resume
            self.active, self.window = True, window
            try:
                start()
            finally:
                self.window = None
            if self._reopen:
                self._reopen = False
                self._wake.set()

    def _attach(self, window):
        window.native.FormClosing += self._closing

    def _closing(self, _sender, args):
        from System.Windows.Forms import CloseReason

        # The close button only closes the window; Task Manager or Windows shutting down end the app.
        if args.CloseReason in (CloseReason.TaskManagerClosing, CloseReason.WindowsShutDown):
            self.quitting = True
            self._wake.set()

    def _loaded(self, *_):
        window = self.window
        if window is None or window is self._watched:
            return
        self._watched = window

        def watch(_control, core):
            core.ProcessFailed += self._process_failed

        self._on_ui(watch)

    def _process_failed(self, sender, args):
        from Microsoft.Web.WebView2.Core import CoreWebView2ProcessFailedKind as Kind

        kind = args.ProcessFailedKind
        LOGGER.warning("WebView2 process failed: %s", kind)
        if kind == Kind.BrowserProcessExited:
            self._reopen = True
            Thread(target=self.hide, name="upm-reopen", daemon=True).start()
        elif kind in (Kind.RenderProcessExited, Kind.RenderProcessUnresponsive):
            sender.Reload()
        # A failed GPU process is restarted by WebView2; the page rebuilds its lost 3D view.

    def _closed(self, *_):
        self.window = None  # from now on a show request opens a new window

    def _on_ui(self, apply):
        """Run apply(control, core) on the UI thread: pythonnet keeps the GIL while reading .NET
        properties, so reading them from another thread can deadlock with the UI thread."""
        form = getattr(self.window, "native", None)
        control = getattr(getattr(form, "browser", None), "webview", None)
        if control is None:
            return
        from System import Action

        def run():
            core = control.CoreWebView2
            if core is not None:
                apply(control, core)

        form.Invoke(Action(run))

    def _pause(self, *_):
        if not self.active:
            return
        self.active = False
        self._on_ui(lambda _control, core: core.ExecuteScriptAsync("window.dispatchEvent(new Event('upm:pause'))"))
        timer = Timer(PAUSE_DELAY, self._suspend)
        timer.daemon = True
        timer.start()

    def _suspend(self):
        from Microsoft.Web.WebView2.Core import CoreWebView2MemoryUsageTargetLevel as Level

        def apply(control, core):
            if self.active:
                return  # Restored meanwhile.
            # An invisible WebView reports the page hidden; only then can it be suspended.
            control.Visible = False
            core.MemoryUsageTargetLevel = Level.Low
            core.TrySuspendAsync()

        self._on_ui(apply)

    def _resume(self, *_):
        if self.active:
            return
        self.active = True
        from Microsoft.Web.WebView2.Core import CoreWebView2MemoryUsageTargetLevel as Level

        def apply(control, core):
            control.Visible = True  # resumes a suspended page
            core.MemoryUsageTargetLevel = Level.Normal
            core.ExecuteScriptAsync("window.dispatchEvent(new Event('upm:resume'))")

        self._on_ui(apply)

    def show(self):
        """Tray click or another launch: bring the open window forward, or open a new one."""
        window = self.window
        if window is None:
            self._wake.set()
            return
        window.show()
        window.restore()
        self._resume()

    def hide(self):
        """Tray Hide: close the window; the app keeps running in the tray."""
        window = self.window
        if window is not None:
            window.destroy()

    def quit(self):
        self.quitting = True
        window = self.window
        if window is not None:
            window.destroy()
        self._wake.set()

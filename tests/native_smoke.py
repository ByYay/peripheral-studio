"""Opt-in actual Windows WebView2 smoke: python tests/native_smoke.py."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import webview
from desktop import launcher
from desktop.single_instance import SingleInstance

result = {'passed': False}
create_window = webview.create_window
lifecycles = []


class RecordedLifecycle(launcher.WindowLifecycle):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        lifecycles.append(self)


def open_from_tray(data):
    # The app starts with only its tray icon; another launch's signal opens the window.
    time.sleep(5)
    SingleInstance(data, 'hardware').notify_running()


def inspect(window):
    try:
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            state = window.evaluate_js("({devices:document.querySelectorAll('.device-card').length,canvas:!!document.querySelector('canvas'),error:document.querySelector('.error-banner')?.textContent})")
            if state and state.get('devices') == 3 and state.get('canvas'):
                window.evaluate_js("document.querySelectorAll('.device-card')[2].click()")
                time.sleep(.5)
                name = window.evaluate_js("document.querySelector('.focus-bar strong').textContent")
                assert 'Aerox' in name
                result.update(passed=True, devices=3, canvas=True, selection=True)
                break
            time.sleep(.2)
        assert result['passed'], f'Native UI did not become ready: {state}'
    except Exception as exc:
        result['error'] = str(exc)
    finally:
        lifecycles[0].quit()  # the tray Quit item; closing the window keeps the app in the tray


if __name__ == '__main__':
    with tempfile.TemporaryDirectory() as data:
        def capture(*args, **kwargs):
            window = create_window(*args, **kwargs)
            started = threading.Event()
            def loaded():
                if not started.is_set():
                    started.set()
                    threading.Thread(target=inspect, args=(window,), daemon=True).start()
            window.events.loaded += loaded
            return window

        threading.Thread(target=open_from_tray, args=(Path(data),), daemon=True).start()
        with patch.object(webview, 'create_window', capture), patch.object(launcher, 'WindowLifecycle', RecordedLifecycle):
            launcher.run(data_dir=Path(data))
    print(json.dumps(result))
    raise SystemExit(0 if result['passed'] else 1)

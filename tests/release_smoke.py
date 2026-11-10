"""Opt-in packaged EXE test; temporary data and loopback-only WebView debugging.

python tests/release_smoke.py [--hardware]; hardware mode reads attached devices only.
"""
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time

import psutil
from playwright.sync_api import sync_playwright, expect

root = Path(__file__).resolve().parents[1]
exe = root / 'dist' / 'UnifiedPeripheralManager.exe'
hardware = '--hardware' in sys.argv[1:]
title = 'Unified Peripheral Manager' + ('' if hardware else ' — Simulation')
mode_args = [] if hardware else ['--simulation']


def app_windows():
    user32 = ctypes.windll.user32
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    found = []
    def visit(hwnd, _):
        text = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, text, len(text))
        if text.value == title:
            found.append(hwnd)
        return True
    user32.EnumWindows(callback_type(visit), 0)
    return found


def close_window():
    # WM_CLOSE from another process (like Task Manager) quits the app.
    for hwnd in app_windows():
        ctypes.windll.user32.PostMessageW(hwnd, 0x0010, 0, 0)


def visible_windows():
    return [hwnd for hwnd in app_windows() if ctypes.windll.user32.IsWindowVisible(hwnd)]


def webviews(process):
    return [p for p in psutil.Process(process.pid).children(recursive=True) if p.name().lower() == 'msedgewebview2.exe']


def launch_again(env, data):
    """A second launch opens the running app's window (single instance) and ends."""
    assert subprocess.run([str(exe)] + mode_args, cwd=data, env=env, timeout=60).returncode == 0
    deadline = time.monotonic() + 20
    while not visible_windows() and time.monotonic() < deadline:
        time.sleep(0.2)
    assert visible_windows(), 'A second launch must open the window'


def connect(p, process):
    """CDP to the current window's WebView2; every opened window has a new one."""
    deadline = time.monotonic() + 50
    while time.monotonic() < deadline and process.poll() is None:
        try:
            return p.chromium.connect_over_cdp(f'http://127.0.0.1:{port}', timeout=1000).contexts[0].pages[0]
        except Exception:
            time.sleep(.3)
    raise AssertionError(f'Packaged WebView did not start; exit={process.poll()}')


def check_close_button_closes_webview(process, env, data):
    ctypes.windll.user32.PostMessageW(visible_windows()[0], 0x0112, 0xF060, 0)  # SC_CLOSE: the title-bar close button
    deadline = time.monotonic() + 15
    while (app_windows() or webviews(process)) and time.monotonic() < deadline:
        time.sleep(0.3)
    assert process.poll() is None, 'The close button must keep the app running in the tray'
    assert not app_windows() and not webviews(process), 'Closing the window must end its WebView2 processes'
    launch_again(env, data)


def renderer(page):
    return page.evaluate("""() => { const gl = document.createElement('canvas').getContext('webgl');
        const info = gl && gl.getExtension('WEBGL_debug_renderer_info');
        return info ? gl.getParameter(info.UNMASKED_RENDERER_WEBGL) : 'unknown'; }""")


def check_hardware(page, process):
    expect(page.locator('.simulation-badge')).to_contain_text('HARDWARE')
    expect(page.locator('.device-card')).to_have_count(3, timeout=20000)
    expect(page.locator('canvas')).to_be_visible()
    expect(page.locator('.settings-panel')).to_have_count(0)
    # A connected keyboard reports its own name (e.g. "MX Keys Mini for Business").
    page.locator('.device-card', has_text='MX Keys Mini').click()
    # Selecting a device shows its parts; settings open only for a chosen part.
    expect(page.locator('.focus-bar')).to_be_visible()
    expect(page.locator('.settings-panel')).to_have_count(0)
    page.get_by_role('button', name='General', exact=True).click()
    expect(page.locator('.settings-panel')).to_be_visible()
    page.screenshot(path=str(root / '.test-artifacts' / 'release-hardware.png'), full_page=True)
    gpu = renderer(page)
    close_window()
    assert process.wait(timeout=15) == 0
    print(json.dumps({'passed':True,'packaged':True,'mode':'hardware','devices':3,'canvas':True,'partPanel':True,'cleanExit':True,'renderer':gpu}))


with socket.socket() as probe:
    probe.bind(('127.0.0.1', 0))
    port = probe.getsockname()[1]


if __name__ == '__main__':
    assert exe.is_file(), 'Build the release EXE first'
    (root / '.test-artifacts').mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as data:
        # Role names and the window title below are English regardless of Windows language.
        app_data = Path(data) / 'UnifiedPeripheralManager'
        app_data.mkdir()
        (app_data / 'preferences.json').write_text('{"language": "en"}', encoding='utf-8')
        env = dict(os.environ, LOCALAPPDATA=data,
                   WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=f'--remote-debugging-port={port} --remote-debugging-address=127.0.0.1')
        process = subprocess.Popen([str(exe)] + mode_args, cwd=data, env=env)
        try:
            # The app starts in the background with only its tray icon: no window, no WebView2.
            time.sleep(8)
            assert process.poll() is None and not app_windows() and not webviews(process), 'The app must start in the tray'
            launch_again(env, data)
            with sync_playwright() as p:
                page = connect(p, process)
                if hardware:
                    check_hardware(page, process)
                    raise SystemExit(0)
                expect(page.locator('.device-card')).to_have_count(3, timeout=20000)
                expect(page.locator('canvas')).to_be_visible()
                assert page.locator('.device-card strong').all_inner_texts() == ['Logitech G733', 'Logitech MX Keys Mini', 'SteelSeries Aerox 3 Wireless']
                page.get_by_role('button', name='SteelSeries Aerox 3 Wireless').click()
                expect(page.locator('.focus-bar')).to_be_visible()
                expect(page.locator('.settings-panel')).to_have_count(0)
                page.get_by_role('button', name='General', exact=True).click()
                page.get_by_role('combobox', name='Polling rate').select_option('500')
                page.get_by_role('button', name='Apply', exact=True).click()
                expect(page.locator('.status-message')).to_contain_text('Settings applied')
                page.reload()
                expect(page.locator('.device-card')).to_have_count(3)
                page.get_by_role('button', name='SteelSeries Aerox 3 Wireless').click()
                page.get_by_role('button', name='General', exact=True).click()
                expect(page.get_by_role('combobox', name='Polling rate')).to_have_value('500')
                page.keyboard.press('Escape')
                page.get_by_role('button', name='DPI button', exact=True).click()
                page.get_by_role('radio', name='Media control').click()
                page.get_by_role('button', name='Save', exact=True).click()
                expect(page.locator('.status-message')).to_contain_text('Assignment saved')
                page.keyboard.press('Escape')
                page.get_by_role('button', name='Rear light', exact=True).click()
                page.get_by_role('button', name='Set lighting color #ff0000').click()
                page.get_by_role('button', name='Apply', exact=True).click()
                expect(page.locator('.status-message')).to_contain_text('Settings applied')
                page.keyboard.press('Escape')
                page.keyboard.press('Escape')
                page.get_by_role('button', name='Logitech MX Keys Mini').click()
                page.get_by_role('button', name='General', exact=True).click()
                page.get_by_role('combobox', name='Backlight mode').select_option('manual')
                page.get_by_role('slider', name='Backlight level').press('End')
                page.get_by_role('button', name='Apply', exact=True).click()
                expect(page.locator('.status-message')).to_contain_text('Settings applied')
                page.screenshot(path=str(root / '.test-artifacts' / 'release.png'), full_page=True)
                gpu = renderer(page)
                check_close_button_closes_webview(process, env, data)
                page = connect(p, process)
                expect(page.locator('canvas')).to_have_count(1, timeout=20000)
                close_window()
                assert process.wait(timeout=15) == 0
                print(json.dumps({'passed':True,'packaged':True,'devices':3,'canvas':True,'settings':True,'reload':True,'startsInTray':True,'closeEndsWebView':True,'reopens':True,'cleanExit':True,'renderer':gpu}))
        finally:
            if process.poll() is None:
                close_window()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
            # A one-file EXE runs the app in a child process; terminating only the
            # bootloader would leave the app (and its 3D scene) running.
            subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True)

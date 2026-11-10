# Build from the project root: python -m PyInstaller --noconfirm UPM.spec
from pathlib import Path

root = Path(SPECPATH)
assets = root / 'frontend' / 'dist'
if not (assets / 'index.html').is_file():
    raise RuntimeError('Build frontend/dist before packaging the desktop application.')

# Optional workspace hardware dependencies; hardware.runtime loads them from
# hardware/vendor and hardware/tools beside its module, and reports them missing otherwise.
icon = root / 'desktop' / 'assets' / 'upm.ico'  # packaging/make_icon.py
datas = [(str(assets), 'frontend/dist'), (str(icon.parent), 'desktop/assets')]
for folder in ('vendor', 'tools'):
    if (root / 'hardware' / folder).is_dir():
        datas.append((str(root / 'hardware' / folder), f'hardware/{folder}'))

a = Analysis(
    [str(root / 'packaging' / 'entry.py')],
    pathex=[str(root)],
    binaries=[],
    datas=datas,
    hiddenimports=['webview', 'webview.platforms.winforms', 'webview.platforms.edgechromium',
                   'pystray._win32'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['PyQt5', 'PyQt6', 'PySide2', 'PySide6', 'webview.platforms.qt',
              'webview.platforms.gtk', 'webview.platforms.cocoa', 'webview.platforms.android',
              'cefpython3', 'tkinter'],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name='UnifiedPeripheralManager',
    debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
    console=False, disable_windowed_traceback=False, icon=str(icon),
)

# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas = []
binaries = []
hiddenimports = ['nicegui', 'nicegui.ui', 'nicegui.elements', 'uvicorn', 'uvicorn.logging', 'uvicorn.protocols', 'uvicorn.protocols.http', 'uvicorn.protocols.websockets', 'uvicorn.lifespan', 'starlette', 'starlette.applications', 'starlette.routing', 'starlette.responses', 'starlette.staticfiles', 'webview', 'webview.platforms.cocoa', 'engineio.async_drivers', 'engineio.async_drivers.threading', 'socketio.async_drivers']
tmp_ret = collect_all('minicat')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('nicegui')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['/Users/mniittymaki/minicat/minicat/ui/desktop.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='CAT+TAG',
    debug=False,
    bootloader_ignore_signals=False,
    strip=True,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['/Users/mniittymaki/minicat/assets/cat-tag.icns'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=True,
    upx=False,
    upx_exclude=[],
    name='CAT+TAG',
)
app = BUNDLE(
    coll,
    name='CAT+TAG.app',
    icon='/Users/mniittymaki/minicat/assets/cat-tag.icns',
    bundle_identifier=None,
)

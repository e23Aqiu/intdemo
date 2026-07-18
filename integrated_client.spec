# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas = []
binaries = []
hiddenimports = [
    'PyQt5', 'PyQt5.sip',
    'pandas', 'pandas._libs', 'openpyxl', 'xlrd',
    'DrissionPage', 'DataRecorder', 'DownloadKit', 'DrissionGet', 'DrissionRecord',
    'playwright', 'playwright.sync_api',
    'ddddocr', 'onnxruntime', 'cv2', 'PIL', 'pinyin',
]

for package in ('ddddocr', 'DrissionPage', 'DataRecorder', 'DownloadKit', 'playwright'):
    collected = collect_all(package)
    datas += collected[0]
    binaries += collected[1]
    hiddenimports += collected[2]

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['PyQt6', 'matplotlib', 'tensorflow'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='运输业务一体化客户端',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

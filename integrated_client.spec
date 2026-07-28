# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all
from integrated_client.config import APP_NAME

datas = []
binaries = []
datas += [
    ('integrated_client/ui/assets/check.svg', 'integrated_client/ui/assets'),
    ('integrated_client/ui/assets/minus.svg', 'integrated_client/ui/assets'),
    ('integrated_client/ui/assets/chevron-down.svg', 'integrated_client/ui/assets'),
    ('integrated_client/ui/assets/chevron-up.svg', 'integrated_client/ui/assets'),
    ('integrated_client/ui/assets/nav-dashboard.svg', 'integrated_client/ui/assets'),
    ('integrated_client/ui/assets/nav-data.svg', 'integrated_client/ui/assets'),
    ('integrated_client/ui/assets/nav-workflow.svg', 'integrated_client/ui/assets'),
    ('integrated_client/ui/assets/nav-accounts.svg', 'integrated_client/ui/assets'),
    ('integrated_client/ui/assets/nav-user.svg', 'integrated_client/ui/assets'),
    ('integrated_client/ui/assets/announcement.svg', 'integrated_client/ui/assets'),
    ('integrated_client/ui/assets/nav-announcement.svg', 'integrated_client/ui/assets'),
    ('integrated_client/ui/assets/toolbar-align-left.svg', 'integrated_client/ui/assets'),
    ('integrated_client/ui/assets/toolbar-align-center.svg', 'integrated_client/ui/assets'),
    ('integrated_client/ui/assets/toolbar-align-right.svg', 'integrated_client/ui/assets'),
    ('integrated_client/ui/assets/toolbar-bullets.svg', 'integrated_client/ui/assets'),
    ('integrated_client/ui/assets/app-icon.png', 'integrated_client/ui/assets'),
    ('integrated_client/ui/assets/app-icon.ico', 'integrated_client/ui/assets'),
    ('integrated_client/online/client-online.json', 'integrated_client/online'),
]
hiddenimports = [
    'PyQt5', 'PyQt5.sip',
    'pandas', 'pandas._libs', 'openpyxl', 'xlrd',
    'DrissionPage', 'DataRecorder', 'DownloadKit', 'DrissionGet', 'DrissionRecord',
    'playwright', 'playwright.sync_api',
    'ddddocr', 'onnxruntime', 'cv2', 'PIL', 'pinyin',
    'cryptography', 'cryptography.hazmat.bindings._rust',
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
    [],
    exclude_binaries=True,
    name=APP_NAME,
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
    version='installer/version_info.txt',
    icon='integrated_client/ui/assets/app-icon.ico',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name=APP_NAME,
)

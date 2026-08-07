# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

from PyInstaller.utils.hooks import collect_all


ROOT = Path(SPECPATH)
ASSET_DIR = ROOT / "integrated_client" / "ui" / "assets"

datas = [
    (str(path), "integrated_client/ui/assets")
    for path in sorted(ASSET_DIR.iterdir())
    if path.is_file()
]
datas.append(
    (
        str(ROOT / "integrated_client" / "online" / "client-online.json"),
        "integrated_client/online",
    )
)
binaries = []
hiddenimports = [
    "PyQt5",
    "PyQt5.sip",
    "pandas",
    "pandas._libs",
    "openpyxl",
    "xlrd",
    "DrissionPage",
    "DataRecorder",
    "DownloadKit",
    "DrissionGet",
    "DrissionRecord",
    "playwright",
    "playwright.sync_api",
    "ddddocr",
    "onnxruntime",
    "cv2",
    "PIL",
    "pinyin",
    "cryptography",
    "cryptography.hazmat.bindings._rust",
]


def without_downloaded_browsers(items):
    """Never bundle an accidental x86/new-glibc Playwright browser cache."""
    return [item for item in items if ".local-browsers" not in str(item[0])]


for package in (
    "ddddocr",
    "DrissionPage",
    "DataRecorder",
    "DownloadKit",
    "playwright",
):
    collected = collect_all(package)
    datas += without_downloaded_browsers(collected[0])
    binaries += without_downloaded_browsers(collected[1])
    hiddenimports += collected[2]

a = Analysis(
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["PyQt6", "PySide6", "matplotlib", "tensorflow", "tkinter"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="intdemo-client",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="intdemo-client",
)

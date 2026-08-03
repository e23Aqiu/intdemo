#!/usr/bin/env python3
"""UOS ARM64 build/runtime preflight with actionable diagnostics."""

import argparse
import importlib
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _glibc_version():
    try:
        value = os.confstr("CS_GNU_LIBC_VERSION") or ""
    except (AttributeError, OSError, ValueError):
        value = ""
    return value.rsplit(" ", 1)[-1] if " " in value else value


def _version_tuple(value):
    parts = []
    for part in str(value or "").split("."):
        digits = "".join(character for character in part if character.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def _uos_description():
    for path in (Path("/etc/os-version"), Path("/etc/os-release")):
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if content.strip():
            return path, content
    return None, ""


def _package_version(module):
    return str(
        getattr(module, "__version__", "")
        or getattr(module, "version", "")
        or "已导入"
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--require-uos", action="store_true")
    parser.add_argument("--skip-browser-launch", action="store_true")
    parser.add_argument("--skip-secret-service", action="store_true")
    args = parser.parse_args()

    errors = []
    warnings = []
    print("=== UOS ARM64 兼容预检 ===")
    print(f"系统: {platform.platform()}")
    print(f"架构: {platform.machine()}")
    print(f"Python: {platform.python_version()} ({sys.executable})")
    print(f"glibc: {_glibc_version() or '无法识别'}")
    print(f"桌面会话: {os.environ.get('XDG_SESSION_TYPE') or '未设置'}")
    print(f"Qt 平台: {os.environ.get('QT_QPA_PLATFORM') or '自动'}")

    machine = platform.machine().casefold()
    if not sys.platform.startswith("linux"):
        errors.append("必须在 Linux 真机上构建 UOS 包")
    if machine not in {"aarch64", "arm64"}:
        errors.append(f"必须在 ARM64 真机上构建，当前为 {machine or '未知'}")
    if sys.version_info < (3, 9):
        errors.append("构建环境至少需要 Python 3.9（不要使用 UOS 系统 Python 3.7）")
    glibc = _version_tuple(_glibc_version())
    if glibc and glibc < (2, 28):
        errors.append(f"glibc 过旧：{_glibc_version()}，目标基线为 2.28")

    os_path, os_content = _uos_description()
    is_uos = "systemname=uos" in os_content.casefold() or "uniontech" in os_content.casefold()
    print(f"系统标识: {os_path or '未找到'} ({'UOS' if is_uos else '非 UOS/未识别'})")
    if args.require_uos and not is_uos:
        errors.append("未识别到统信 UOS；如仅做开发验证，请去掉 --require-uos")

    modules = (
        "PyQt5.QtCore",
        "cryptography",
        "numpy",
        "pandas",
        "openpyxl",
        "onnxruntime",
        "cv2",
        "PIL",
        "playwright",
        "ddddocr",
        "DrissionPage",
    )
    print("\n依赖导入:")
    for name in modules:
        try:
            module = importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001 - diagnostic boundary
            errors.append(f"依赖 {name} 导入失败：{exc}")
            print(f"  [失败] {name}: {exc}")
        else:
            print(f"  [正常] {name}: {_package_version(module)}")

    browser_path = None
    try:
        from integrated_client.browser import (
            check_builtin_chromium,
            get_builtin_chromium_path,
        )

        browser_path = get_builtin_chromium_path()
        print(f"\nChromium: {browser_path}")
        try:
            version = subprocess.run(
                [browser_path, "--version"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            detail = (version.stdout or version.stderr or "").strip()
            print(f"Chromium 版本: {detail or '无法读取'}")
        except (OSError, subprocess.SubprocessError) as exc:
            warnings.append(f"无法读取 Chromium 版本：{exc}")
        if not args.skip_browser_launch:
            _, runtime_version = check_builtin_chromium()
            print(f"Playwright 启动检查: 正常（Chromium {runtime_version}）")
    except Exception as exc:  # noqa: BLE001 - diagnostic boundary
        errors.append(f"Chromium 兼容检查失败：{exc}")

    secret_tool = shutil.which("secret-tool")
    print(f"Secret Service 工具: {secret_tool or '未安装'}")
    if not args.skip_secret_service:
        try:
            from integrated_client.online.secure import SecretServiceProtector

            protector = SecretServiceProtector()
            sample = b"intdemo-uos-preflight"
            if protector.unprotect(protector.protect(sample)) != sample:
                raise RuntimeError("加解密结果不一致")
            print("Secret Service 加密检查: 正常")
        except Exception as exc:  # noqa: BLE001 - diagnostic boundary
            errors.append(f"Secret Service 检查失败：{exc}")

    if warnings:
        print("\n警告:")
        for warning in warnings:
            print(f"  - {warning}")
    if errors:
        print("\n未通过:")
        for error in errors:
            print(f"  - {error}")
        return 1
    print("\n预检通过，可以构建 UOS ARM64 客户端。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

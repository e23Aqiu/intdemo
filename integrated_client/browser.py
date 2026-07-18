import os
import sys
from pathlib import Path


def configure_playwright_browser_path() -> None:
    """让源码运行和打包程序都使用随 Playwright 安装的 Chromium。"""
    if getattr(sys, "frozen", False):
        browser_dir = (
            Path(sys._MEIPASS)
            / "playwright"
            / "driver"
            / "package"
            / ".local-browsers"
        )
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(browser_dir)
    else:
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "0"


configure_playwright_browser_path()

from playwright.sync_api import sync_playwright  # noqa: E402


def get_builtin_chromium_path(playwright=None) -> str:
    """返回内置 Chromium 路径；未安装时给出可操作的错误信息。"""
    if playwright is None:
        with sync_playwright() as runtime:
            executable = Path(runtime.chromium.executable_path)
    else:
        executable = Path(playwright.chromium.executable_path)

    if not executable.is_file():
        raise RuntimeError(
            "未找到内置 Chromium。请在项目虚拟环境中执行：\n"
            "$env:PLAYWRIGHT_BROWSERS_PATH='0'; "
            "python -m playwright install chromium"
        )
    return str(executable)

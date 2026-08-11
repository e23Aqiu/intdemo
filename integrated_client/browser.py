import os
import shutil
import sys
from pathlib import Path

from .platform_support import chromium_launch_args


_BROWSER_ENV = "INTDEMO_CHROMIUM_PATH"
_LINUX_BROWSER_COMMANDS = (
    "chromium",
    "chromium-browser",
    "uos-browser",
    "uos-browser-stable",
    "deepin-browser",
    "deepin-browser-stable",
    "google-chrome-stable",
    "google-chrome",
)


def configure_playwright_browser_path() -> None:
    """Configure the platform-specific managed-browser location."""
    if os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        return
    if getattr(sys, "frozen", False):
        browser_dir = (
            Path(sys._MEIPASS)
            / "playwright"
            / "driver"
            / "package"
            / ".local-browsers"
        )
        if browser_dir.is_dir():
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(browser_dir)
    elif os.name == "nt":
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "0"
    elif sys.platform.startswith("linux"):
        configured_cache = os.environ.get(
            "INTDEMO_UOS_BROWSER_CACHE", ""
        ).strip()
        browser_cache = (
            Path(configured_cache).expanduser()
            if configured_cache
            else Path(__file__).resolve().parents[1]
            / ".playwright-uos-arm64"
        )
        if browser_cache.is_dir():
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(
                browser_cache.resolve()
            )


configure_playwright_browser_path()

from playwright.sync_api import sync_playwright  # noqa: E402


def _usable_browser(path):
    candidate = Path(str(path or "")).expanduser()
    if not candidate.is_file():
        return None
    if os.name != "nt" and not os.access(candidate, os.X_OK):
        return None
    return candidate.resolve()


def _configured_browser():
    value = os.environ.get(_BROWSER_ENV, "").strip()
    if not value:
        return None
    candidate = _usable_browser(value)
    if candidate is None:
        raise RuntimeError(
            f"{_BROWSER_ENV} 指向的浏览器不存在或不可执行：{value}"
        )
    return candidate


def _system_browser():
    if not sys.platform.startswith("linux"):
        return None
    for command in _LINUX_BROWSER_COMMANDS:
        resolved = shutil.which(command)
        candidate = _usable_browser(resolved)
        if candidate is not None:
            return candidate
    return None


def _playwright_browser(playwright):
    try:
        return _usable_browser(playwright.chromium.executable_path)
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
        return None


def _resolve_playwright_browser(playwright=None):
    if playwright is not None:
        return _playwright_browser(playwright)
    try:
        with sync_playwright() as runtime:
            return _playwright_browser(runtime)
    except Exception:
        return None


def get_builtin_chromium_path(playwright=None) -> str:
    """Resolve a compatible Chromium while retaining the public API name.

    Windows continues to prefer Playwright's bundled browser.  Linux prefers
    the project-managed browser after it has passed UOS compatibility checks,
    then falls back to a system Chromium.
    """
    configured = _configured_browser()
    if configured is not None:
        return str(configured)

    managed_browser = bool(
        os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
    )
    if sys.platform.startswith("linux") and managed_browser:
        executable = _resolve_playwright_browser(playwright)
        if executable is not None:
            return str(executable)

    if sys.platform.startswith("linux"):
        system_browser = _system_browser()
        if system_browser is not None:
            return str(system_browser)

    executable = _resolve_playwright_browser(playwright)
    if executable is not None:
        return str(executable)

    if not sys.platform.startswith("linux"):
        system_browser = _system_browser()
        if system_browser is not None:
            return str(system_browser)

    if sys.platform.startswith("linux"):
        raise RuntimeError(
            "未找到项目内置 Chromium。请先运行：\n"
            "bash scripts/uos-arm64/prepare-env.sh\n"
            "也可以显式指定兼容浏览器：\n"
            "export INTDEMO_CHROMIUM_PATH=/浏览器/可执行文件/路径"
        )
    raise RuntimeError(
        "未找到内置 Chromium。请在项目虚拟环境中执行：\n"
        "$env:PLAYWRIGHT_BROWSERS_PATH='0'; "
        "python -m playwright install chromium"
    )


def check_builtin_chromium() -> tuple:
    """实际启动一次已解析的 Chromium，返回路径和浏览器版本。"""
    with sync_playwright() as runtime:
        executable = get_builtin_chromium_path(runtime)
        browser = None
        try:
            browser = runtime.chromium.launch(
                headless=True,
                executable_path=executable,
                args=chromium_launch_args(),
            )
            page = browser.new_page()
            page.goto("about:blank", wait_until="load", timeout=10_000)
            return executable, browser.version
        except Exception as exc:
            raise RuntimeError(f"Chromium 启动检查失败：{exc}") from exc
        finally:
            if browser is not None:
                browser.close()

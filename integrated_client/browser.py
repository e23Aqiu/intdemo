import os
import configparser
import shlex
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

_WINDOWS_COMPATIBLE_BROWSER_NAMES = (
    "Microsoft Edge",
    "360 浏览器",
)

_UOS_BROWSER_DESKTOP_FILES = (
    "org.deepin.browser.desktop",
    "deepin-browser.desktop",
    "deepin-browser-stable.desktop",
    "uos-browser.desktop",
    "uos-browser-stable.desktop",
)


class CompatibleBrowserUnavailableError(RuntimeError):
    """Raised when 爱企查兼容模式 has no usable system browser."""


def _candidate_paths(*values):
    """Yield existing executable candidates without assuming one install layout."""
    for value in values:
        if value:
            candidate_path = Path(str(value)).expanduser()
            if not candidate_path.is_absolute():
                continue
            candidate = _usable_browser(candidate_path)
            if candidate is not None:
                yield candidate


def _linux_desktop_file_candidates():
    """Yield likely user/system desktop launchers for the UOS browser."""
    data_home = os.environ.get("XDG_DATA_HOME", "").strip()
    try:
        home = Path.home()
    except RuntimeError:
        home = None
    roots = [Path("/usr/local/share"), Path("/usr/share"), Path("/var/lib"), Path("/opt/apps")]
    if data_home:
        try:
            roots.insert(0, Path(data_home).expanduser())
        except RuntimeError:
            pass
    elif home is not None:
        roots.insert(0, home / ".local" / "share")
    relative_paths = tuple(
        relative
        for name in _UOS_BROWSER_DESKTOP_FILES
        for relative in (
            Path("applications") / name,
            Path("desktop-directories") / name,
            Path("entries") / "applications" / name,
        )
    )
    seen = set()
    for root in roots:
        for relative in relative_paths:
            candidate = root / relative
            if candidate.is_file() and str(candidate) not in seen:
                seen.add(str(candidate))
                yield candidate
    if home is None:
        return
    for desktop_dir in (home / "Desktop", home / "桌面"):
        for name in _UOS_BROWSER_DESKTOP_FILES:
            candidate = desktop_dir / name
            if candidate.is_file() and str(candidate) not in seen:
                seen.add(str(candidate))
                yield candidate

    opt_apps = Path("/opt/apps")
    if opt_apps.is_dir():
        for name in _UOS_BROWSER_DESKTOP_FILES:
            try:
                nested = opt_apps.glob(f"*/entries/applications/{name}")
            except OSError:
                continue
            for candidate in nested:
                if candidate.is_file() and str(candidate) not in seen:
                    seen.add(str(candidate))
                    yield candidate


def _desktop_exec_candidates(desktop_file):
    """Resolve executable tokens from a freedesktop ``.desktop`` launcher."""
    try:
        content = Path(desktop_file).read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError:
        return
    values = []
    parser = configparser.RawConfigParser(interpolation=None, strict=False)
    try:
        parser.read_string(content)
        if parser.has_section("Desktop Entry"):
            values.extend(
                parser.get("Desktop Entry", key, fallback="")
                for key in ("TryExec", "Exec")
            )
    except configparser.Error:
        for line in content.splitlines():
            key, separator, value = line.partition("=")
            if separator and key.strip() in {"Exec", "TryExec"}:
                values.append(value.strip())
    for value in values:
        try:
            tokens = shlex.split(value, posix=True)
        except ValueError:
            tokens = value.split()
        if not tokens:
            continue
        executable_index = 0
        if tokens[0] == "env":
            executable_index = 1
            while executable_index < len(tokens) and "=" in tokens[executable_index]:
                executable_index += 1
        if executable_index >= len(tokens):
            continue
        executable = tokens[executable_index]
        if executable.startswith("%"):
            continue
        candidate = _usable_browser(executable)
        if candidate is not None:
            yield candidate
            continue
        resolved = shutil.which(executable)
        candidate = _usable_browser(resolved)
        if candidate is not None:
            yield candidate


def _uos_browser_candidates():
    """Find UOS Deepin browser binaries, including desktop-launcher installs."""
    seen = set()
    for command in ("deepin-browser", "deepin-browser-stable"):
        candidate = _usable_browser(shutil.which(command))
        if candidate is not None and str(candidate) not in seen:
            seen.add(str(candidate))
            yield candidate
    for desktop_file in _linux_desktop_file_candidates():
        for candidate in _desktop_exec_candidates(desktop_file):
            if str(candidate) not in seen:
                seen.add(str(candidate))
                yield candidate


def get_compatible_browser_path() -> tuple[str, str]:
    """Resolve the browser used by 爱企查兼容模式.

    The returned tuple is ``(path, display_name)``. Windows prefers Edge and
    then 360; UOS uses the Deepin browser. Other platforms deliberately do not
    opt into this mode.
    """
    if sys.platform.startswith("win"):
        program_files = os.environ.get("ProgramFiles", "")
        program_w6432 = os.environ.get("ProgramW6432", "")
        program_files_x86 = os.environ.get("ProgramFiles(x86)", "")
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        roaming_app_data = os.environ.get("APPDATA", "")
        edge_candidates = tuple(
            Path(root) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
            for root in (
                program_w6432,
                program_files,
                program_files_x86,
                local_app_data,
            )
            if root
        )
        for candidate in _candidate_paths(*edge_candidates):
            return str(candidate), _WINDOWS_COMPATIBLE_BROWSER_NAMES[0]
        candidate = _usable_browser(shutil.which("msedge.exe"))
        if candidate is not None:
            return str(candidate), _WINDOWS_COMPATIBLE_BROWSER_NAMES[0]

        browser_roots = tuple(
            root
            for root in (
                program_w6432,
                program_files,
                program_files_x86,
                local_app_data,
                roaming_app_data,
            )
            if root
        )
        browser_360_candidates = tuple(
            Path(root) / relative
            for root in browser_roots
            for relative in (
                Path("360") / "360se6" / "Application" / "360se.exe",
                Path("360") / "360se" / "360se.exe",
                Path("360se6") / "Application" / "360se.exe",
                Path("360") / "360Chrome" / "Chrome" / "Application" / "360chrome.exe",
                Path("360Chrome") / "Chrome" / "Application" / "360chrome.exe",
                Path("360ChromeX") / "Chrome" / "Application" / "360chrome.exe",
                Path("360") / "360ChromeX" / "Chrome" / "Application" / "360chrome.exe",
                Path("360ChromeX") / "Chrome" / "Application" / "360ChromeX.exe",
                Path("360") / "360ChromeX" / "Chrome" / "Application" / "360ChromeX.exe",
            )
        )
        for candidate in _candidate_paths(*browser_360_candidates):
            return str(candidate), _WINDOWS_COMPATIBLE_BROWSER_NAMES[1]
        for command in ("360se.exe", "360chrome.exe", "360ChromeX.exe"):
            candidate = _usable_browser(shutil.which(command))
            if candidate is not None:
                return str(candidate), _WINDOWS_COMPATIBLE_BROWSER_NAMES[1]
        raise CompatibleBrowserUnavailableError(
            "未找到本机兼容浏览器（优先 Microsoft Edge，其次 360 浏览器）。"
            "请关闭“爱企查兼容模式”后重试。"
        )

    if sys.platform.startswith("linux"):
        for candidate in _uos_browser_candidates():
            return str(candidate), "统信 Deepin 浏览器"
        raise CompatibleBrowserUnavailableError(
            "未找到统信 Deepin 浏览器。请关闭“爱企查兼容模式”后重试。"
        )

    raise CompatibleBrowserUnavailableError(
        "当前系统不支持爱企查兼容模式，请关闭该模式后重试。"
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

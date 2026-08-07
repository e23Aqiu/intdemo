"""Small, dependency-free helpers for desktop platform compatibility."""

import os
import platform
import shutil
import sys
from pathlib import Path, PurePosixPath


WINDOWS_UPDATE_PLATFORM = "windows-x86_64"
UOS_UPDATE_PLATFORM = "linux-aarch64"


def _is_system_qt_plugin_path(value):
    """Return whether *value* exposes a distro Qt 5 plugin directory.

    The UOS desktop plugins are built against the distro Qt 5.11 runtime.  The
    client bundles Qt 5.15, so allowing either runtime to discover the other
    runtime's plugins can crash inside QApplication before Python can report an
    exception.  App-owned plugin roots remain supported; only conventional
    system library trees are rejected.
    """
    normalized = str(PurePosixPath(str(value).strip()))
    if not normalized.startswith("/"):
        return False
    system_library_roots = (
        "/lib",
        "/lib64",
        "/usr/lib",
        "/usr/lib64",
        "/usr/local/lib",
    )
    if not any(
        normalized == root or normalized.startswith(f"{root}/")
        for root in system_library_roots
    ):
        return False
    parts = tuple(part.casefold() for part in PurePosixPath(normalized).parts)
    return any(
        parts[index] == "qt5" and parts[index + 1] == "plugins"
        for index in range(len(parts) - 1)
    )


def _isolate_bundled_qt_plugins():
    """Remove inherited paths that would mix UOS Qt plugins into bundled Qt."""
    for variable in ("QT_PLUGIN_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH"):
        configured_paths = [
            item for item in os.environ.get(variable, "").split(os.pathsep) if item
        ]
        safe_paths = [
            item for item in configured_paths if not _is_system_qt_plugin_path(item)
        ]
        if safe_paths:
            os.environ[variable] = os.pathsep.join(safe_paths)
        else:
            os.environ.pop(variable, None)


def normalized_machine(value=None):
    machine = str(value or platform.machine() or "").strip().casefold()
    return "aarch64" if machine in {"aarch64", "arm64"} else machine


def is_linux_arm64():
    return sys.platform.startswith("linux") and normalized_machine() == "aarch64"


def is_uos():
    if not sys.platform.startswith("linux"):
        return False
    for candidate in (Path("/etc/os-version"), Path("/etc/os-release")):
        try:
            content = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        normalized = content.casefold()
        if "systemname=uos" in normalized or "uniontech" in normalized:
            return True
    return False


def configure_desktop_environment():
    """Select conservative defaults for Qt on an ARM64 Wayland desktop.

    The conda-forge Qt 5 build always contains the XCB plugin, while a Wayland
    plugin is not guaranteed to be present on older UOS installations.  UOS
    Desktop provides XWayland, so XCB is the safest default.  UOS may inject
    ``QT_QPA_PLATFORM=wayland`` even when the Qt Wayland plugin is absent;
    only the project-specific override requests native Wayland deliberately.
    """
    if not is_linux_arm64():
        return
    _isolate_bundled_qt_plugins()
    os.environ.setdefault("QT_AUTO_SCREEN_SCALE_FACTOR", "1")
    if not os.environ.get("QT_IM_MODULE", "").strip():
        input_method_hint = " ".join(
            (
                os.environ.get("GTK_IM_MODULE", ""),
                os.environ.get("XMODIFIERS", ""),
                os.environ.get("INPUT_METHOD", ""),
            )
        ).casefold()
        # UOS uses Fcitx/Fcitx5 by default.  Qt 5 expects the module name
        # ``fcitx`` for both generations; preserve an explicit IBus session.
        os.environ["QT_IM_MODULE"] = (
            "ibus" if "ibus" in input_method_hint else "fcitx"
        )
    explicit_platform = os.environ.get(
        "INTDEMO_QT_QPA_PLATFORM", ""
    ).strip()
    if explicit_platform:
        os.environ["QT_QPA_PLATFORM"] = explicit_platform
    if (
        os.environ.get("XDG_SESSION_TYPE", "").casefold() == "wayland"
        and os.environ.get("DISPLAY")
        and not explicit_platform
        and os.environ.get("QT_QPA_PLATFORM", "").strip().casefold()
        in {"", "wayland", "wayland-egl"}
    ):
        os.environ["QT_QPA_PLATFORM"] = "xcb"


def chromium_launch_args():
    """Return Chromium flags that are safe on UOS and no-ops elsewhere."""
    if not sys.platform.startswith("linux"):
        return []

    arguments = ["--disable-dev-shm-usage"]
    ozone_platform = os.environ.get("INTDEMO_CHROMIUM_OZONE_PLATFORM", "").strip()
    if not ozone_platform and os.environ.get(
        "XDG_SESSION_TYPE", ""
    ).casefold() == "wayland":
        ozone_platform = "x11" if os.environ.get("DISPLAY") else "wayland"
    if ozone_platform and ozone_platform.casefold() != "auto":
        arguments.append(f"--ozone-platform={ozone_platform}")
    return arguments


def supports_self_update():
    """Return whether this runtime has a supported unattended installer path."""
    target = update_platform_key()
    return target == WINDOWS_UPDATE_PLATFORM or (
        target == UOS_UPDATE_PLATFORM and is_uos()
    )


def update_platform_key(system=None, machine=None):
    """Return the update-manifest target for this desktop runtime."""
    platform_name = str(system or sys.platform or "").strip().casefold()
    architecture = normalized_machine(machine)
    if platform_name.startswith("win") or (system is None and os.name == "nt"):
        if architecture in {"amd64", "x86_64"}:
            return WINDOWS_UPDATE_PLATFORM
    if platform_name.startswith("linux") and architecture == "aarch64":
        return UOS_UPDATE_PLATFORM
    return ""


def update_install_command(package_path):
    """Return a safe detached installer command for a verified update package."""
    path = Path(package_path).expanduser().resolve()
    target = update_platform_key()
    if target == WINDOWS_UPDATE_PLATFORM and path.suffix.casefold() == ".exe":
        return str(path), ["/SP-", "/CLOSEAPPLICATIONS"]
    if target == UOS_UPDATE_PLATFORM and path.suffix.casefold() == ".deb":
        # UOS ships Deepin Package Manager as its desktop DEB installer. It
        # presents package details and obtains administrator authorization
        # through the desktop PolicyKit agent. Starting pkexec directly from
        # a detached GUI process can exit without ever showing that prompt.
        graphical_installer = shutil.which("deepin-deb-installer")
        if graphical_installer:
            return graphical_installer, [str(path)]
        opener = shutil.which("xdg-open")
        if opener:
            return opener, [str(path)]
        pkexec = shutil.which("pkexec")
        dpkg = shutil.which("dpkg")
        if pkexec and dpkg:
            return pkexec, [dpkg, "--install", str(path)]
        raise RuntimeError(
            "未找到 deepin-deb-installer、xdg-open 或 pkexec/dpkg，"
            "无法启动 UOS 更新安装包"
        )
    raise RuntimeError("当前平台或更新包格式不支持自动安装")


def update_install_environment(environment=None):
    """Return an environment safe for launching a system-owned installer.

    PyInstaller temporarily prepends its bundled libraries to
    ``LD_LIBRARY_PATH``. Passing that environment to UOS's Qt-based package
    installer can make the system application load the client's bundled Qt
    libraries and terminate before its window appears.
    """
    source = os.environ if environment is None else environment
    cleaned = {str(key): str(value) for key, value in source.items()}
    if update_platform_key() != UOS_UPDATE_PLATFORM:
        return cleaned

    original_library_path = cleaned.pop("LD_LIBRARY_PATH_ORIG", None)
    if original_library_path:
        cleaned["LD_LIBRARY_PATH"] = original_library_path
    else:
        cleaned.pop("LD_LIBRARY_PATH", None)
    for variable in (
        "QT_PLUGIN_PATH",
        "QT_QPA_PLATFORM_PLUGIN_PATH",
        "QT_QPA_PLATFORM",
        "QML_IMPORT_PATH",
        "QML2_IMPORT_PATH",
        "QTWEBENGINEPROCESS_PATH",
        "QTWEBENGINE_RESOURCES_PATH",
        "QTWEBENGINE_LOCALES_PATH",
    ):
        cleaned.pop(variable, None)
    return cleaned

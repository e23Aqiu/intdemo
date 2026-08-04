"""Small, dependency-free helpers for desktop platform compatibility."""

import os
import platform
import sys
from pathlib import Path


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
    os.environ.setdefault("QT_AUTO_SCREEN_SCALE_FACTOR", "1")
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
    """The published update channel currently contains Windows installers."""
    return os.name == "nt"

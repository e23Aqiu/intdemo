"""Cross-platform file dialogs with a UOS desktop-native preference."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from PyQt5.QtWidgets import QFileDialog as QtFileDialog

from ..platform_support import is_uos


def _zenity_filters(file_filter: str) -> list[str]:
    filters: list[str] = []
    for entry in str(file_filter or "").split(";;"):
        normalized = entry.strip()
        if not normalized:
            continue
        if "(" in normalized and normalized.endswith(")"):
            label, patterns = normalized.rsplit("(", 1)
            patterns = patterns[:-1].strip()
            label = label.strip()
            filters.append(f"{label} | {patterns}" if label else patterns)
        else:
            filters.append(normalized)
    return filters


def _initial_path(value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        return ""
    path = Path(normalized).expanduser()
    if path.is_dir() and not normalized.endswith(os.sep):
        return str(path) + os.sep
    return str(path)


def _uos_file_selection(
    parent,
    caption: str,
    directory: str,
    file_filter: str,
    *,
    multiple: bool = False,
    save: bool = False,
) -> list[str] | None:
    """Return selected paths, ``[]`` for cancel, or ``None`` for Qt fallback."""
    if not is_uos():
        return None
    program = shutil.which("zenity")
    if not program:
        return None

    # Keep the native chooser modal so the Qt parent cannot cover it while
    # the synchronous selection call is waiting for a result.
    arguments = [
        program,
        "--file-selection",
        "--modal",
        f"--title={caption or '选择文件'}",
    ]
    start = _initial_path(directory)
    if start:
        arguments.append(f"--filename={start}")
    if multiple:
        arguments.extend(("--multiple", "--separator=\n"))
    if save:
        arguments.extend(("--save", "--confirm-overwrite"))
    for item in _zenity_filters(file_filter):
        arguments.append(f"--file-filter={item}")

    if parent is not None and os.environ.get("DISPLAY"):
        try:
            window_id = int(parent.window().winId())
        except (AttributeError, RuntimeError, TypeError, ValueError):
            window_id = 0
        if window_id > 0:
            arguments.append(f"--attach={window_id}")

    environment = os.environ.copy()
    environment.setdefault("GTK_USE_PORTAL", "1")
    try:
        result = subprocess.run(
            arguments,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
        )
    except OSError:
        return None
    if result.returncode == 1:
        return []
    if result.returncode != 0:
        return None
    return [line for line in result.stdout.splitlines() if line]


class SystemFileDialog:
    """QFileDialog-compatible facade using the UOS system chooser when possible."""

    @staticmethod
    def getOpenFileName(
        parent=None,
        caption="",
        directory="",
        filter="",
        initialFilter="",
        options=None,
    ):
        selected = _uos_file_selection(parent, caption, directory, filter)
        if selected is not None:
            return (selected[0] if selected else "", "")
        arguments = [parent, caption, directory, filter, initialFilter]
        if options is not None:
            arguments.append(options)
        return QtFileDialog.getOpenFileName(*arguments)

    @staticmethod
    def getOpenFileNames(
        parent=None,
        caption="",
        directory="",
        filter="",
        initialFilter="",
        options=None,
    ):
        selected = _uos_file_selection(
            parent,
            caption,
            directory,
            filter,
            multiple=True,
        )
        if selected is not None:
            return selected, ""
        arguments = [parent, caption, directory, filter, initialFilter]
        if options is not None:
            arguments.append(options)
        return QtFileDialog.getOpenFileNames(*arguments)

    @staticmethod
    def getSaveFileName(
        parent=None,
        caption="",
        directory="",
        filter="",
        initialFilter="",
        options=None,
    ):
        selected = _uos_file_selection(
            parent,
            caption,
            directory,
            filter,
            save=True,
        )
        if selected is not None:
            return (selected[0] if selected else "", "")
        arguments = [parent, caption, directory, filter, initialFilter]
        if options is not None:
            arguments.append(options)
        return QtFileDialog.getSaveFileName(*arguments)

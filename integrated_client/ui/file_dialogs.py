"""Cross-platform file dialogs with a UOS desktop-native preference."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from PyQt5.QtCore import (
    QCoreApplication,
    QEvent,
    QEventLoop,
    QObject,
    QProcess,
    QProcessEnvironment,
    QThread,
    Qt,
)
from PyQt5.QtWidgets import QApplication
from PyQt5.QtWidgets import QFileDialog as QtFileDialog

from ..platform_support import is_uos, system_application_environment


class _ApplicationInputBlocker(QObject):
    """Discard input aimed at the client while an external chooser is open."""

    _blocked_event_types = frozenset(
        event_type
        for event_type in (
            QEvent.Close,
            QEvent.ContextMenu,
            QEvent.DragEnter,
            QEvent.DragLeave,
            QEvent.DragMove,
            QEvent.Drop,
            QEvent.KeyPress,
            QEvent.KeyRelease,
            QEvent.MouseButtonDblClick,
            QEvent.MouseButtonPress,
            QEvent.MouseButtonRelease,
            QEvent.MouseMove,
            getattr(QEvent, "NonClientAreaMouseButtonDblClick", None),
            getattr(QEvent, "NonClientAreaMouseButtonPress", None),
            getattr(QEvent, "NonClientAreaMouseButtonRelease", None),
            getattr(QEvent, "NonClientAreaMouseMove", None),
            QEvent.Shortcut,
            QEvent.ShortcutOverride,
            QEvent.TabletMove,
            QEvent.TabletPress,
            QEvent.TabletRelease,
            QEvent.TouchBegin,
            QEvent.TouchCancel,
            QEvent.TouchEnd,
            QEvent.TouchUpdate,
            QEvent.Wheel,
        )
        if event_type is not None
    )

    def eventFilter(self, watched, event):
        if event.type() in self._blocked_event_types:
            return True
        return super().eventFilter(watched, event)


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


def _find_zenity():
    """Find the distro chooser even when a desktop launcher has a short PATH."""
    program = shutil.which("zenity")
    if program:
        return program
    for candidate in ("/usr/bin/zenity", "/bin/zenity", "/usr/local/bin/zenity"):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def _run_process_blocking(
    arguments: list[str],
    environment: dict[str, str],
    *,
    nested: bool = True,
):
    """Run Zenity without blocking delivery and rejection of client input."""
    application = QApplication.instance()
    # Calls without a parent are also used by headless helpers/tests. Keep
    # those on the simple subprocess path; there is no client window whose
    # event queue needs to be guarded.
    if not nested:
        application = None
    if application is None or QThread.currentThread() is not application.thread():
        try:
            completed = subprocess.run(
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
        return completed.returncode, completed.stdout

    process = QProcess()
    process.setProgram(arguments[0])
    process.setArguments(arguments[1:])
    process_environment = QProcessEnvironment()
    for name, value in environment.items():
        process_environment.insert(str(name), str(value))
    process.setProcessEnvironment(process_environment)

    event_loop = QEventLoop()
    process.finished.connect(lambda *_args: event_loop.quit())
    process.errorOccurred.connect(lambda *_args: event_loop.quit())

    input_blocker = _ApplicationInputBlocker()
    enabled_windows = [
        window
        for window in application.topLevelWidgets()
        if window.isVisible() and window.isEnabled()
    ]
    application.installEventFilter(input_blocker)
    for window in enabled_windows:
        window.setEnabled(False)

    try:
        process.start()
        if not process.waitForStarted(3000):
            return None
        if process.state() != QProcess.NotRunning:
            event_loop.exec_()
        if process.exitStatus() != QProcess.NormalExit:
            return None
        output = bytes(process.readAllStandardOutput()).decode(
            "utf-8", errors="replace"
        )
        return process.exitCode(), output
    finally:
        # Drain native pointer/key events while the application is still
        # blocked. Otherwise an X11 click can reach a button after the chooser
        # closes and look like a delayed click.
        QCoreApplication.sendPostedEvents()
        application.processEvents(QEventLoop.AllEvents)
        for window in enabled_windows:
            window.setEnabled(True)
        # Keep filtering through the first event flush after re-enabling the
        # client so the chooser's final mouse release cannot activate it.
        QCoreApplication.sendPostedEvents()
        application.processEvents(QEventLoop.AllEvents)
        application.removeEventFilter(input_blocker)


def _parent_window_id(parent) -> int:
    if parent is None or not os.environ.get("DISPLAY"):
        return 0
    try:
        return int(parent.window().winId())
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return 0


def _qt_uos_file_selection(
    parent,
    caption: str,
    directory: str,
    file_filter: str,
    *,
    initial_filter: str = "",
    options=None,
    multiple: bool = False,
    save: bool = False,
) -> tuple[list[str], str]:
    """Use a predictable in-process modal chooser when Zenity is unavailable."""
    dialog = QtFileDialog(parent, caption, directory, file_filter)
    try:
        if options is not None:
            dialog.setOptions(options)
        # Native UOS dialogs do not consistently honour Qt modality/window
        # hints. The Qt implementation does, and is only used as a fallback.
        dialog.setOption(QtFileDialog.DontUseNativeDialog, True)
        dialog.setWindowModality(Qt.ApplicationModal)
        dialog.setModal(True)
        dialog.setWindowFlags(
            dialog.windowFlags()
            | Qt.Dialog
            | Qt.WindowStaysOnTopHint
        )
        if initial_filter:
            dialog.selectNameFilter(initial_filter)
        if save:
            dialog.setAcceptMode(QtFileDialog.AcceptSave)
            dialog.setFileMode(QtFileDialog.AnyFile)
        elif multiple:
            dialog.setFileMode(QtFileDialog.ExistingFiles)
        else:
            dialog.setFileMode(QtFileDialog.ExistingFile)
        if not dialog.exec_():
            return [], ""
        return dialog.selectedFiles(), dialog.selectedNameFilter()
    finally:
        dialog.deleteLater()


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
    program = _find_zenity()
    if not program:
        return None

    # Attach to the Qt window when X11 exposes a native id. Pure Wayland and
    # restricted desktop launchers still use the portal chooser without an id.
    window_id = _parent_window_id(parent)
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

    if window_id > 0:
        arguments.append(f"--attach={window_id}")

    environment = system_application_environment()
    if window_id > 0:
        # The portal creates a separate chooser whose transient parent is not
        # guaranteed to survive the GTK/Qt boundary. Keep Zenity on X11 so
        # --attach is honoured by the UOS window manager.
        environment["GDK_BACKEND"] = "x11"
        environment["GTK_USE_PORTAL"] = "0"
    else:
        environment.setdefault("GTK_USE_PORTAL", "1")
    result = _run_process_blocking(
        arguments,
        environment,
        nested=parent is not None,
    )
    if result is None:
        return None
    returncode, output = result
    if returncode == 1:
        return []
    if returncode != 0:
        return None
    return [line for line in output.splitlines() if line]


def _uos_selection_or_fallback(
    parent,
    caption,
    directory,
    file_filter,
    initial_filter,
    options,
    *,
    multiple=False,
    save=False,
):
    selected = _uos_file_selection(
        parent,
        caption,
        directory,
        file_filter,
        multiple=multiple,
        save=save,
    )
    if selected is not None:
        return selected, ""
    return _qt_uos_file_selection(
        parent,
        caption,
        directory,
        file_filter,
        initial_filter=initial_filter,
        options=options,
        multiple=multiple,
        save=save,
    )


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
        if is_uos():
            selected, selected_filter = _uos_selection_or_fallback(
                parent, caption, directory, filter, initialFilter, options
            )
            return (selected[0] if selected else "", selected_filter)
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
        if is_uos():
            return _uos_selection_or_fallback(
                parent,
                caption,
                directory,
                filter,
                initialFilter,
                options,
                multiple=True,
            )
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
        if is_uos():
            selected, selected_filter = _uos_selection_or_fallback(
                parent,
                caption,
                directory,
                filter,
                initialFilter,
                options,
                save=True,
            )
            return (selected[0] if selected else "", selected_filter)
        arguments = [parent, caption, directory, filter, initialFilter]
        if options is not None:
            arguments.append(options)
        return QtFileDialog.getSaveFileName(*arguments)

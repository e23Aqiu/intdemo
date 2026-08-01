from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PyQt5.QtCore import QObject, QRunnable, Qt, QThreadPool, pyqtSignal
from PyQt5.QtWidgets import QLabel, QProgressBar, QVBoxLayout

from .frameless import FramelessDialog


class _CallSignals(QObject):
    finished = pyqtSignal(object, object)


class _CallTask(QRunnable):
    def __init__(self, function: Callable[[], Any]):
        super().__init__()
        self.function = function
        self.signals = _CallSignals()

    def run(self):
        try:
            result = self.function()
        except Exception as exc:  # noqa: BLE001 - crosses the Qt worker boundary
            self.signals.finished.emit(None, exc)
        else:
            self.signals.finished.emit(result, None)


class LoadingDialog(FramelessDialog):
    """Small non-dismissible modal shown while a server request is running."""

    def __init__(self, message: str, parent=None):
        super().__init__(parent)
        self._finished = False
        self.setWindowTitle(message)
        self.setModal(True)
        self.setFixedSize(350, 150)
        self.window_controls.close_button.hide()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 30, 30, 26)
        layout.setSpacing(16)
        self.message_label = QLabel(message)
        self.message_label.setObjectName("LoadingMessage")
        self.message_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.message_label)
        self.progress = QProgressBar()
        self.progress.setObjectName("LoadingProgress")
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        layout.addWidget(self.progress)

    def finish(self):
        self._finished = True
        self.accept()

    def reject(self):
        if self._finished:
            super().reject()

    def closeEvent(self, event):
        if self._finished:
            event.accept()
        else:
            event.ignore()


def run_with_loading(
    parent,
    message: str,
    function: Callable[[], Any],
) -> Any:
    """Run a bounded server call in a worker while keeping the UI responsive."""

    dialog = LoadingDialog(message, parent)
    task = _CallTask(function)
    outcome: dict[str, Any] = {}

    def completed(result, error):
        outcome["result"] = result
        outcome["error"] = error
        dialog.finish()

    task.signals.finished.connect(completed)
    QThreadPool.globalInstance().start(task)
    dialog.exec_()
    error = outcome.get("error")
    if error is not None:
        raise error
    return outcome.get("result")

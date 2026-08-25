from __future__ import annotations

import threading

from PyQt5.QtCore import QObject, QRunnable, QThreadPool, QTimer, pyqtSignal

from ..diagnostics import get_logger
from .update import UpdateCancelled, UpdateClient


class _UpdateTask(QRunnable):
    def __init__(self, coordinator, action, update=None):
        super().__init__()
        self.coordinator = coordinator
        self.action = action
        self.update = update

    def run(self):
        try:
            if self.action == "check":
                result = self.coordinator.client.check()
            else:
                result = self.coordinator.client.download(
                    self.update,
                    progress_callback=self.coordinator.download_progress.emit,
                    speed_callback=self.coordinator.download_speed.emit,
                    cancelled_callback=self.coordinator._download_cancel.is_set,
                    state_callback=self.coordinator.state_changed.emit,
                )
            self.coordinator._task_finished.emit(self.action, result, "")
        except UpdateCancelled:
            self.coordinator._task_finished.emit("download_cancelled", None, "")
        except Exception as exc:  # noqa: BLE001 - protects Qt worker boundary
            get_logger().exception("Update %s failed", self.action)
            self.coordinator._task_finished.emit(
                self.action,
                None,
                str(exc),
            )


class UpdateCoordinator(QObject):
    update_available = pyqtSignal(object)
    state_changed = pyqtSignal(str, str)
    download_progress = pyqtSignal(object, object)
    download_speed = pyqtSignal(object)
    download_completed = pyqtSignal(object)
    _task_finished = pyqtSignal(str, object, str)

    def __init__(self, client: UpdateClient, parent=None):
        super().__init__(parent)
        self.client = client
        self._pool = QThreadPool.globalInstance()
        self._busy = False
        self._download_active = False
        self._download_cancel = threading.Event()
        self._manual_check = False
        self._stopped = True
        self._initial_timer = QTimer(self)
        self._initial_timer.setSingleShot(True)
        self._initial_timer.setInterval(3000)
        self._initial_timer.timeout.connect(self.check)
        self._periodic_timer = QTimer(self)
        self._periodic_timer.setInterval(6 * 60 * 60 * 1000)
        self._periodic_timer.timeout.connect(self.check)
        self._task_finished.connect(self._on_task_finished)

    @property
    def download_active(self) -> bool:
        return self._download_active

    def start(self):
        self._stopped = False
        self._initial_timer.start()
        self._periodic_timer.start()

    def stop(self):
        self._stopped = True
        self._initial_timer.stop()
        self._periodic_timer.stop()
        self.cancel_download()

    def check(self, manual=False):
        if self._stopped or self._busy:
            return False
        self._busy = True
        self._manual_check = bool(manual)
        if self._manual_check:
            self.state_changed.emit("checking", "正在检查更新…")
        self._pool.start(_UpdateTask(self, "check"))
        return True

    def download(self, update):
        if self._stopped or self._busy:
            return False
        self._busy = True
        self._download_active = True
        self._download_cancel.clear()
        package_label = "增量更新包" if update.is_delta else "完整更新包"
        self.state_changed.emit(
            "downloading",
            f"正在下载 v{update.version} {package_label}；下载期间可以继续使用程序。",
        )
        self._pool.start(_UpdateTask(self, "download", update))
        return True

    def cancel_download(self) -> bool:
        if not self._download_active:
            return False
        self._download_cancel.set()
        self.state_changed.emit("cancelling", "正在停止更新下载…")
        return True

    def _on_task_finished(self, action, result, error):
        self._busy = False
        if action in {"download", "download_cancelled"}:
            self._download_active = False
            self._download_cancel.clear()
        if self._stopped:
            return
        if action == "download_cancelled":
            self.state_changed.emit(
                "download_cancelled",
                "更新下载已停止，可稍后继续重新下载。",
            )
            return
        if error:
            if action == "check":
                if self._manual_check:
                    self.state_changed.emit("check_error", error)
                self._manual_check = False
            else:
                self.state_changed.emit("download_error", error)
            return
        if action == "check":
            if result is not None:
                self.update_available.emit(result)
            elif self._manual_check:
                self.state_changed.emit(
                    "up_to_date",
                    "当前已是最新版本。",
                )
            self._manual_check = False
        else:
            self.state_changed.emit("downloaded", "更新包下载并校验完成")
            self.download_completed.emit(result)

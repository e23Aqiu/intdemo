from __future__ import annotations

from PyQt5.QtCore import QObject, QRunnable, QThreadPool, QTimer, pyqtSignal

from ..diagnostics import get_logger
from .update import UpdateClient


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
                result = self.coordinator.client.download(self.update)
            self.coordinator._task_finished.emit(self.action, result, "")
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
    download_completed = pyqtSignal(object)
    _task_finished = pyqtSignal(str, object, str)

    def __init__(self, client: UpdateClient, parent=None):
        super().__init__(parent)
        self.client = client
        self._pool = QThreadPool.globalInstance()
        self._busy = False
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

    def start(self):
        self._stopped = False
        self._initial_timer.start()
        self._periodic_timer.start()

    def stop(self):
        self._stopped = True
        self._initial_timer.stop()
        self._periodic_timer.stop()

    def check(self, manual=False):
        if self._stopped or self._busy:
            return
        self._busy = True
        self._manual_check = bool(manual)
        if self._manual_check:
            self.state_changed.emit("checking", "正在检查更新…")
        self._pool.start(_UpdateTask(self, "check"))

    def download(self, update):
        if self._stopped or self._busy:
            return
        self._busy = True
        self.state_changed.emit("downloading", f"正在下载 v{update.version}…")
        self._pool.start(_UpdateTask(self, "download", update))

    def _on_task_finished(self, action, result, error):
        self._busy = False
        if self._stopped:
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
                    "当前已是最新版本",
                )
            self._manual_check = False
        else:
            self.state_changed.emit("downloaded", "更新包下载并校验完成")
            self.download_completed.emit(result)

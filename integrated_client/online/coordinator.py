from __future__ import annotations

from PyQt5.QtCore import QObject, QRunnable, QThreadPool, QTimer, QUrl, pyqtSignal
from PyQt5.QtNetwork import (
    QAbstractSocket,
    QNetworkRequest,
    QSslCertificate,
    QSslConfiguration,
)
from PyQt5.QtWebSockets import QWebSocket, QWebSocketProtocol

from ..diagnostics import get_logger
from .api import ApiResponseError, NetworkUnavailable


class _SyncTask(QRunnable):
    def __init__(self, coordinator):
        super().__init__()
        self.coordinator = coordinator

    def run(self):
        try:
            status = self.coordinator.engine.run_once()
        except Exception as exc:  # noqa: BLE001 - protects Qt from worker failures
            get_logger().exception("Unhandled synchronization worker error")
            status = self.coordinator.engine.status(
                "error",
                f"同步线程异常：{exc}",
            )
        self.coordinator._worker_finished.emit(status)


class SyncCoordinator(QObject):
    status_changed = pyqtSignal(object)
    data_changed = pyqtSignal()
    _worker_finished = pyqtSignal(object)

    def __init__(self, engine, parent=None):
        super().__init__(parent)
        self.engine = engine
        self._running = False
        self._rerun_requested = False
        self._stopped = True
        self._pool = QThreadPool.globalInstance()
        self._worker_finished.connect(self._on_worker_finished)

        self._push_timer = QTimer(self)
        self._push_timer.setInterval(5000)
        self._push_timer.timeout.connect(self._on_push_timer)
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(30000)
        self._poll_timer.timeout.connect(self.request_sync)
        self._ws_retry_timer = QTimer(self)
        self._ws_retry_timer.setSingleShot(True)
        self._ws_retry_timer.setInterval(5000)
        self._ws_retry_timer.timeout.connect(self._connect_websocket)

        self._websocket = QWebSocket(
            "",
            QWebSocketProtocol.VersionLatest,
            self,
        )
        self._websocket.textMessageReceived.connect(self._on_websocket_message)
        self._websocket.disconnected.connect(self._on_websocket_disconnected)

    def start(self):
        self._stopped = False
        self._push_timer.start()
        self._poll_timer.start()
        # The first synchronization may rotate an expired refresh token.
        # Connect WebSocket only after that worker completes so two threads
        # never try to refresh the same session at the same time.
        self.request_sync()

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def is_stopped(self) -> bool:
        return self._stopped

    def stop(self):
        self._stopped = True
        self._rerun_requested = False
        self._push_timer.stop()
        self._poll_timer.stop()
        self._ws_retry_timer.stop()
        self._websocket.close()

    def _on_push_timer(self):
        if self.engine.database.get_due_sync_items(limit=1):
            self.request_sync()

    def request_sync(self):
        if self._stopped:
            return
        if self._running:
            self._rerun_requested = True
            return
        self._running = True
        self.status_changed.emit(self.engine.status("syncing"))
        self._pool.start(_SyncTask(self))

    def retry_now(self):
        state = self.engine.session.state
        account_id = (
            state.account.server_account_id
            if state is not None
            else None
        )
        self.engine.database.make_sync_retries_due(account_id)
        self.request_sync()

    def pause_for_reauthentication(self) -> bool:
        if self._running:
            return False
        self.stop()
        return True

    def resume_after_reauthentication(self):
        state = self.engine.session.state
        if state is None or not state.is_online:
            return
        account_id = state.account.server_account_id
        self.engine.database.make_sync_retries_due(account_id)
        if self._stopped:
            self.start()
        else:
            self.request_sync()

    def _on_worker_finished(self, status):
        self._running = False
        self.status_changed.emit(status)
        if status.state == "reauth_required":
            self.stop()
            return
        if status.state == "online":
            self.data_changed.emit()
            if self._websocket.state() == QAbstractSocket.UnconnectedState:
                self._connect_websocket()
        if self._rerun_requested and not self._stopped:
            self._rerun_requested = False
            QTimer.singleShot(0, self.request_sync)

    def _connect_websocket(self):
        if self._stopped or not self.engine.session.state:
            return
        try:
            token = self.engine.session.access_token()
            request = QNetworkRequest(
                QUrl(self.engine.session.api.config.websocket_url)
            )
            request.setRawHeader(
                b"Authorization",
                f"Bearer {token}".encode("ascii"),
            )
            ca_bundle = self.engine.session.api.config.ca_bundle
            if ca_bundle:
                certificates = QSslCertificate.fromPath(ca_bundle)
                if not certificates:
                    self.status_changed.emit(
                        self.engine.status(
                            "error",
                            "无法加载 WebSocket CA 根证书",
                        )
                    )
                    return
                ssl_configuration = QSslConfiguration.defaultConfiguration()
                ssl_configuration.setCaCertificates(
                    ssl_configuration.caCertificates() + certificates
                )
                request.setSslConfiguration(ssl_configuration)
            self._websocket.open(request)
        except (NetworkUnavailable, ApiResponseError) as exc:
            offline = (
                isinstance(exc, NetworkUnavailable)
                or exc.retryable
                or exc.status_code >= 500
            )
            if offline:
                note_offline = getattr(self.engine.session, "note_offline", None)
                if callable(note_offline):
                    note_offline()
            self.status_changed.emit(
                self.engine.status(
                    "offline" if offline else "error",
                    str(exc),
                )
            )
            return
        except Exception as exc:  # noqa: BLE001 - Qt slots must not leak exceptions
            get_logger().exception("Could not connect WebSocket")
            self.status_changed.emit(
                self.engine.status("error", f"WebSocket 连接失败：{exc}")
            )
            return

    def _on_websocket_message(self, message):
        if (
            "revision_changed" in message
            or '"type":"connected"' in message
            or "test_connection_blocked" in message
        ):
            self.request_sync()

    def _on_websocket_disconnected(self):
        if not self._stopped:
            self._ws_retry_timer.start()

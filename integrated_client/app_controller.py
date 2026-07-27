from PyQt5.QtCore import QObject, QTimer
from PyQt5.QtWidgets import QApplication, QDialog

from .database import Database
from .online import ApiClient, OnlineConfig
from .online.coordinator import SyncCoordinator
from .online.session import OnlineSessionManager
from .online.sync import SyncEngine
from .online.update import UpdateClient
from .online.update_coordinator import UpdateCoordinator
from .ui.auth_dialogs import LoginDialog
from .ui.main_window import MainWindow


class ApplicationController(QObject):
    def __init__(
        self,
        application: QApplication,
        database: Database,
        session_manager=None,
    ):
        super().__init__()
        self.application = application
        self.database = database
        self.window = None
        self.online_configuration_error = ""
        if session_manager is None:
            try:
                session_manager = OnlineSessionManager(
                    database,
                    ApiClient(OnlineConfig.load()),
                )
            except (RuntimeError, ValueError) as exc:
                self.online_configuration_error = str(exc)
        self.session_manager = session_manager
        self.sync_coordinator = None
        self.update_coordinator = None
        self._logging_out = False

    def start(self):
        QTimer.singleShot(0, self._show_login)

    def _show_login(self):
        dialog = LoginDialog(
            self.database,
            session_manager=self.session_manager,
            configuration_error=self.online_configuration_error,
        )
        if dialog.exec_() != QDialog.Accepted:
            self.application.quit()
            return
        if self.session_manager is not None:
            self.sync_coordinator = SyncCoordinator(
                SyncEngine(self.database, self.session_manager)
            )
            self.update_coordinator = UpdateCoordinator(
                UpdateClient(self.session_manager.api.config)
            )
        self.window = MainWindow(
            self.database,
            dialog.account,
            session_manager=self.session_manager,
            sync_coordinator=self.sync_coordinator,
            update_coordinator=self.update_coordinator,
        )
        self.window.logout_requested.connect(self._handle_logout)
        self.window.window_closed.connect(self._handle_window_closed)
        self.window.show()
        if self.sync_coordinator is not None:
            # Let the login dialog unwind and the main window finish its first
            # paint before starting network and worker-thread activity.
            QTimer.singleShot(0, self.sync_coordinator.start)
        if self.update_coordinator is not None:
            self.update_coordinator.start()

    def _handle_logout(self):
        if not self.window:
            return
        self._logging_out = True
        window = self.window
        self.window = None
        if self.sync_coordinator is not None:
            self.sync_coordinator.stop()
            self.sync_coordinator = None
        if self.update_coordinator is not None:
            self.update_coordinator.stop()
            self.update_coordinator = None
        if self.session_manager is not None:
            self.session_manager.logout()
        window.close()
        window.deleteLater()
        QTimer.singleShot(0, self._show_login)

    def _handle_window_closed(self):
        if self.sync_coordinator is not None:
            self.sync_coordinator.stop()
            self.sync_coordinator = None
        if self.update_coordinator is not None:
            self.update_coordinator.stop()
            self.update_coordinator = None
        if self._logging_out:
            self._logging_out = False
            return
        self.window = None
        self.application.quit()

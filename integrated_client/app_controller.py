from PyQt5.QtCore import QObject, QTimer
from PyQt5.QtWidgets import QApplication, QDialog

from .database import Database
from .online import ApiClient, OnlineConfig
from .online.coordinator import SyncCoordinator
from .online.session import OnlineSessionManager
from .online.sync import SyncEngine
from .online.update import UpdateClient
from .online.update_coordinator import UpdateCoordinator
from .preferences import LoginCredentialStore
from .ui.auth_dialogs import LoginDialog
from .ui.loading_dialog import run_with_loading
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
        self.credential_store = LoginCredentialStore(
            self.database.path.parent,
            protector=getattr(session_manager, "protector", None),
        )
        self.sync_coordinator = None
        self.update_coordinator = None
        self._logging_out = False
        self.offline_business_mode = False

    def start(self):
        QTimer.singleShot(0, self._show_login)

    def _show_login(self):
        dialog = LoginDialog(
            self.database,
            session_manager=self.session_manager,
            configuration_error=self.online_configuration_error,
            credential_store=self.credential_store,
        )
        if dialog.exec_() != QDialog.Accepted:
            self.application.quit()
            return
        self.offline_business_mode = bool(
            getattr(dialog, "offline_business_mode", False)
        )
        if self.session_manager is not None and not self.offline_business_mode:
            self.sync_coordinator = SyncCoordinator(
                SyncEngine(self.database, self.session_manager)
            )
            self.update_coordinator = UpdateCoordinator(
                UpdateClient(self.session_manager.api.config)
            )
        self.window = MainWindow(
            self.database,
            dialog.account,
            session_manager=(
                None if self.offline_business_mode else self.session_manager
            ),
            sync_coordinator=self.sync_coordinator,
            update_coordinator=self.update_coordinator,
            credential_store=self.credential_store,
            business_metrics_enabled=not self.offline_business_mode,
            offline_business_mode=self.offline_business_mode,
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
        guest_mode = self.offline_business_mode
        window = self.window
        self.window = None
        if self.sync_coordinator is not None:
            self.sync_coordinator.stop()
            self.sync_coordinator = None
        if self.update_coordinator is not None:
            self.update_coordinator.stop()
            self.update_coordinator = None
        if self.offline_business_mode:
            if (
                self.session_manager is not None
                and self.session_manager.state is not None
                and self.session_manager.state.mode == "offline_untracked"
            ):
                self.session_manager.end_offline_session()
        elif self.session_manager is not None:
            run_with_loading(window, "退出中…", self.session_manager.logout)
        self.offline_business_mode = False
        if not guest_mode:
            try:
                self.credential_store.disable_auto_login()
            except (OSError, RuntimeError, ValueError):
                pass
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

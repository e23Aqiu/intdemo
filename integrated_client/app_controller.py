from PyQt5.QtCore import QObject, QTimer
from PyQt5.QtWidgets import QApplication, QDialog

from .database import Database
from .ui.auth_dialogs import LoginDialog
from .ui.main_window import MainWindow


class ApplicationController(QObject):
    def __init__(self, application: QApplication, database: Database):
        super().__init__()
        self.application = application
        self.database = database
        self.window = None
        self.database.ensure_default_admin()
        self.database.ensure_default_station_users()
        self._logging_out = False

    def start(self):
        QTimer.singleShot(0, self._show_login)

    def _show_login(self):
        dialog = LoginDialog(self.database)
        if dialog.exec_() != QDialog.Accepted:
            self.application.quit()
            return
        self.window = MainWindow(self.database, dialog.account)
        self.window.logout_requested.connect(self._handle_logout)
        self.window.window_closed.connect(self._handle_window_closed)
        self.window.show()

    def _handle_logout(self):
        if not self.window:
            return
        self._logging_out = True
        window = self.window
        self.window = None
        window.close()
        window.deleteLater()
        QTimer.singleShot(0, self._show_login)

    def _handle_window_closed(self):
        if self._logging_out:
            self._logging_out = False
            return
        self.window = None
        self.application.quit()

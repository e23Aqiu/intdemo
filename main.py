import sys

from integrated_client.platform_support import configure_desktop_environment

configure_desktop_environment()

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QIcon
from PyQt5.QtWidgets import QApplication

from integrated_client.app_controller import ApplicationController
from integrated_client.config import APP_NAME, ORGANIZATION_NAME
from integrated_client.database import Database
from integrated_client.diagnostics import configure_diagnostics, get_logger
from integrated_client.ui.theme import (
    APP_STYLESHEET,
    _control_asset_path,
    install_disabled_cursor_filter,
)


def main():
    configure_diagnostics()
    if hasattr(Qt, "AA_EnableHighDpiScaling"):
        QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    if hasattr(Qt, "AA_UseHighDpiPixmaps"):
        QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(ORGANIZATION_NAME)
    if sys.platform.startswith("linux") and hasattr(app, "setDesktopFileName"):
        app.setDesktopFileName("com.e23aqiu.intdemo")
    app.setWindowIcon(QIcon(_control_asset_path("app-icon.png")))
    app.setStyle("Fusion")
    app.setStyleSheet(APP_STYLESHEET)
    install_disabled_cursor_filter(app)
    app.setQuitOnLastWindowClosed(False)

    try:
        database = Database()
        controller = ApplicationController(app, database)
        controller.start()
        return app.exec_()
    except Exception:
        get_logger().exception("Fatal error while starting the application")
        raise


if __name__ == "__main__":
    sys.exit(main())

import sys

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication

from integrated_client.app_controller import ApplicationController
from integrated_client.config import APP_NAME, ORGANIZATION_NAME
from integrated_client.database import Database
from integrated_client.ui.theme import APP_STYLESHEET


def main():
    if hasattr(Qt, "AA_EnableHighDpiScaling"):
        QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    if hasattr(Qt, "AA_UseHighDpiPixmaps"):
        QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(ORGANIZATION_NAME)
    app.setStyle("Fusion")
    app.setStyleSheet(APP_STYLESHEET)
    app.setQuitOnLastWindowClosed(False)

    database = Database()
    controller = ApplicationController(app, database)
    controller.start()
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
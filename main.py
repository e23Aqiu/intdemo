import sys

from integrated_client.platform_support import configure_desktop_environment

configure_desktop_environment()

from PyQt5.QtCore import Qt, QTimer, qVersion
from PyQt5.QtGui import QIcon
from PyQt5.QtWidgets import QApplication

from integrated_client.app_controller import ApplicationController
from integrated_client.config import APP_NAME, ORGANIZATION_NAME
from integrated_client.database import Database
from integrated_client.diagnostics import configure_diagnostics, get_logger
from integrated_client.online.uos_layers import confirm_running_layer
from integrated_client.ui.theme import (
    APP_STYLESHEET,
    _control_asset_path,
    install_disabled_cursor_filter,
)


def main():
    configure_diagnostics()
    runtime_self_check = "--self-check" in sys.argv
    if hasattr(Qt, "AA_EnableHighDpiScaling"):
        QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    if hasattr(Qt, "AA_UseHighDpiPixmaps"):
        QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

    app = QApplication(
        [argument for argument in sys.argv if argument != "--self-check"]
    )
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(ORGANIZATION_NAME)
    if sys.platform.startswith("linux") and hasattr(app, "setDesktopFileName"):
        app.setDesktopFileName("com.e23aqiu.intdemo")
    app.setWindowIcon(QIcon(_control_asset_path("app-icon.png")))
    app.setStyle("Fusion")
    app.setStyleSheet(APP_STYLESHEET)
    install_disabled_cursor_filter(app)
    app.setQuitOnLastWindowClosed(False)

    if runtime_self_check:
        # Force creation of the selected platform input context so UOS builds
        # verify the bundled Fcitx plugin instead of merely discovering it.
        app.inputMethod().locale()
        print(f"客户端运行库自检通过：Qt {qVersion()}", flush=True)
        return 0

    try:
        database = Database()
        controller = ApplicationController(app, database)
        controller.start()
        # Promote a UOS trial only after Qt's event loop has remained healthy
        # long enough to display the login UI. If startup fails first, the
        # stable launcher rolls back on the next run.
        QTimer.singleShot(1500, confirm_running_layer)
        return app.exec_()
    except Exception:
        get_logger().exception("Fatal error while starting the application")
        raise


if __name__ == "__main__":
    sys.exit(main())

from __future__ import annotations

import sys
from pathlib import Path

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication

from .ui import ReleasePublisherWindow


def main() -> int:
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    application = QApplication(sys.argv)
    application.setApplicationName("IntDemo Release Publisher")
    repo_root = Path(__file__).resolve().parent.parent
    window = ReleasePublisherWindow(repo_root)
    window.show()
    return application.exec_()


if __name__ == "__main__":
    raise SystemExit(main())

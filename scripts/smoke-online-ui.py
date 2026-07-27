from __future__ import annotations

import argparse
import faulthandler
import sys
import traceback
from pathlib import Path

from PyQt5.QtCore import QThreadPool, QTimer
from PyQt5.QtWidgets import QApplication

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from integrated_client.database import Database
from integrated_client.online import ApiClient, OnlineConfig
from integrated_client.online.coordinator import SyncCoordinator
from integrated_client.online.session import OnlineSessionManager
from integrated_client.online.sync import SyncEngine
from integrated_client.ui.main_window import MainWindow


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("config", type=Path)
    parser.add_argument("--seconds", type=int, default=15)
    parser.add_argument("--show", action="store_true")
    args = parser.parse_args()

    faulthandler.enable()
    faulthandler.dump_traceback_later(10, repeat=True)
    unhandled: list[str] = []

    def exception_hook(exc_type, exc_value, exc_traceback):
        text = "".join(
            traceback.format_exception(exc_type, exc_value, exc_traceback)
        )
        unhandled.append(text)
        print("UNHANDLED_EXCEPTION", file=sys.stderr, flush=True)
        print(text, file=sys.stderr, flush=True)
        QTimer.singleShot(0, application.quit)

    sys.excepthook = exception_hook
    print("STEP=application", flush=True)
    application = QApplication([])
    print("STEP=database", flush=True)
    database = Database()
    print("STEP=configuration", flush=True)
    configuration = OnlineConfig.load(args.config)
    print("STEP=session", flush=True)
    session = OnlineSessionManager(database, ApiClient(configuration))
    profile = session._decrypt_profile()
    if not profile:
        print("No saved online profile is available.", file=sys.stderr)
        return 2
    print("STEP=account", flush=True)
    account = session._state_from_bundle(profile["bundle"], "online")
    print("STEP=coordinator", flush=True)
    coordinator = SyncCoordinator(SyncEngine(database, session))
    coordinator.status_changed.connect(
        lambda status: print(
            f"STATUS={status.state} ERROR={status.error or ''}",
            flush=True,
        )
    )
    print("STEP=window", flush=True)
    window = MainWindow(
        database,
        account,
        session_manager=session,
        sync_coordinator=coordinator,
    )
    print("STEP=start", flush=True)
    if args.show:
        window.show()
    coordinator.start()
    QTimer.singleShot(max(1, args.seconds) * 1000, application.quit)
    exit_code = application.exec_()
    coordinator.stop()
    QThreadPool.globalInstance().waitForDone(10000)
    window.deleteLater()
    print(f"EXIT={exit_code} UNHANDLED={len(unhandled)}", flush=True)
    return 1 if unhandled else exit_code


if __name__ == "__main__":
    raise SystemExit(main())

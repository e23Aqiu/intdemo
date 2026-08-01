from __future__ import annotations

import faulthandler
import logging
import os
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import APP_NAME, APP_VERSION, get_data_dir

LOGGER_NAME = "intdemo.client"
_configured = False
_fault_stream = None
_handling_exception = threading.Lock()


def get_log_dir() -> Path:
    path = get_data_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_log_path() -> Path:
    return get_log_dir() / "client.log"


def get_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)


def _write_unhandled_exception(
    exc_type,
    exc_value,
    exc_traceback,
    *,
    source: str,
) -> None:
    logger = get_logger()
    if not _handling_exception.acquire(blocking=False):
        return
    try:
        logger.critical(
            "Unhandled exception in %s",
            source,
            exc_info=(exc_type, exc_value, exc_traceback),
        )
    finally:
        _handling_exception.release()


def _sys_exception_hook(exc_type, exc_value, exc_traceback) -> None:
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    _write_unhandled_exception(
        exc_type,
        exc_value,
        exc_traceback,
        source="Qt/main thread",
    )


def _thread_exception_hook(args) -> None:
    _write_unhandled_exception(
        args.exc_type,
        args.exc_value,
        args.exc_traceback,
        source=f"thread {getattr(args.thread, 'name', 'unknown')}",
    )


def configure_diagnostics() -> Path:
    """Configure persistent logging before any Qt window or worker is created."""
    global _configured, _fault_stream
    log_path = get_log_path()
    if _configured:
        return log_path

    logger = get_logger()
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = RotatingFileHandler(
        log_path,
        maxBytes=2 * 1024 * 1024,
        backupCount=4,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s %(threadName)s %(name)s: %(message)s"
        )
    )
    logger.addHandler(handler)

    fault_path = get_log_dir() / "native-crash.log"
    try:
        _fault_stream = fault_path.open("a", encoding="utf-8")
        faulthandler.enable(file=_fault_stream, all_threads=True)
    except (OSError, RuntimeError):
        _fault_stream = None
        logger.exception("Could not enable native fault logging")

    sys.excepthook = _sys_exception_hook
    if hasattr(threading, "excepthook"):
        threading.excepthook = _thread_exception_hook

    _configured = True
    logger.info(
        "%s %s started (pid=%s, executable=%s)",
        APP_NAME,
        APP_VERSION,
        os.getpid(),
        sys.executable,
    )
    return log_path

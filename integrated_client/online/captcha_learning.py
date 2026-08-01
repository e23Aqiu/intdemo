from __future__ import annotations

import base64
import hashlib
import json
import os
import uuid
from datetime import datetime

from PyQt5.QtCore import QObject, QRunnable, QThreadPool, QTimer, pyqtSignal

from ..captcha_models import CaptchaModelManager
from ..config import get_data_dir
from ..diagnostics import get_logger

MAX_MODEL_CACHE_BYTES = 20 * 1024 * 1024
MAX_SAMPLE_UPLOAD_BYTES = 1 * 1024 * 1024
MODEL_CACHE_SCHEMA_VERSION = 1
MAX_BACKGROUND_TASKS = 64
UPLOAD_MODE_OFF = "off"
UPLOAD_MODE_METRICS_ONLY = "metrics_only"
UPLOAD_MODE_SAMPLES_AND_METRICS = "samples_and_metrics"
UPLOAD_MODES = {
    UPLOAD_MODE_OFF,
    UPLOAD_MODE_METRICS_ONLY,
    UPLOAD_MODE_SAMPLES_AND_METRICS,
}


class _TaskSignals(QObject):
    finished = pyqtSignal(object, object)


class _Task(QRunnable):
    def __init__(self, function):
        super().__init__()
        self.function = function
        self.signals = _TaskSignals()

    def run(self):
        try:
            result = self.function()
        except Exception as exc:  # noqa: BLE001 - crosses the Qt worker boundary
            self.signals.finished.emit(None, exc)
        else:
            self.signals.finished.emit(result, None)


class CaptchaLearningService(QObject):
    """Fetch reporting policy, upload authorized data, and cache active models."""

    policy_changed = pyqtSignal(object)
    upload_completed = pyqtSignal(object)
    model_changed = pyqtSignal(str, str)

    def __init__(self, session_manager, parent=None, model_manager=None):
        super().__init__(parent)
        self.session_manager = session_manager
        self.model_manager = model_manager or CaptchaModelManager()
        self.policy = {
            "upload_mode": UPLOAD_MODE_OFF,
            "upload_enabled": False,
            "revision": 0,
            "active_models": {},
        }
        self._pool = QThreadPool.globalInstance()
        self._tasks = set()
        self._task_callbacks = {}
        self._task_generation = 0
        self._stopped = True
        self._loading_policy = False
        self._refresh_pending = False
        self._downloading_models = set()
        self._timer = QTimer(self)
        self._timer.setInterval(60_000)
        self._timer.timeout.connect(self.refresh_policy)
        self._load_last_active_models()

    def start(self):
        self._stopped = False
        self._timer.start()
        QTimer.singleShot(0, self.refresh_policy)

    def stop(self):
        self._stopped = True
        self._task_generation += 1
        self._timer.stop()
        self._loading_policy = False
        self._refresh_pending = False
        self._downloading_models.clear()

    def upload_mode(self) -> str:
        mode = str(self.policy.get("upload_mode") or "")
        if mode in UPLOAD_MODES:
            return mode
        # Older servers expose only the boolean policy.
        return (
            UPLOAD_MODE_SAMPLES_AND_METRICS
            if self.policy.get("upload_enabled")
            else UPLOAD_MODE_OFF
        )

    def reporting_enabled(self) -> bool:
        return self.upload_mode() != UPLOAD_MODE_OFF and not self._stopped

    def sample_collection_enabled(self) -> bool:
        return (
            self.upload_mode() == UPLOAD_MODE_SAMPLES_AND_METRICS
            and not self._stopped
        )

    def collection_enabled(self) -> bool:
        """Compatibility alias for code that asks whether samples may be captured."""
        return self.sample_collection_enabled()

    def _start_task(self, function, completed):
        if self._stopped:
            return None
        task = _Task(function)
        self._tasks.add(task)
        self._task_callbacks[id(task.signals)] = (
            task,
            completed,
            self._task_generation,
        )
        task.signals.finished.connect(self._task_finished)
        self._pool.start(task)
        return task

    def _task_finished(self, result, error):
        entry = self._task_callbacks.pop(id(self.sender()), None)
        if entry is None:
            return
        task, completed, generation = entry
        self._tasks.discard(task)
        if self._stopped or generation != self._task_generation:
            return
        completed(result, error)

    def refresh_policy(self):
        if self._stopped:
            return
        if self._loading_policy:
            self._refresh_pending = True
            return
        self._loading_policy = True

        def load():
            token = self.session_manager.access_token()
            return self.session_manager.api.captcha_policy(token)

        self._start_task(load, self._policy_loaded)

    def _policy_loaded(self, result, error):
        self._loading_policy = False
        if self._stopped:
            return
        refresh_again = self._refresh_pending
        self._refresh_pending = False
        if error is not None:
            get_logger().warning("Could not refresh CAPTCHA learning policy: %s", error)
        else:
            self.policy = dict(result or {})
            self.policy_changed.emit(self.policy)
            self._sync_active_models()
        if refresh_again:
            QTimer.singleShot(0, self.refresh_policy)

    def _sync_active_models(self):
        active = self.policy.get("active_models") or {}
        for captcha_type in ("numeric", "click"):
            metadata = active.get(captcha_type)
            if not metadata:
                self.model_manager.clear(captcha_type)
                self._clear_active_model_marker(captcha_type)
                continue
            version = str(metadata.get("version") or "")
            digest = str(metadata.get("artifact_sha256") or "")
            if (
                not version
                or len(digest) != 64
                or self.model_manager.version(captcha_type) == version
                or captcha_type in self._downloading_models
            ):
                continue
            if self._load_model_cache(captcha_type, version, digest):
                continue
            self._downloading_models.add(captcha_type)

            def download(kind=captcha_type):
                token = self.session_manager.access_token()
                return self.session_manager.api.current_captcha_model(token, kind)

            self._start_task(
                download,
                lambda result, error, kind=captcha_type, meta=dict(metadata): (
                    self._model_downloaded(kind, meta, result, error)
                ),
            )

    def _model_downloaded(self, captcha_type, metadata, artifact, error):
        self._downloading_models.discard(captcha_type)
        if self._stopped:
            return
        active_metadata = (self.policy.get("active_models") or {}).get(
            captcha_type
        )
        if (
            not active_metadata
            or str(active_metadata.get("version") or "")
            != str(metadata.get("version") or "")
            or str(active_metadata.get("artifact_sha256") or "")
            != str(metadata.get("artifact_sha256") or "")
        ):
            self._sync_active_models()
            return
        if error is not None:
            get_logger().warning(
                "Could not download CAPTCHA model %s: %s",
                captcha_type,
                error,
            )
            return
        artifact = bytes(artifact or b"")
        expected = str(metadata.get("artifact_sha256") or "")
        if hashlib.sha256(artifact).hexdigest() != expected:
            get_logger().error("CAPTCHA model checksum mismatch: %s", captcha_type)
            return
        version = str(metadata.get("version") or "")
        try:
            self.model_manager.install(captcha_type, version, artifact)
        except Exception as exc:  # noqa: BLE001 - invalid remote artifact
            get_logger().exception("Could not install CAPTCHA model: %s", exc)
            return
        try:
            self._save_model_cache(captcha_type, version, artifact)
        except OSError as exc:
            get_logger().warning("Could not cache CAPTCHA model: %s", exc)
        self.model_changed.emit(captcha_type, version)

    @staticmethod
    def _model_cache_path(captcha_type, version):
        directory = get_data_dir() / "captcha-models" / captcha_type
        safe_version = "".join(
            character
            for character in str(version)
            if character.isalnum() or character in "._-"
        )[:80]
        if not safe_version:
            safe_version = hashlib.sha256(str(version).encode("utf-8")).hexdigest()[:16]
        return directory / f"{safe_version}.npz"

    def _load_model_cache(self, captcha_type, version, expected_digest):
        target = self._model_cache_path(captcha_type, version)
        try:
            if target.stat().st_size > MAX_MODEL_CACHE_BYTES:
                return False
            artifact = target.read_bytes()
            if hashlib.sha256(artifact).hexdigest() != expected_digest:
                return False
            self.model_manager.install(captcha_type, version, artifact)
        except (OSError, ValueError):
            return False
        except Exception as exc:  # noqa: BLE001 - invalid local cache
            get_logger().warning("Could not load cached CAPTCHA model: %s", exc)
            return False
        try:
            self._write_active_model_marker(
                captcha_type,
                version,
                expected_digest,
            )
        except OSError as exc:
            get_logger().warning(
                "Could not update cached CAPTCHA model marker: %s",
                exc,
            )
        self.model_changed.emit(captcha_type, version)
        return True

    @staticmethod
    def _active_model_marker_path(captcha_type):
        return get_data_dir() / "captcha-models" / captcha_type / "active.json"

    @classmethod
    def _write_active_model_marker(cls, captcha_type, version, digest):
        target = cls._active_model_marker_path(captcha_type)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".active.{uuid.uuid4().hex}.tmp")
        payload = {
            "schema_version": MODEL_CACHE_SCHEMA_VERSION,
            "captcha_type": captcha_type,
            "version": version,
            "artifact_sha256": digest,
        }
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False),
                encoding="utf-8",
            )
            os.replace(temporary, target)
        finally:
            if temporary.exists():
                try:
                    temporary.unlink()
                except OSError:
                    pass

    @classmethod
    def _clear_active_model_marker(cls, captcha_type):
        try:
            cls._active_model_marker_path(captcha_type).unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            get_logger().warning(
                "Could not clear cached CAPTCHA model marker: %s",
                exc,
            )

    def _load_last_active_models(self):
        for captcha_type in ("numeric", "click"):
            marker = self._active_model_marker_path(captcha_type)
            try:
                if marker.stat().st_size > 4096:
                    continue
                payload = json.loads(marker.read_text(encoding="utf-8"))
                if (
                    payload.get("schema_version") != MODEL_CACHE_SCHEMA_VERSION
                    or payload.get("captcha_type") != captcha_type
                ):
                    continue
                version = str(payload.get("version") or "")
                digest = str(payload.get("artifact_sha256") or "")
                target = self._model_cache_path(captcha_type, version)
                if (
                    not version
                    or len(digest) != 64
                    or target.stat().st_size > MAX_MODEL_CACHE_BYTES
                ):
                    continue
                artifact = target.read_bytes()
                if hashlib.sha256(artifact).hexdigest() != digest:
                    continue
                self.model_manager.install(captcha_type, version, artifact)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue
            except Exception as exc:  # noqa: BLE001 - invalid local cache
                get_logger().warning(
                    "Could not restore cached CAPTCHA model: %s",
                    exc,
                )

    @classmethod
    def _save_model_cache(cls, captcha_type, version, artifact):
        target = cls._model_cache_path(captcha_type, version)
        directory = target.parent
        directory.mkdir(parents=True, exist_ok=True)
        temporary = directory / f".{target.stem}.{uuid.uuid4().hex}.tmp"
        try:
            temporary.write_bytes(artifact)
            os.replace(temporary, target)
            cls._write_active_model_marker(
                captcha_type,
                version,
                hashlib.sha256(artifact).hexdigest(),
            )
        finally:
            if temporary.exists():
                try:
                    temporary.unlink()
                except OSError:
                    pass

    def record_attempt(self, event: dict) -> bool:
        upload_mode = self.upload_mode()
        if (
            self._stopped
            or upload_mode == UPLOAD_MODE_OFF
            or not isinstance(event, dict)
            or len(self._tasks) >= MAX_BACKGROUND_TASKS
        ):
            return False
        success = bool(event.get("success"))
        payload = {
            "captcha_type": str(event.get("captcha_type") or ""),
            "model_version": str(event.get("model_version") or "unknown")[:80],
            "success": success,
            "assisted": bool(event.get("assisted")),
            "occurred_at": str(
                event.get("occurred_at")
                or datetime.now().astimezone().isoformat()
            ),
        }
        if success and upload_mode == UPLOAD_MODE_SAMPLES_AND_METRICS:
            image = bytes(event.get("image_bytes") or b"")
            answer = event.get("answer")
            if (
                image
                and len(image) <= MAX_SAMPLE_UPLOAD_BYTES
                and isinstance(answer, dict)
            ):
                payload.update(
                    {
                        "image_mime": str(
                            event.get("image_mime") or "image/png"
                        ),
                        "image_base64": base64.b64encode(image).decode("ascii"),
                        "answer": answer,
                    }
                )

        def upload():
            token = self.session_manager.access_token()
            return self.session_manager.api.submit_captcha_attempt(token, payload)

        self._start_task(upload, self._attempt_uploaded)
        return True

    def _attempt_uploaded(self, result, error):
        if error is not None:
            get_logger().warning("Could not upload CAPTCHA attempt: %s", error)
            return
        self.upload_completed.emit(result or {})
        if (
            result
            and int(result.get("policy_revision") or 0)
            > int(self.policy.get("revision") or 0)
        ):
            self.refresh_policy()

from __future__ import annotations

import base64
import hashlib
import json
import os
import uuid
from datetime import datetime
from pathlib import Path

from PyQt5.QtCore import QObject, QRunnable, QThreadPool, QTimer, pyqtSignal

from ..captcha_models import (
    ENHANCED_ONNX_ALGORITHM,
    HOG_SVM_ALGORITHM,
    CaptchaModelManager,
)
from ..config import get_data_dir
from ..diagnostics import get_logger

MAX_MODEL_CACHE_BYTES = 20 * 1024 * 1024
MAX_SAMPLE_UPLOAD_BYTES = 1 * 1024 * 1024
MODEL_CACHE_SCHEMA_VERSION = 2
MAX_BACKGROUND_TASKS = 64
MANUAL_MODEL_PREFIXES = ("human-", "human_")
UPLOAD_MODE_OFF = "off"
UPLOAD_MODE_METRICS_ONLY = "metrics_only"
UPLOAD_MODE_SAMPLES_AND_METRICS = "samples_and_metrics"
UPLOAD_MODES = {
    UPLOAD_MODE_OFF,
    UPLOAD_MODE_METRICS_ONLY,
    UPLOAD_MODE_SAMPLES_AND_METRICS,
}
SUPPORTED_REMOTE_MODEL_ALGORITHMS = {
    HOG_SVM_ALGORITHM,
    ENHANCED_ONNX_ALGORITHM,
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
    pending_count_changed = pyqtSignal(int)
    model_changed = pyqtSignal(str, str)

    def __init__(
        self,
        session_manager,
        parent=None,
        model_manager=None,
        database=None,
        server_account_id=None,
    ):
        super().__init__(parent)
        self.session_manager = session_manager
        self.model_manager = model_manager or CaptchaModelManager()
        self.database = database
        self.server_account_id = str(server_account_id or "").strip()
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
        self._downloading_models = {}
        self._active_model_identities = {}
        self._uploading_outbox_ids = set()
        self._timer = QTimer(self)
        self._timer.setInterval(60_000)
        self._timer.timeout.connect(self.refresh_policy)
        self._load_last_active_models()

    def start(self):
        self._stopped = False
        self._timer.start()
        self._emit_pending_count()
        QTimer.singleShot(0, self.refresh_policy)

    def stop(self):
        self._stopped = True
        self._task_generation += 1
        self._timer.stop()
        self._loading_policy = False
        self._refresh_pending = False
        self._downloading_models.clear()
        self._uploading_outbox_ids.clear()

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
            previous_mode = self.upload_mode()
            self.policy = dict(result or {})
            self.policy_changed.emit(self.policy)
            self._emit_pending_count()
            self._sync_active_models()
            if (
                previous_mode != UPLOAD_MODE_SAMPLES_AND_METRICS
                and self.upload_mode() == UPLOAD_MODE_SAMPLES_AND_METRICS
            ):
                QTimer.singleShot(0, self.retry_pending)
        if refresh_again:
            QTimer.singleShot(0, self.refresh_policy)

    def _sync_active_models(self):
        active = self.policy.get("active_models") or {}
        for captcha_type in ("numeric", "click"):
            metadata = active.get(captcha_type)
            identity = self._model_identity(captcha_type, metadata)
            if identity is None:
                self._clear_installed_model(captcha_type)
                continue
            _, version, algorithm, digest = identity
            if (
                self._active_model_identities.get(captcha_type) == identity
                and self.model_manager.version(captcha_type) == version
                and self.model_manager.algorithm(captcha_type) == algorithm
            ):
                continue
            if self._downloading_models.get(captcha_type) == identity:
                continue
            if self._load_model_cache(
                captcha_type,
                version,
                digest,
                algorithm,
            ):
                continue
            self._downloading_models[captcha_type] = identity

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
        requested_identity = self._model_identity(captcha_type, metadata)
        current_download = self._downloading_models.get(captcha_type)
        if self._stopped:
            return
        active_metadata = (self.policy.get("active_models") or {}).get(
            captcha_type
        )
        active_identity = self._model_identity(captcha_type, active_metadata)
        if requested_identity is None or active_identity != requested_identity:
            if current_download == requested_identity:
                self._downloading_models.pop(captcha_type, None)
            self._sync_active_models()
            return
        if current_download not in {None, requested_identity}:
            return
        if current_download == requested_identity:
            self._downloading_models.pop(captcha_type, None)
        if error is not None:
            get_logger().warning(
                "Could not download CAPTCHA model %s: %s",
                captcha_type,
                error,
            )
            return
        _, version, algorithm, expected = requested_identity
        artifact = bytes(artifact or b"")
        if hashlib.sha256(artifact).hexdigest() != expected:
            get_logger().error("CAPTCHA model checksum mismatch: %s", captcha_type)
            self._reject_model_artifact(captcha_type, version)
            return
        try:
            self.model_manager.install(
                captcha_type,
                version,
                artifact,
                expected_algorithm=algorithm,
            )
        except Exception as exc:  # noqa: BLE001 - invalid remote artifact
            get_logger().exception("Could not install CAPTCHA model: %s", exc)
            self._reject_model_artifact(captcha_type, version)
            return
        self._active_model_identities[captcha_type] = requested_identity
        try:
            self._save_model_cache(
                captcha_type,
                version,
                artifact,
                algorithm=algorithm,
            )
        except (OSError, ValueError) as exc:
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

    def _load_model_cache(
        self,
        captcha_type,
        version,
        expected_digest,
        expected_algorithm,
    ):
        target = self._model_cache_path(captcha_type, version)
        try:
            if target.stat().st_size > MAX_MODEL_CACHE_BYTES:
                raise ValueError("cached model exceeds the size limit")
            artifact = target.read_bytes()
            if hashlib.sha256(artifact).hexdigest() != expected_digest:
                raise ValueError("cached model checksum mismatch")
            self.model_manager.install(
                captcha_type,
                version,
                artifact,
                expected_algorithm=expected_algorithm,
            )
        except (OSError, ValueError):
            return False
        except Exception as exc:  # noqa: BLE001 - invalid local cache
            get_logger().warning("Could not load cached CAPTCHA model: %s", exc)
            return False
        identity = (
            captcha_type,
            version,
            expected_algorithm,
            expected_digest,
        )
        self._active_model_identities[captcha_type] = identity
        try:
            self._write_active_model_marker(
                captcha_type,
                version,
                expected_digest,
                algorithm=expected_algorithm,
            )
        except OSError as exc:
            get_logger().warning(
                "Could not update cached CAPTCHA model marker: %s",
                exc,
            )
        self.model_changed.emit(captcha_type, version)
        return True

    @staticmethod
    def _model_identity(captcha_type, metadata):
        if not isinstance(metadata, dict):
            return None
        version = str(metadata.get("version") or "")
        algorithm = str(metadata.get("algorithm") or "")
        digest = str(metadata.get("artifact_sha256") or "").lower()
        if (
            captcha_type not in {"numeric", "click"}
            or not version
            or len(version) > 128
            or algorithm not in SUPPORTED_REMOTE_MODEL_ALGORITHMS
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            return None
        return captcha_type, version, algorithm, digest

    def _clear_installed_model(self, captcha_type):
        self.model_manager.clear(captcha_type)
        self._active_model_identities.pop(captcha_type, None)
        self._clear_active_model_marker(captcha_type)
        self._prune_model_cache(captcha_type)

    def _reject_model_artifact(self, captcha_type, version):
        self._clear_installed_model(captcha_type)
        try:
            self._model_cache_path(captcha_type, version).unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            get_logger().warning("Could not remove invalid CAPTCHA model: %s", exc)

    @staticmethod
    def _active_model_marker_path(captcha_type):
        return get_data_dir() / "captcha-models" / captcha_type / "active.json"

    @classmethod
    def _prune_model_cache(cls, captcha_type, *, keep=None):
        directory = cls._active_model_marker_path(captcha_type).parent
        try:
            entries = tuple(directory.iterdir())
        except FileNotFoundError:
            return
        except OSError as exc:
            get_logger().warning("Could not inspect CAPTCHA model cache: %s", exc)
            return
        keep_path = None if keep is None else Path(keep)
        for entry in entries:
            if (
                not entry.is_file()
                or entry.is_symlink()
                or entry.suffix.lower() != ".npz"
                or entry == keep_path
            ):
                continue
            try:
                entry.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                get_logger().warning(
                    "Could not remove old CAPTCHA model cache %s: %s",
                    entry,
                    exc,
                )

    @classmethod
    def _write_active_model_marker(
        cls,
        captcha_type,
        version,
        digest,
        algorithm="",
    ):
        target = cls._active_model_marker_path(captcha_type)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".active.{uuid.uuid4().hex}.tmp")
        payload = {
            "schema_version": MODEL_CACHE_SCHEMA_VERSION,
            "captcha_type": captcha_type,
            "version": version,
            "artifact_sha256": digest,
            "algorithm": str(algorithm or ""),
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
                    self._clear_active_model_marker(captcha_type)
                    continue
                version = str(payload.get("version") or "")
                digest = str(payload.get("artifact_sha256") or "").lower()
                algorithm = str(payload.get("algorithm") or "")
                identity = self._model_identity(captcha_type, payload)
                if identity is None:
                    self._reject_model_artifact(captcha_type, version)
                    continue
                target = self._model_cache_path(captcha_type, version)
                if (
                    not version
                    or len(digest) != 64
                    or target.stat().st_size > MAX_MODEL_CACHE_BYTES
                ):
                    self._reject_model_artifact(captcha_type, version)
                    continue
                artifact = target.read_bytes()
                if hashlib.sha256(artifact).hexdigest() != digest:
                    self._reject_model_artifact(captcha_type, version)
                    continue
                self.model_manager.install(
                    captcha_type,
                    version,
                    artifact,
                    expected_algorithm=algorithm,
                )
                self._active_model_identities[captcha_type] = identity
                self._prune_model_cache(captcha_type, keep=target)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                self._clear_installed_model(captcha_type)
                continue
            except Exception as exc:  # noqa: BLE001 - invalid local cache
                get_logger().warning(
                    "Could not restore cached CAPTCHA model: %s",
                    exc,
                )
                self._reject_model_artifact(captcha_type, version)

    @classmethod
    def _save_model_cache(
        cls,
        captcha_type,
        version,
        artifact,
        *,
        algorithm="",
    ):
        artifact = bytes(artifact or b"")
        if not artifact or len(artifact) > MAX_MODEL_CACHE_BYTES:
            raise ValueError("CAPTCHA model cache artifact exceeds the size limit")
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
                algorithm=algorithm,
            )
            cls._prune_model_cache(captcha_type, keep=target)
        finally:
            if temporary.exists():
                try:
                    temporary.unlink()
                except OSError:
                    pass

    def _resolved_server_account_id(self) -> str:
        if self.server_account_id:
            return self.server_account_id
        state = getattr(self.session_manager, "state", None)
        account = getattr(state, "account", None)
        return str(getattr(account, "server_account_id", None) or "").strip()

    def pending_upload_count(self) -> int:
        account_id = self._resolved_server_account_id()
        if self.database is None or not account_id:
            return 0
        try:
            return self.database.get_captcha_upload_pending_count(account_id)
        except Exception as exc:  # noqa: BLE001 - keep the business UI responsive
            get_logger().warning("Could not count pending CAPTCHA uploads: %s", exc)
            return 0

    def _emit_pending_count(self) -> None:
        self.pending_count_changed.emit(self.pending_upload_count())

    def retry_pending(self) -> int:
        """Retry persisted CAPTCHA samples when collection is currently allowed."""
        if self._stopped or self.upload_mode() != UPLOAD_MODE_SAMPLES_AND_METRICS:
            return 0
        account_id = self._resolved_server_account_id()
        if self.database is None or not account_id:
            return 0
        capacity = max(0, MAX_BACKGROUND_TASKS - len(self._tasks))
        if capacity <= 0:
            return 0
        try:
            rows = self.database.get_pending_captcha_uploads(account_id, limit=500)
        except Exception as exc:  # noqa: BLE001 - local persistence failure
            get_logger().warning("Could not read pending CAPTCHA uploads: %s", exc)
            return 0
        started = 0
        for row in rows:
            if started >= capacity:
                break
            if self._start_queued_upload(row["id"], row["payload"]):
                started += 1
        self._emit_pending_count()
        return started

    def _start_queued_upload(self, outbox_id: int, payload: dict) -> bool:
        outbox_id = int(outbox_id)
        if (
            self._stopped
            or outbox_id in self._uploading_outbox_ids
            or len(self._tasks) >= MAX_BACKGROUND_TASKS
        ):
            return False
        self._uploading_outbox_ids.add(outbox_id)

        def upload():
            token = self.session_manager.access_token()
            return self.session_manager.api.submit_captcha_attempt(token, payload)

        task = self._start_task(
            upload,
            lambda result, error, row_id=outbox_id: self._queued_attempt_uploaded(
                row_id,
                result,
                error,
            ),
        )
        if task is None:
            self._uploading_outbox_ids.discard(outbox_id)
            return False
        return True

    def _queued_attempt_uploaded(self, outbox_id, result, error):
        self._uploading_outbox_ids.discard(int(outbox_id))
        try:
            if error is not None:
                self.database.mark_captcha_upload_failed(outbox_id, str(error))
            else:
                self.database.mark_captcha_upload_sent(outbox_id)
        except Exception as persistence_error:  # noqa: BLE001 - preserve diagnostics
            get_logger().warning(
                "Could not update pending CAPTCHA upload %s: %s",
                outbox_id,
                persistence_error,
            )
        self._emit_pending_count()
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

    def record_attempt(self, event: dict) -> bool:
        upload_mode = self.upload_mode()
        if (
            self._stopped
            or upload_mode == UPLOAD_MODE_OFF
            or not isinstance(event, dict)
        ):
            return False
        success = bool(event.get("success"))
        assisted = bool(event.get("assisted"))
        model_version = str(event.get("model_version") or "unknown")[:80]
        normalized_version = model_version.strip().lower()
        manual = assisted or normalized_version == "human" or (
            normalized_version.startswith(MANUAL_MODEL_PREFIXES)
        )
        if upload_mode == UPLOAD_MODE_METRICS_ONLY and manual:
            return False
        payload = {
            "captcha_type": str(event.get("captcha_type") or ""),
            "model_version": model_version,
            "success": success,
            "assisted": assisted,
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

        if "image_base64" in payload:
            account_id = self._resolved_server_account_id()
            if self.database is not None and account_id:
                try:
                    outbox_id = self.database.enqueue_captcha_upload(
                        account_id,
                        payload,
                    )
                except Exception as exc:  # noqa: BLE001 - fall back to direct upload
                    get_logger().warning(
                        "Could not persist CAPTCHA upload before sending: %s",
                        exc,
                    )
                else:
                    self._emit_pending_count()
                    self._start_queued_upload(outbox_id, payload)
                    return True

        if len(self._tasks) >= MAX_BACKGROUND_TASKS:
            return False

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

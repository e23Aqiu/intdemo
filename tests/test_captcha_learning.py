import base64
import hashlib
import io
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image, ImageDraw
from playwright.sync_api import sync_playwright
from PyQt5.QtCore import QItemSelectionModel
from PyQt5.QtWidgets import QApplication, QMessageBox

from integrated_client.captcha_models import (
    CaptchaModelManager,
    CaptchaTrainingError,
    HogLinearSvmCaptchaModel,
    KnnCaptchaModel,
    train_candidate,
)
from integrated_client.config import DEFAULT_ADMIN_PASSWORD, DEFAULT_ADMIN_USERNAME
from integrated_client.database import Database
from integrated_client.online.api import ApiResponseError
from integrated_client.online.captcha_learning import CaptchaLearningService
from integrated_client.tools.transport_tool import (
    BusinessBackfillWorker,
    Worker,
    ocr_code,
)
from integrated_client.ui.machine_learning_page import MachineLearningPage
from integrated_client.ui.main_window import MainWindow


class _FakeTrainerStatus:
    def __init__(
        self,
        *,
        code="not_installed",
        version="",
        platform="",
        installed_size=0,
        maintenance_required=False,
        cleanup_available=False,
        maintenance_message="",
    ):
        self.code = code
        self.version = version
        self.platform = platform
        self.installed_size = installed_size
        self.maintenance_required = maintenance_required
        self.cleanup_available = cleanup_available
        self.maintenance_message = maintenance_message
        self.last_self_test_at = ""
        self.detail = "测试状态"

    @property
    def available(self):
        return self.code == "available"

    @property
    def installed(self):
        return self.code != "not_installed"


class _FakeTrainerManager:
    def __init__(self, *, available=False, mode="standard"):
        self._status = _FakeTrainerStatus(
            code="available" if available else "not_installed",
            version="1.2.3" if available else "",
            platform="windows-x86_64" if available else "",
            installed_size=1024 if available else 0,
        )
        self.mode = mode if available else "standard"
        self.installed_sources = []
        self.self_test_count = 0
        self.uninstall_count = 0

    def status(self, *, verify_files=True):
        return self._status

    def preferred_mode(self):
        return self.mode

    def set_preferred_mode(self, mode):
        if mode == "enhanced" and not self._status.available:
            raise RuntimeError("强化组件不可用")
        self.mode = mode
        return mode

    def install(self, source):
        self.installed_sources.append(Path(source))
        self._status = _FakeTrainerStatus(
            code="available",
            version="1.2.3",
            platform="windows-x86_64",
            installed_size=1024,
        )
        return SimpleNamespace(status=self._status, replaced_version="")

    def self_test(self):
        self.self_test_count += 1
        return self._status

    def uninstall(self):
        self.uninstall_count += 1
        self._status = _FakeTrainerStatus()
        self.mode = "standard"
        return 1024


def _png_bytes(image):
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _numeric_image(value, variation=0):
    image = Image.new("L", (96, 32), 255)
    draw = ImageDraw.Draw(image)
    for index, character in enumerate(value):
        digit = int(character)
        left = index * 24
        for bit in range(4):
            if digit & (1 << bit):
                x = left + 3 + bit * 5
                draw.rectangle((x, 4, x + 2, 27), fill=0)
        draw.rectangle(
            (left + 2, 26 - (digit % 3), left + 20, 28 - (digit % 3)),
            fill=0,
        )
    draw.point((variation % 96, variation % 3), fill=0)
    return _png_bytes(image)


def _click_image(variation=0):
    image = Image.new("RGB", (120, 60), "white")
    draw = ImageDraw.Draw(image)
    draw.ellipse((18, 18, 42, 42), fill="black")
    draw.rectangle((78, 18, 102, 42), fill="black")
    draw.line((78, 18, 102, 42), fill="white", width=3)
    draw.point((variation % 120, variation % 5), fill="gray")
    return _png_bytes(image)


def _dataset_archive(numeric_count=25, click_count=35):
    samples = []
    images = {}
    for index in range(numeric_count):
        value = (
            f"{index % 10}"
            f"{(index + 3) % 10}"
            f"{(index + 6) % 10}"
            f"{(index + 9) % 10}"
        )
        name = f"images/numeric-{index}.png"
        images[name] = _numeric_image(value, index)
        samples.append(
            {
                "captcha_type": "numeric",
                "image": name,
                "answer": {"value": value},
                "fingerprint": f"{index + 1:064x}",
            }
        )
    for index in range(click_count):
        name = f"images/click-{index}.png"
        images[name] = _click_image(index)
        samples.append(
            {
                "captcha_type": "click",
                "image": name,
                "answer": {
                    "prompt": ["甲", "乙"],
                    "points": [
                        {"x": 0.25, "y": 0.5},
                        {"x": 0.75, "y": 0.5},
                    ],
                },
                "fingerprint": f"{10_000 + index:064x}",
            }
        )
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "manifest.json",
            json.dumps({"schema_version": 1, "samples": samples}),
        )
        for name, image in images.items():
            archive.writestr(name, image)
    return output.getvalue()


class _FakeApi:
    def __init__(self):
        self.attempts = []
        self.export_types = []
        self.sample_rows = []
        self.deleted_sample_ids = []
        self.downloaded_sample_ids = []
        self.sample_images = {}
        self.renamed_model = None

    @staticmethod
    def captcha_policy(_token):
        return {
            "upload_mode": "off",
            "upload_enabled": False,
            "revision": 1,
            "active_models": {},
        }

    def submit_captcha_attempt(self, _token, payload):
        self.attempts.append(payload)
        return {"stored": True, "policy_revision": 1}

    @staticmethod
    def current_captcha_model(_token, _captcha_type):
        return b""

    @staticmethod
    def admin_captcha_learning_overview(_token):
        return {}

    @staticmethod
    def admin_update_captcha_policy(_token, _mode):
        return {}

    def admin_export_captcha_dataset(self, _token, captcha_type=None):
        self.export_types.append(captcha_type)
        return f"dataset-{captcha_type or 'mixed'}".encode("ascii")

    @staticmethod
    def admin_import_captcha_dataset(_token, _archive):
        return {}

    def admin_captcha_samples(
        self,
        _token,
        *,
        captcha_type=None,
        limit=200,
        offset=0,
    ):
        rows = [
            row
            for row in self.sample_rows
            if not captcha_type or row.get("captcha_type") == captcha_type
        ]
        return {
            "items": rows[offset : offset + limit],
            "total": len(rows),
            "limit": limit,
            "offset": offset,
        }

    def admin_delete_captcha_samples(self, _token, sample_ids):
        self.deleted_sample_ids.extend(sample_ids)
        before = len(self.sample_rows)
        selected = set(sample_ids)
        self.sample_rows = [
            row for row in self.sample_rows if str(row.get("id")) not in selected
        ]
        deleted = before - len(self.sample_rows)
        return {
            "deleted_count": deleted,
            "missing_count": len(sample_ids) - deleted,
        }

    def admin_download_captcha_sample_image(self, _token, sample_id):
        self.downloaded_sample_ids.append(str(sample_id))
        return self.sample_images.get(str(sample_id), b"")

    @staticmethod
    def admin_create_captcha_model(_token, _payload):
        return {}

    @staticmethod
    def admin_activate_captcha_model(_token, _model_id):
        return {}

    def admin_rename_captcha_model(self, _token, model_id, display_name):
        self.renamed_model = (model_id, display_name)
        return {
            "id": model_id,
            "display_name": display_name,
        }

    @staticmethod
    def admin_use_builtin_captcha_model(_token, _captcha_type):
        return {}

    @staticmethod
    def admin_delete_captcha_model(_token, _model_id):
        return None


class _FakeSession:
    def __init__(self):
        self.api = _FakeApi()

    @staticmethod
    def access_token():
        return "token"


class CaptchaLearningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.archive = _dataset_archive()

    def test_numeric_and_click_candidates_round_trip_without_pickle(self):
        numeric = train_candidate(self.archive, "numeric")
        self.assertEqual(numeric.sample_count, 25)
        self.assertGreater(numeric.test_count, 0)
        self.assertTrue(numeric.artifact.startswith(b"PK"))
        self.assertEqual(numeric.algorithm, "hog-linear-svm-v1")
        numeric_model = HogLinearSvmCaptchaModel.from_bytes(numeric.artifact)
        self.assertEqual(
            len(numeric_model.predict_numeric(_numeric_image("0369"))),
            4,
        )

        manager = CaptchaModelManager()
        manager.install("numeric", numeric.version, numeric.artifact)
        self.assertEqual(manager.version("numeric"), numeric.version)

        click = train_candidate(self.archive, "click")
        self.assertEqual(click.sample_count, 35)
        self.assertEqual(click.algorithm, "hog-linear-svm-v1")
        click_model = HogLinearSvmCaptchaModel.from_bytes(click.artifact)
        positions = click_model.predict_click_regions(
            _click_image(),
            [(18, 18, 42, 42), (78, 18, 102, 42)],
        )
        self.assertTrue(positions)
        self.assertTrue(all(len(point) == 2 for point in positions.values()))

    def test_standard_numeric_training_uses_one_shared_full_image_feature(self):
        with patch(
            "integrated_client.captcha_models._numeric_features",
            side_effect=AssertionError("legacy equal-width slicing must not run"),
        ):
            candidate = train_candidate(self.archive, "numeric", mode="standard")

        self.assertEqual(candidate.algorithm, "hog-linear-svm-v1")
        self.assertIn("-hog-svm-", candidate.version)
        self.assertEqual(
            candidate.metrics["classifiers"],
            "four_position_ovr_linear_svm_shared_full_image",
        )

        with self.assertRaises(CaptchaTrainingError):
            train_candidate(self.archive, "numeric", mode="enhanced")

    def test_training_rejects_unsafe_or_insufficient_archives(self):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr(
                "manifest.json",
                json.dumps({"schema_version": 1, "samples": []}),
            )
            archive.writestr("../escape.png", b"not-an-image")
        with self.assertRaises(CaptchaTrainingError):
            train_candidate(output.getvalue(), "numeric")

        with self.assertRaises(CaptchaTrainingError):
            train_candidate(_dataset_archive(numeric_count=5, click_count=0), "numeric")

    def test_service_honors_all_upload_modes(self):
        session = _FakeSession()
        service = CaptchaLearningService(session)
        service._stopped = False
        service.policy = {
            "upload_mode": "samples_and_metrics",
            "upload_enabled": True,
            "revision": 1,
            "active_models": {},
        }

        def immediate(function, completed):
            completed(function(), None)

        service._start_task = immediate
        image = _numeric_image("1234")
        self.assertTrue(
            service.record_attempt(
                {
                    "captcha_type": "numeric",
                    "source": "transport_numeric",
                    "model_version": "human-manual",
                    "success": True,
                    "assisted": True,
                    "image_bytes": image,
                    "answer": {"value": "1234"},
                }
            )
        )
        self.assertTrue(
            service.record_attempt(
                {
                    "captcha_type": "numeric",
                    "source": "transport_numeric",
                    "model_version": "ddddocr-builtin",
                    "success": False,
                    "assisted": False,
                }
            )
        )
        self.assertIn("image_base64", session.api.attempts[0])
        self.assertNotIn("image_base64", session.api.attempts[1])
        self.assertNotIn("answer", session.api.attempts[1])

        service.policy["upload_mode"] = "metrics_only"
        self.assertTrue(
            service.record_attempt(
                {
                    "captcha_type": "numeric",
                    "source": "transport_numeric",
                    "model_version": "ddddocr-builtin",
                    "success": True,
                    "assisted": False,
                    "image_bytes": image,
                    "answer": {"value": "1234"},
                }
            )
        )
        self.assertNotIn("image_base64", session.api.attempts[2])
        self.assertNotIn("answer", session.api.attempts[2])
        self.assertEqual(
            set(session.api.attempts[2]),
            {
                "captcha_type",
                "model_version",
                "success",
                "assisted",
                "occurred_at",
            },
        )
        self.assertFalse(
            service.record_attempt(
                {
                    "captcha_type": "click",
                    "source": "business_click",
                    "model_version": "human-manual",
                    "success": True,
                    "assisted": True,
                    "image_bytes": image,
                    "answer": {
                        "prompt": ["甲"],
                        "points": [{"x": 0.5, "y": 0.5}],
                    },
                }
            )
        )
        self.assertEqual(len(session.api.attempts), 3)

        service.policy["upload_mode"] = "off"
        self.assertFalse(
            service.record_attempt(
                {
                    "captcha_type": "numeric",
                    "source": "transport_numeric",
                    "success": False,
                }
            )
        )
        self.assertEqual(len(session.api.attempts), 3)

    def test_failed_sample_upload_is_persisted_and_can_be_retried(self):
        session = _FakeSession()
        should_fail = {"value": True}

        def submit(_token, payload):
            session.api.attempts.append(payload)
            if should_fail["value"]:
                raise OSError("network unavailable")
            return {"stored": True, "policy_revision": 1}

        session.api.submit_captcha_attempt = submit

        def immediate(function, completed):
            try:
                result = function()
            except Exception as exc:  # noqa: BLE001 - mirrors the QRunnable
                completed(None, exc)
            else:
                completed(result, None)
            return object()

        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "client.db")
            service = CaptchaLearningService(
                session,
                database=database,
                server_account_id="account-1",
            )
            service._stopped = False
            service.policy = {
                "upload_mode": "samples_and_metrics",
                "upload_enabled": True,
                "revision": 1,
                "active_models": {},
            }
            service._start_task = immediate

            self.assertTrue(
                service.record_attempt(
                    {
                        "captcha_type": "numeric",
                        "model_version": "human-manual",
                        "success": True,
                        "assisted": True,
                        "image_bytes": _numeric_image("1234"),
                        "answer": {"value": "1234"},
                    }
                )
            )
            self.assertEqual(service.pending_upload_count(), 1)
            pending = database.get_pending_captcha_uploads("account-1")
            self.assertEqual(pending[0]["attempt_count"], 1)
            self.assertIn("image_base64", pending[0]["payload"])

            should_fail["value"] = False
            service.retry_pending()
            self.assertEqual(service.pending_upload_count(), 0)
            self.assertEqual(len(session.api.attempts), 2)

    def test_model_cache_is_checksum_verified_before_install(self):
        candidate = train_candidate(self.archive, "numeric")
        digest = hashlib.sha256(candidate.artifact).hexdigest()
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "integrated_client.online.captcha_learning.get_data_dir",
            return_value=Path(temp_dir),
        ):
            CaptchaLearningService._save_model_cache(
                "numeric",
                candidate.version,
                candidate.artifact,
                algorithm=candidate.algorithm,
            )
            service = CaptchaLearningService(_FakeSession())
            self.assertEqual(
                service.model_manager.version("numeric"),
                candidate.version,
            )
            self.assertTrue(
                service._load_model_cache(
                    "numeric",
                    candidate.version,
                    digest,
                    candidate.algorithm,
                )
            )
            self.assertEqual(
                service.model_manager.version("numeric"),
                candidate.version,
            )
            self.assertFalse(
                service._load_model_cache(
                    "numeric",
                    candidate.version,
                    "0" * 64,
                    candidate.algorithm,
                )
            )

    def test_saving_model_cache_removes_older_versions(self):
        candidate = train_candidate(self.archive, "numeric")
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "integrated_client.online.captcha_learning.get_data_dir",
            return_value=Path(temp_dir),
        ):
            old_cache = CaptchaLearningService._model_cache_path(
                "numeric", "numeric-old"
            )
            old_cache.parent.mkdir(parents=True)
            old_cache.write_bytes(b"old-model")
            unrelated = old_cache.parent / "notes.txt"
            unrelated.write_text("stale", encoding="utf-8")

            CaptchaLearningService._save_model_cache(
                "numeric",
                candidate.version,
                candidate.artifact,
                algorithm=candidate.algorithm,
            )

            self.assertFalse(old_cache.exists())
            self.assertTrue(unrelated.exists())
            self.assertTrue(
                CaptchaLearningService._model_cache_path(
                    "numeric", candidate.version
                ).exists()
            )
            self.assertTrue(
                CaptchaLearningService._active_model_marker_path(
                    "numeric"
                ).exists()
            )

    def test_saving_model_cache_rejects_oversized_artifact(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "integrated_client.online.captcha_learning.get_data_dir",
            return_value=Path(temp_dir),
        ), patch(
            "integrated_client.online.captcha_learning.MAX_MODEL_CACHE_BYTES",
            8,
        ):
            with self.assertRaises(ValueError):
                CaptchaLearningService._save_model_cache(
                    "numeric",
                    "numeric-too-large",
                    b"123456789",
                    algorithm="hog-linear-svm-v1",
                )
            self.assertFalse(
                CaptchaLearningService._model_cache_path(
                    "numeric", "numeric-too-large"
                ).exists()
            )

    def test_restoring_cached_model_removes_older_versions(self):
        candidate = train_candidate(self.archive, "numeric")
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "integrated_client.online.captcha_learning.get_data_dir",
            return_value=Path(temp_dir),
        ):
            CaptchaLearningService._save_model_cache(
                "numeric",
                candidate.version,
                candidate.artifact,
                algorithm=candidate.algorithm,
            )
            stale = CaptchaLearningService._model_cache_path(
                "numeric", "numeric-stale"
            )
            stale.write_bytes(b"stale")

            service = CaptchaLearningService(_FakeSession())

            self.assertEqual(
                service.model_manager.version("numeric"), candidate.version
            )
            self.assertFalse(stale.exists())

    def test_switching_to_builtin_removes_cached_custom_models(self):
        candidate = train_candidate(self.archive, "numeric")
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "integrated_client.online.captcha_learning.get_data_dir",
            return_value=Path(temp_dir),
        ):
            CaptchaLearningService._save_model_cache(
                "numeric",
                candidate.version,
                candidate.artifact,
                algorithm=candidate.algorithm,
            )
            service = CaptchaLearningService(_FakeSession())
            service.policy = {"active_models": {}}

            service._sync_active_models()

            cache_dir = Path(temp_dir) / "captcha-models" / "numeric"
            self.assertFalse(any(cache_dir.iterdir()))
            self.assertEqual(
                service.model_manager.version("numeric"),
                "ddddocr-builtin",
            )

    def test_model_cache_pruning_leaves_symbolic_links_untouched(self):
        candidate = train_candidate(self.archive, "numeric")
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "integrated_client.online.captcha_learning.get_data_dir",
            return_value=Path(temp_dir),
        ):
            external = Path(temp_dir) / "external-model.npz"
            external.write_bytes(b"external")
            link = CaptchaLearningService._model_cache_path(
                "numeric", "linked-model"
            )
            link.parent.mkdir(parents=True)
            try:
                link.symlink_to(external)
            except (OSError, NotImplementedError):
                self.skipTest("symbolic links are unavailable for this account")

            CaptchaLearningService._save_model_cache(
                "numeric",
                candidate.version,
                candidate.artifact,
                algorithm=candidate.algorithm,
            )

            self.assertTrue(link.is_symlink())
            self.assertEqual(external.read_bytes(), b"external")

    def test_cache_load_rejects_algorithm_mismatch(self):
        candidate = train_candidate(self.archive, "numeric")
        digest = hashlib.sha256(candidate.artifact).hexdigest()
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "integrated_client.online.captcha_learning.get_data_dir",
            return_value=Path(temp_dir),
        ):
            service = CaptchaLearningService(
                _FakeSession(),
                model_manager=CaptchaModelManager(),
            )
            CaptchaLearningService._save_model_cache(
                "numeric",
                candidate.version,
                candidate.artifact,
                algorithm=candidate.algorithm,
            )

            self.assertFalse(
                service._load_model_cache(
                    "numeric",
                    candidate.version,
                    digest,
                    "tiny-cnn-onnx-v1",
                )
            )
            self.assertEqual(
                service.model_manager.version("numeric"),
                "ddddocr-builtin",
            )

    def test_v110_does_not_restore_legacy_knn_from_local_cache(self):
        legacy = KnnCaptchaModel(
            "numeric",
            "numeric-knn-retired",
            [[0.0] * (24 * 32), [1.0] * (24 * 32)],
            ["1", "5"],
            k=1,
        )
        artifact = legacy.to_bytes()
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "integrated_client.online.captcha_learning.get_data_dir",
            return_value=Path(temp_dir),
        ):
            CaptchaLearningService._save_model_cache(
                "numeric",
                legacy.version,
                artifact,
                algorithm=legacy.ALGORITHM,
            )
            service = CaptchaLearningService(_FakeSession())

            self.assertEqual(
                service.model_manager.version("numeric"),
                "ddddocr-builtin",
            )
            self.assertFalse(
                CaptchaLearningService._active_model_marker_path(
                    "numeric"
                ).exists()
            )

    def test_cache_marker_algorithm_must_match_artifact(self):
        candidate = train_candidate(self.archive, "numeric")
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "integrated_client.online.captcha_learning.get_data_dir",
            return_value=Path(temp_dir),
        ):
            CaptchaLearningService._save_model_cache(
                "numeric",
                candidate.version,
                candidate.artifact,
                algorithm="tiny-cnn-onnx-v1",
            )

            service = CaptchaLearningService(_FakeSession())

            self.assertEqual(
                service.model_manager.version("numeric"),
                "ddddocr-builtin",
            )
            self.assertFalse(
                CaptchaLearningService._active_model_marker_path(
                    "numeric"
                ).exists()
            )
            self.assertFalse(
                CaptchaLearningService._model_cache_path(
                    "numeric", candidate.version
                ).exists()
            )

    def test_downloaded_model_algorithm_must_match_policy(self):
        candidate = train_candidate(self.archive, "numeric")
        metadata = {
            "version": candidate.version,
            "algorithm": "tiny-cnn-onnx-v1",
            "artifact_sha256": hashlib.sha256(candidate.artifact).hexdigest(),
        }
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "integrated_client.online.captcha_learning.get_data_dir",
            return_value=Path(temp_dir),
        ):
            service = CaptchaLearningService(_FakeSession())
            service._stopped = False
            service.policy = {"active_models": {"numeric": metadata}}

            service._model_downloaded(
                "numeric",
                metadata,
                candidate.artifact,
                None,
            )

            self.assertEqual(
                service.model_manager.version("numeric"),
                "ddddocr-builtin",
            )
            self.assertFalse(
                CaptchaLearningService._active_model_marker_path(
                    "numeric"
                ).exists()
            )

    def test_downloaded_legacy_knn_is_rejected_under_standard_policy(self):
        legacy = KnnCaptchaModel(
            "numeric",
            "numeric-knn-retired",
            [[0.0] * (24 * 32), [1.0] * (24 * 32)],
            ["1", "5"],
            k=1,
        )
        artifact = legacy.to_bytes()
        metadata = {
            "version": legacy.version,
            "algorithm": "hog-linear-svm-v1",
            "artifact_sha256": hashlib.sha256(artifact).hexdigest(),
        }
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "integrated_client.online.captcha_learning.get_data_dir",
            return_value=Path(temp_dir),
        ):
            service = CaptchaLearningService(_FakeSession())
            service._stopped = False
            service.policy = {"active_models": {"numeric": metadata}}

            service._model_downloaded("numeric", metadata, artifact, None)

            self.assertEqual(
                service.model_manager.version("numeric"),
                "ddddocr-builtin",
            )
            self.assertFalse(
                CaptchaLearningService._model_cache_path(
                    "numeric", legacy.version
                ).exists()
            )

    def test_same_version_with_changed_algorithm_is_not_treated_as_current(self):
        candidate = train_candidate(self.archive, "numeric")
        standard_metadata = {
            "version": candidate.version,
            "algorithm": candidate.algorithm,
            "artifact_sha256": hashlib.sha256(candidate.artifact).hexdigest(),
        }
        changed_metadata = {
            **standard_metadata,
            "algorithm": "tiny-cnn-onnx-v1",
        }
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "integrated_client.online.captcha_learning.get_data_dir",
            return_value=Path(temp_dir),
        ):
            service = CaptchaLearningService(_FakeSession())
            service._stopped = False
            service.model_manager.install(
                "numeric",
                candidate.version,
                candidate.artifact,
                expected_algorithm=candidate.algorithm,
            )
            service._active_model_identities["numeric"] = (
                "numeric",
                candidate.version,
                candidate.algorithm,
                standard_metadata["artifact_sha256"],
            )
            service.policy = {"active_models": {"numeric": changed_metadata}}
            started = []
            service._start_task = lambda function, completed: started.append(
                (function, completed)
            )

            service._sync_active_models()

            self.assertEqual(len(started), 1)
            self.assertEqual(
                service._downloading_models.get("numeric"),
                (
                    "numeric",
                    candidate.version,
                    "tiny-cnn-onnx-v1",
                    changed_metadata["artifact_sha256"],
                ),
            )

    def test_stale_model_download_is_not_installed_after_policy_changes(self):
        candidate = train_candidate(self.archive, "numeric")
        metadata = {
            "version": candidate.version,
            "algorithm": candidate.algorithm,
            "artifact_sha256": hashlib.sha256(candidate.artifact).hexdigest(),
        }
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "integrated_client.online.captcha_learning.get_data_dir",
            return_value=Path(temp_dir),
        ):
            service = CaptchaLearningService(_FakeSession())
            service._stopped = False
            service.policy = {
                "upload_mode": "off",
                "upload_enabled": False,
                "revision": 2,
                "active_models": {},
            }
            service._model_downloaded(
                "numeric",
                metadata,
                candidate.artifact,
                None,
            )
            self.assertEqual(
                service.model_manager.version("numeric"),
                "ddddocr-builtin",
            )

    def test_older_download_cannot_remove_newer_download_identity(self):
        candidate = train_candidate(self.archive, "numeric")
        old_metadata = {
            "version": candidate.version,
            "algorithm": candidate.algorithm,
            "artifact_sha256": hashlib.sha256(candidate.artifact).hexdigest(),
        }
        new_metadata = {
            **old_metadata,
            "version": f"{candidate.version}-new",
            "artifact_sha256": "1" * 64,
        }
        new_identity = (
            "numeric",
            new_metadata["version"],
            new_metadata["algorithm"],
            new_metadata["artifact_sha256"],
        )
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "integrated_client.online.captcha_learning.get_data_dir",
            return_value=Path(temp_dir),
        ):
            service = CaptchaLearningService(_FakeSession())
            service._stopped = False
            service.policy = {"active_models": {"numeric": new_metadata}}
            service._downloading_models["numeric"] = new_identity

            service._model_downloaded(
                "numeric",
                old_metadata,
                candidate.artifact,
                None,
            )

            self.assertEqual(
                service._downloading_models.get("numeric"),
                new_identity,
            )
            self.assertEqual(
                service.model_manager.version("numeric"),
                "ddddocr-builtin",
            )

    def test_workers_emit_samples_immediately_after_captcha_passes(self):
        numeric_worker = Worker(
            "unused.xlsx",
            True,
            False,
            2,
            True,
            2,
            False,
            captcha_collection_enabled=lambda: True,
        )
        numeric_events = []
        numeric_worker.captcha_attempt_signal.connect(numeric_events.append)
        numeric_worker._emit_captcha_attempt(
            success=False,
            model_version="ddddocr-builtin",
            assisted=False,
        )
        numeric_worker._report_successful_captcha_attempt(
            model_version="human-manual",
            assisted=True,
            image_bytes=_numeric_image("1234"),
            answer={"value": "1234"},
        )
        self.assertEqual(len(numeric_events), 2)
        self.assertFalse(numeric_events[0]["success"])
        self.assertNotIn("image_bytes", numeric_events[0])
        self.assertTrue(numeric_events[1]["success"])

        click_worker = BusinessBackfillWorker(
            "unused.xlsx",
            True,
            True,
            2,
            False,
            captcha_collection_enabled=lambda: True,
        )
        click_events = []
        click_worker.captcha_attempt_signal.connect(click_events.append)
        click_worker._report_successful_click_captcha(
            {
                "image_bytes": _click_image(),
                "prompt": ["甲", "乙"],
            },
            [{"x": 0.25, "y": 0.5}, {"x": 0.75, "y": 0.5}],
            model_version="human-manual",
            assisted=True,
        )
        self.assertEqual(click_events[0]["captcha_type"], "click")
        self.assertEqual(len(click_events[0]["answer"]["points"]), 2)

    def test_manual_click_capture_uses_clean_source_and_installs_handler_first(self):
        worker = BusinessBackfillWorker(
            "unused.xlsx",
            True,
            True,
            2,
            False,
            captcha_collection_enabled=lambda: True,
        )
        clean_image = _click_image()
        source = "data:image/png;base64," + base64.b64encode(
            clean_image
        ).decode("ascii")
        call_order = []

        class Prompt:
            first = None

            def __init__(self):
                self.first = self

            @staticmethod
            def inner_text():
                return "请依次点击【甲,乙】"

        class CaptchaImage:
            first = None

            def __init__(self):
                self.first = self

            @staticmethod
            def screenshot():
                raise AssertionError("data URL 原图可用时不应截取带覆盖层的页面")

            @staticmethod
            def evaluate(script, *args):
                if "resetClicks" in script:
                    call_order.append("handler")
                    return {
                        "source": source,
                        "prompt_text": "请依次点击【甲,乙】",
                        "marker_points": [],
                    }
                if "currentSrc" in script:
                    call_order.append("state")
                    return {
                        "source": source,
                        "prompt_text": "请依次点击【甲,乙】",
                    }
                raise AssertionError("unexpected image evaluation script")

        class Page:
            @staticmethod
            def locator(selector):
                if selector == ".verify-msg":
                    return Prompt()
                if selector == ".back-img":
                    return CaptchaImage()
                raise AssertionError(f"unexpected selector: {selector}")

            @staticmethod
            def evaluate(script, *args):
                if "__intdemoCaptchaClickEvents" in script:
                    return []
                if "point-area" in script:
                    return [
                        {"x": 0.25, "y": 0.4},
                        {"x": 0.75, "y": 0.6},
                    ]
                raise AssertionError("unexpected evaluation script")

        worker.page = Page()
        capture = worker._prepare_manual_click_capture()
        self.assertEqual(capture["prompt"], ["甲", "乙"])
        self.assertEqual(call_order[:2], ["handler", "state"])
        with Image.open(io.BytesIO(capture["image_bytes"])) as actual, Image.open(
            io.BytesIO(clean_image)
        ) as expected:
            self.assertEqual(actual.size, expected.size)
            self.assertEqual(
                actual.convert("RGB").tobytes(),
                expected.convert("RGB").tobytes(),
            )

        points = worker._manual_click_points(capture)
        self.assertEqual(
            points,
            [{"x": 0.25, "y": 0.4}, {"x": 0.75, "y": 0.6}],
        )

        events = []
        worker.captcha_attempt_signal.connect(events.append)
        worker._report_successful_click_captcha(
            capture,
            points,
            model_version="human-manual",
            assisted=True,
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["image_bytes"], capture["image_bytes"])
        self.assertEqual(events[0]["answer"]["prompt"], ["甲", "乙"])

    def test_manual_click_prompt_accepts_common_separators(self):
        for text in (
            "请依次点击【甲,乙】",
            "请依次点击【甲，乙】",
            "请依次点击【甲、乙】",
            "请依次点击【甲 乙】",
        ):
            with self.subTest(text=text):
                self.assertEqual(
                    BusinessBackfillWorker._parse_manual_click_prompt(text),
                    ["甲", "乙"],
                )

    def test_manual_click_fallback_screenshot_hides_and_restores_markers(self):
        worker = BusinessBackfillWorker(
            "unused.xlsx",
            True,
            False,
            2,
            True,
            captcha_collection_enabled=lambda: True,
        )
        clean_image = _click_image()
        marker_hidden = {"value": False}

        class Prompt:
            first = None

            def __init__(self):
                self.first = self

            @staticmethod
            def inner_text():
                return "请依次点击【甲,乙】"

        class CaptchaImage:
            first = None

            def __init__(self):
                self.first = self

            @staticmethod
            def screenshot():
                if not marker_hidden["value"]:
                    raise AssertionError("截图时必须先隐藏点选图标")
                return clean_image

            @staticmethod
            def evaluate(script, *args):
                if "resetClicks" in script:
                    return {
                        "source": "https://example.invalid/captcha.png",
                        "prompt_text": "请依次点击【甲,乙】",
                        "marker_points": [],
                    }
                if "currentSrc" in script:
                    return {
                        "source": "https://example.invalid/captcha.png",
                        "prompt_text": "请依次点击【甲,乙】",
                    }
                if "styleId" in script:
                    marker_hidden["value"] = True
                    return None
                raise AssertionError("unexpected image evaluation script")

        class Page:
            @staticmethod
            def locator(selector):
                if selector == ".verify-msg":
                    return Prompt()
                if selector == ".back-img":
                    return CaptchaImage()
                raise AssertionError(f"unexpected selector: {selector}")

            @staticmethod
            def evaluate(script, *args):
                if "getElementById" in script:
                    marker_hidden["value"] = False
                    return None
                raise AssertionError("unexpected page evaluation script")

        worker.page = Page()
        capture = worker._prepare_manual_click_capture()

        self.assertEqual(capture["image_bytes"], clean_image)
        self.assertFalse(marker_hidden["value"])

    def test_manual_click_capture_restarts_when_atomic_snapshot_changes(self):
        worker = BusinessBackfillWorker(
            "unused.xlsx",
            True,
            False,
            2,
            True,
            captcha_collection_enabled=lambda: True,
        )
        first_source = "data:image/png;base64," + base64.b64encode(
            _click_image(5)
        ).decode("ascii")
        second_image = _click_image(6)
        second_source = "data:image/png;base64," + base64.b64encode(
            second_image
        ).decode("ascii")
        snapshots = [
            {
                "source": first_source,
                "prompt_text": "请依次点击【甲,乙】",
                "generation": 1,
                "marker_points": [],
            },
            {
                "source": second_source,
                "prompt_text": "请依次点击【丙,丁】",
                "generation": 2,
                "marker_points": [],
            },
        ]
        states = [
            {
                "source": second_source,
                "prompt_text": "请依次点击【丙,丁】",
                "generation": 2,
            },
            {
                "source": second_source,
                "prompt_text": "请依次点击【丙,丁】",
                "generation": 2,
            },
        ]
        reset_values = []

        class CaptchaImage:
            first = None

            def __init__(self):
                self.first = self

            @staticmethod
            def evaluate(script, *args):
                if "resetClicks" in script:
                    reset_values.append(args[0])
                    return snapshots.pop(0)
                if "currentSrc" in script:
                    return states.pop(0)
                raise AssertionError("unexpected image evaluation script")

        class Page:
            @staticmethod
            def locator(selector):
                if selector == ".back-img":
                    return CaptchaImage()
                raise AssertionError(f"unexpected selector: {selector}")

        worker.page = Page()
        capture = worker._prepare_manual_click_capture()

        self.assertEqual(reset_values, [True, False])
        self.assertEqual(capture["prompt"], ["丙", "丁"])
        with Image.open(io.BytesIO(capture["image_bytes"])) as actual, Image.open(
            io.BytesIO(second_image)
        ) as expected:
            self.assertEqual(
                actual.convert("RGB").tobytes(),
                expected.convert("RGB").tobytes(),
            )

    def test_manual_click_fixed_url_identity_uses_clean_pixels(self):
        worker = BusinessBackfillWorker(
            "unused.xlsx",
            True,
            False,
            2,
            True,
            captcha_collection_enabled=lambda: True,
        )
        source = "https://example.invalid/captcha.png"
        prompt = ["甲", "乙"]
        first_image = _click_image(7)
        second_image = _click_image(8)
        capture = {
            "image_bytes": first_image,
            "prompt": prompt,
            "prompt_text": "请依次点击【甲,乙】",
            "challenge_id": worker._click_challenge_id(
                source,
                first_image,
                prompt,
                1,
                use_image=True,
            ),
            "challenge_source": source,
            "challenge_generation": 1,
            "identity_uses_image": True,
        }

        class CaptchaImage:
            first = None

            def __init__(self):
                self.first = self

        class Page:
            @staticmethod
            def locator(selector):
                if selector == ".back-img":
                    return CaptchaImage()
                raise AssertionError(f"unexpected selector: {selector}")

        worker.page = Page()
        state = {
            "source": source,
            "prompt_text": "请依次点击【甲,乙】",
            "generation": 1,
        }
        with patch.object(
            worker,
            "_click_captcha_state",
            return_value=state,
        ), patch.object(
            worker,
            "_clean_manual_click_screenshot",
            return_value=first_image,
        ):
            self.assertEqual(
                worker._current_manual_click_challenge_id(capture),
                capture["challenge_id"],
            )

        with patch.object(
            worker,
            "_click_captcha_state",
            return_value=state,
        ), patch.object(
            worker,
            "_clean_manual_click_screenshot",
            return_value=second_image,
        ):
            self.assertNotEqual(
                worker._current_manual_click_challenge_id(capture),
                capture["challenge_id"],
            )

    def test_manual_click_capture_is_prefetched_before_stability_wait(self):
        worker = BusinessBackfillWorker(
            "unused.xlsx",
            True,
            False,
            2,
            True,
            captcha_collection_enabled=lambda: True,
        )
        capture = {
            "image_bytes": _click_image(),
            "prompt": ["甲", "乙"],
            "challenge_id": "challenge-1",
        }
        order = []

        class Page:
            @staticmethod
            def wait_for_timeout(milliseconds):
                order.append(("wait", milliseconds))

        worker.page = Page()

        def prepare(*, reset_clicks):
            order.append(("capture", reset_clicks))
            return capture

        with patch.object(
            worker,
            "_business_result_is_ready",
            return_value=False,
        ), patch.object(
            worker,
            "_captcha_image_is_ready",
            return_value=True,
        ), patch.object(
            worker,
            "_prepare_manual_click_capture",
            side_effect=prepare,
        ):
            self.assertEqual(worker._wait_for_business_response(), "captcha")

        self.assertEqual(order[0], ("capture", True))
        self.assertEqual(order, [("capture", True)])
        self.assertIs(worker._prefetched_manual_click_capture, capture)

    def test_manual_click_success_after_preliminary_error_saves_original_capture(self):
        worker = BusinessBackfillWorker(
            "unused.xlsx",
            True,
            False,
            2,
            True,
            captcha_collection_enabled=lambda: True,
        )
        capture = {
            "image_bytes": _click_image(),
            "prompt": ["甲", "乙"],
            "challenge_id": "challenge-1",
        }
        points = [{"x": 0.25, "y": 0.4}, {"x": 0.75, "y": 0.6}]

        class Locator:
            first = None

            def __init__(self, selector):
                self.selector = selector
                self.first = self

            def count(self):
                if self.selector == ".point-area":
                    return 2
                if self.selector == ".layui-layer-loading2":
                    return 0
                if self.selector == ".verify-msg":
                    return 1
                raise AssertionError(f"unexpected selector: {self.selector}")

            @staticmethod
            def is_visible():
                return True

        class Page:
            @staticmethod
            def locator(selector):
                return Locator(selector)

        worker.page = Page()
        events = []
        logs = []
        worker.captcha_attempt_signal.connect(events.append)
        worker.log.connect(logs.append)

        with patch.object(
            worker,
            "_prepare_manual_click_capture",
            return_value=capture,
        ) as prepare_capture, patch.object(
            worker,
            "_manual_click_points",
            return_value=points,
        ), patch.object(
            worker,
            "_captcha_prompt_is_visible",
            return_value=True,
        ), patch.object(
            worker,
            "_business_result_is_ready",
            return_value=False,
        ), patch.object(
            worker,
            "_wait_for_manual_click_outcome",
            return_value={"state": "passed"},
        ), patch("integrated_client.tools.transport_tool.time.sleep"):
            self.assertTrue(
                worker._complete_manual_click_captcha("human-manual")
            )

        prepare_capture.assert_called_once_with(reset_clicks=True)
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0]["success"])
        self.assertEqual(events[0]["image_bytes"], capture["image_bytes"])
        self.assertEqual(events[0]["answer"]["points"], points)
        relevant_logs = [
            line
            for line in logs
            if any(
                marker in line
                for marker in (
                    "等待用户点完验证码",
                    "用户已点完验证码",
                    "验证码点击错误",
                    "验证码通过",
                )
            )
        ]
        self.assertEqual(
            relevant_logs,
            [
                "⏳ 等待用户点完验证码...",
                "✅ 用户已点完验证码",
                "❌ 验证码点击错误，请重新点击验证码...",
                "✅ 验证码通过，开始获取信息",
            ],
        )

    def test_manual_click_failure_waits_for_fresh_challenge_before_next_wait(self):
        worker = BusinessBackfillWorker(
            "unused.xlsx",
            True,
            False,
            2,
            True,
            captcha_collection_enabled=lambda: True,
        )
        first_capture = {
            "image_bytes": _click_image(),
            "prompt": ["甲", "乙"],
            "challenge_id": "challenge-1",
        }
        second_capture = {
            "image_bytes": _click_image(1),
            "prompt": ["丙", "丁"],
            "challenge_id": "challenge-2",
        }
        points = [{"x": 0.25, "y": 0.4}, {"x": 0.75, "y": 0.6}]

        class Locator:
            first = None

            def __init__(self, selector):
                self.selector = selector
                self.first = self

            def count(self):
                if self.selector == ".point-area":
                    return 2
                if self.selector == ".layui-layer-loading2":
                    return 0
                if self.selector == ".verify-msg":
                    return 1
                raise AssertionError(f"unexpected selector: {self.selector}")

            @staticmethod
            def is_visible():
                return True

        class Page:
            @staticmethod
            def locator(selector):
                return Locator(selector)

        worker.page = Page()
        timeline = []
        wait_count = [0]

        def record_log(message):
            timeline.append(("log", message))
            if "等待用户点完验证码" in message:
                wait_count[0] += 1
                if wait_count[0] == 2:
                    worker.stop()

        worker.log.connect(record_log)
        worker.captcha_attempt_signal.connect(
            lambda event: timeline.append(("event", event))
        )

        with patch.object(
            worker,
            "_prepare_manual_click_capture",
            return_value=first_capture,
        ) as prepare_capture, patch.object(
            worker,
            "_manual_click_points",
            return_value=points,
        ), patch.object(
            worker,
            "_captcha_prompt_is_visible",
            return_value=True,
        ), patch.object(
            worker,
            "_business_result_is_ready",
            return_value=False,
        ), patch.object(
            worker,
            "_wait_for_manual_click_outcome",
            return_value={"state": "retry", "capture": second_capture},
        ), patch("integrated_client.tools.transport_tool.time.sleep"):
            self.assertFalse(
                worker._complete_manual_click_captcha("human-manual")
            )

        prepare_capture.assert_called_once_with(reset_clicks=True)
        failure_indexes = [
            index
            for index, (kind, value) in enumerate(timeline)
            if kind == "event" and not value["success"]
        ]
        retry_wait_index = next(
            index
            for index, (kind, value) in enumerate(timeline)
            if kind == "log"
            and "等待用户点完验证码" in value
            and index
            > next(
                first
                for first, (first_kind, first_value) in enumerate(timeline)
                if first_kind == "log" and "等待用户点完验证码" in first_value
            )
        )
        self.assertEqual(len(failure_indexes), 1)
        self.assertLess(failure_indexes[0], retry_wait_index)
        relevant_logs = [
            value
            for kind, value in timeline
            if kind == "log"
            and any(
                marker in value
                for marker in (
                    "等待用户点完验证码",
                    "用户已点完验证码",
                    "验证码点击错误",
                    "验证码通过",
                )
            )
        ]
        self.assertEqual(
            relevant_logs,
            [
                "⏳ 等待用户点完验证码...",
                "✅ 用户已点完验证码",
                "❌ 验证码点击错误，请重新点击验证码...",
                "⏳ 等待用户点完验证码...",
            ],
        )
        self.assertFalse(
            any(
                kind == "event" and value["success"]
                for kind, value in timeline
            )
        )

    def test_manual_click_outcome_only_retries_after_challenge_changes(self):
        worker = BusinessBackfillWorker(
            "unused.xlsx",
            True,
            False,
            2,
            True,
            captcha_collection_enabled=lambda: True,
        )
        first_capture = {
            "image_bytes": _click_image(),
            "prompt": ["甲", "乙"],
            "challenge_id": "challenge-1",
        }
        second_capture = {
            "image_bytes": _click_image(1),
            "prompt": ["丙", "丁"],
            "challenge_id": "challenge-2",
        }

        class Page:
            @staticmethod
            def wait_for_timeout(_milliseconds):
                return None

        worker.page = Page()
        with patch.object(
            worker,
            "_manual_click_attempt_from_events",
            return_value=None,
        ), patch.object(
            worker,
            "_business_result_is_ready",
            return_value=False,
        ), patch.object(
            worker,
            "_captcha_prompt_is_visible",
            return_value=True,
        ), patch.object(
            worker,
            "_captcha_image_is_ready",
            return_value=True,
        ), patch.object(
            worker,
            "_business_captcha_loading_is_visible",
            return_value=False,
        ), patch.object(
            worker,
            "_current_manual_click_challenge_id",
            side_effect=["challenge-1", "challenge-2"],
        ), patch.object(
            worker,
            "_manual_click_marker_points",
            return_value=[{"x": 0.25, "y": 0.4}, {"x": 0.75, "y": 0.6}],
        ), patch.object(
            worker,
            "_prepare_manual_click_capture",
            return_value=second_capture,
        ) as prepare_capture:
            outcome = worker._wait_for_manual_click_outcome(first_capture, 2)

        self.assertEqual(outcome["state"], "retry")
        self.assertIs(outcome["capture"], second_capture)
        prepare_capture.assert_called_once_with(reset_clicks=False)

    def test_manual_click_outcome_waits_for_delayed_result_on_same_challenge(self):
        worker = BusinessBackfillWorker(
            "unused.xlsx",
            True,
            False,
            2,
            True,
            captcha_collection_enabled=lambda: True,
        )
        capture = {
            "image_bytes": _click_image(),
            "prompt": ["甲", "乙"],
            "challenge_id": "challenge-1",
            "event_challenge_id": "challenge-1",
        }

        class Page:
            @staticmethod
            def wait_for_timeout(_milliseconds):
                return None

        worker.page = Page()
        with patch.object(
            worker,
            "_manual_click_attempt_from_events",
            return_value=None,
        ), patch.object(
            worker,
            "_business_result_is_ready",
            side_effect=[False, False, True],
        ), patch.object(
            worker,
            "_captcha_prompt_is_visible",
            return_value=True,
        ), patch.object(
            worker,
            "_captcha_image_is_ready",
            return_value=True,
        ), patch.object(
            worker,
            "_business_captcha_loading_is_visible",
            return_value=False,
        ), patch.object(
            worker,
            "_current_manual_click_challenge_id",
            return_value="challenge-1",
        ), patch.object(
            worker,
            "_prepare_manual_click_capture",
        ) as prepare_capture:
            outcome = worker._wait_for_manual_click_outcome(capture, 2)

        self.assertEqual(outcome, {"state": "passed"})
        prepare_capture.assert_not_called()

    def test_manual_click_rapid_retry_uses_new_clean_source_and_points(self):
        worker = BusinessBackfillWorker(
            "unused.xlsx",
            True,
            False,
            2,
            True,
            captcha_collection_enabled=lambda: True,
        )
        clean_image = _click_image(2)
        source = "data:image/png;base64," + base64.b64encode(
            clean_image
        ).decode("ascii")
        events = [
            {
                "x": 0.2,
                "y": 0.3,
                "source": source,
                "prompt_text": "请依次点击【丙，丁】",
            },
            {
                "x": 0.8,
                "y": 0.7,
                "source": source,
                "prompt_text": "请依次点击【丙，丁】",
            },
        ]
        with patch.object(worker, "_manual_click_events", return_value=events):
            attempt = worker._manual_click_attempt_from_events(
                exclude_challenge_id="challenge-1"
            )

        self.assertEqual(attempt["capture"]["prompt"], ["丙", "丁"])
        self.assertEqual(
            attempt["points"],
            [{"x": 0.2, "y": 0.3}, {"x": 0.8, "y": 0.7}],
        )
        with Image.open(io.BytesIO(attempt["capture"]["image_bytes"])) as actual:
            self.assertEqual(actual.size, (120, 60))

    def test_manual_click_remote_retry_is_evidence_even_without_image_bytes(self):
        worker = BusinessBackfillWorker(
            "unused.xlsx",
            True,
            False,
            2,
            True,
            captcha_collection_enabled=lambda: True,
        )
        events = [
            {
                "x": 0.2,
                "y": 0.3,
                "source": "https://example.invalid/captcha.png",
                "prompt_text": "请依次点击【丙，丁】",
                "generation": 2,
            },
            {
                "x": 0.8,
                "y": 0.7,
                "source": "https://example.invalid/captcha.png",
                "prompt_text": "请依次点击【丙，丁】",
                "generation": 2,
            },
        ]
        with patch.object(worker, "_manual_click_events", return_value=events):
            attempt = worker._manual_click_attempt_from_events(
                exclude_challenge_id="challenge-1"
            )

        self.assertIsNone(attempt["capture"])
        self.assertEqual(len(attempt["points"]), 2)

    def test_manual_click_remote_rapid_success_never_saves_old_image(self):
        worker = BusinessBackfillWorker(
            "unused.xlsx",
            True,
            False,
            2,
            True,
            captcha_collection_enabled=lambda: True,
        )
        old_image = _click_image(9)
        capture = {
            "image_bytes": old_image,
            "prompt": ["甲", "乙"],
            "prompt_text": "请依次点击【甲,乙】",
            "challenge_id": "old-visual",
            "event_challenge_id": "old-event",
            "challenge_source": "https://example.invalid/captcha.png",
            "challenge_generation": 1,
        }
        old_points = [{"x": 0.2, "y": 0.3}, {"x": 0.8, "y": 0.7}]
        rapid_attempt = {
            "capture": None,
            "points": [{"x": 0.3, "y": 0.2}, {"x": 0.7, "y": 0.8}],
        }

        class Page:
            pass

        worker.page = Page()
        events = []
        worker.captcha_attempt_signal.connect(events.append)
        with patch.object(
            worker,
            "_prepare_manual_click_capture",
            return_value=capture,
        ), patch.object(
            worker,
            "_captcha_prompt_is_visible",
            return_value=True,
        ), patch.object(
            worker,
            "_current_manual_click_challenge_id",
            return_value="old-visual",
        ), patch.object(
            worker,
            "_manual_click_points",
            return_value=old_points,
        ), patch.object(
            worker,
            "_manual_click_attempt_from_events",
            return_value=rapid_attempt,
        ), patch.object(
            worker,
            "_clear_manual_click_events",
        ), patch.object(
            worker,
            "_business_result_is_ready",
            return_value=True,
        ), patch("integrated_client.tools.transport_tool.time.sleep"):
            self.assertTrue(
                worker._complete_manual_click_captcha("human-manual")
            )

        self.assertEqual([event["success"] for event in events], [False, True])
        self.assertTrue(all("image_bytes" not in event for event in events))

    def test_manual_click_real_chromium_keeps_same_source_retry_events(self):
        worker = BusinessBackfillWorker(
            "unused.xlsx",
            True,
            False,
            2,
            True,
            captcha_collection_enabled=lambda: True,
        )
        first_image = _click_image(3)
        first_source = "data:image/png;base64," + base64.b64encode(
            first_image
        ).decode("ascii")

        with sync_playwright() as runtime:
            try:
                browser = runtime.chromium.launch(headless=True)
            except Exception as exc:
                self.skipTest(f"Chromium unavailable: {exc}")
            try:
                page = browser.new_page(viewport={"width": 500, "height": 300})
                page.set_content(
                    f"""
                    <div class="verify-msg">请依次点击【甲,乙】</div>
                    <img class="back-img" src="{first_source}"
                         style="display:block;width:120px;height:60px">
                    <script>
                    document.querySelector('.back-img').addEventListener('click', event => {{
                        const marker = document.createElement('span');
                        marker.className = 'point-area';
                        marker.style.cssText = `position:fixed;pointer-events:none;
                            width:12px;height:12px;border-radius:6px;background:red;
                            left:${{event.clientX - 6}}px;top:${{event.clientY - 6}}px`;
                        document.body.appendChild(marker);
                    }});
                    </script>
                    """
                )
                worker.page = page
                first_capture = worker._prepare_manual_click_capture()

                image = page.locator(".back-img")
                image.click(position={"x": 24, "y": 24})
                image.click(position={"x": 90, "y": 42})

                page.evaluate(
                    """
                    source => {
                        document.querySelectorAll('.point-area').forEach(
                            marker => marker.remove()
                        );
                        document.querySelector('.back-img').setAttribute(
                            'src', source
                        );
                    }
                    """,
                    first_source,
                )
                page.wait_for_function(
                    "() => document.querySelector('.back-img').complete"
                )
                image.click(position={"x": 30, "y": 20})
                image.click(position={"x": 96, "y": 40})

                first_points = worker._manual_click_points(first_capture)
                worker._clear_manual_click_events(first_capture)
                retry_attempt = worker._manual_click_attempt_from_events(
                    exclude_challenge_id=first_capture["event_challenge_id"]
                )
            finally:
                browser.close()

        self.assertEqual(len(first_points), 2)
        self.assertIsNotNone(retry_attempt)
        self.assertEqual(retry_attempt["capture"]["prompt"], ["甲", "乙"])
        self.assertEqual(len(retry_attempt["points"]), 2)
        self.assertNotEqual(
            retry_attempt["capture"]["challenge_id"],
            first_capture["challenge_id"],
        )
        with Image.open(
            io.BytesIO(retry_attempt["capture"]["image_bytes"])
        ) as actual, Image.open(io.BytesIO(first_image)) as expected:
            self.assertEqual(
                actual.convert("RGB").tobytes(),
                expected.convert("RGB").tobytes(),
            )

    def test_machine_learning_page_exports_one_classified_dataset(self):
        session = _FakeSession()
        with patch.object(MachineLearningPage, "refresh"):
            page = MachineLearningPage(session)
        page.refresh_timer.stop()

        with tempfile.TemporaryDirectory() as temp_dir:
            target_path = Path(temp_dir) / "captcha-dataset.zip"

            def immediate(function, completed):
                completed(function(), None)

            with patch.object(
                page,
                "_start",
                side_effect=immediate,
            ), patch(
                "integrated_client.ui.machine_learning_page.QFileDialog.getSaveFileName",
                return_value=(str(target_path), "ZIP 数据集 (*.zip)"),
            ), patch(
                "integrated_client.ui.machine_learning_page.QMessageBox.information"
            ):
                page._export_dataset()

            self.assertEqual(session.api.export_types, [None])
            self.assertEqual(target_path.read_bytes(), b"dataset-mixed")
            self.assertTrue(page.export_btn.isEnabled())
            self.assertIn("numeric", page.export_btn.toolTip())
            self.assertIn("click", page.export_btn.toolTip())
            self.assertIn("原格式分类包", page.import_btn.toolTip())

        page.deleteLater()

    def test_machine_learning_page_displays_training_mode_and_algorithm(self):
        manager = _FakeTrainerManager(available=True, mode="enhanced")
        with patch.object(MachineLearningPage, "refresh"):
            page = MachineLearningPage(
                _FakeSession(),
                trainer_manager=manager,
            )
        page.refresh_timer.stop()

        self.assertEqual(page.training_mode_combo.currentData(), "enhanced")
        self.assertIn("强化模式", page.training_method_label.text())
        self.assertIn("tiny-cnn-onnx-v1", page.training_method_label.text())
        self.assertIn("已安装", page.trainer_status_label.text())
        self.assertIn("1.2.3", page.trainer_status_label.text())
        self.assertTrue(page.self_test_trainer_btn.isEnabled())
        self.assertTrue(page.uninstall_trainer_btn.isEnabled())
        page.deleteLater()

    def test_machine_learning_page_disables_enhanced_mode_without_component(self):
        manager = _FakeTrainerManager(available=False)
        with patch.object(MachineLearningPage, "refresh"):
            page = MachineLearningPage(
                _FakeSession(),
                trainer_manager=manager,
            )
        page.refresh_timer.stop()

        enhanced_index = page.training_mode_combo.findData("enhanced")
        self.assertEqual(page.training_mode_combo.currentData(), "standard")
        self.assertFalse(
            page.training_mode_combo.model().item(enhanced_index).isEnabled()
        )
        self.assertIn("hog-linear-svm-v1", page.training_method_label.text())
        self.assertIn("未安装", page.trainer_status_label.text())
        self.assertTrue(page.install_trainer_btn.isEnabled())
        self.assertFalse(page.self_test_trainer_btn.isEnabled())
        self.assertFalse(page.uninstall_trainer_btn.isEnabled())
        page.deleteLater()

    def test_machine_learning_page_allows_cleaning_confirmed_residuals(self):
        manager = _FakeTrainerManager(available=False)
        manager._status = _FakeTrainerStatus(
            maintenance_required=True,
            cleanup_available=True,
            maintenance_message="有 1 个卸载临时目录待清理",
            installed_size=2048,
        )
        with patch.object(MachineLearningPage, "refresh"):
            page = MachineLearningPage(
                _FakeSession(),
                trainer_manager=manager,
            )
        page.refresh_timer.stop()

        self.assertIn("检测到组件残留", page.trainer_status_label.text())
        self.assertTrue(page.uninstall_trainer_btn.isEnabled())
        page.deleteLater()

    def test_machine_learning_page_runs_trainer_component_lifecycle(self):
        manager = _FakeTrainerManager(available=False)
        with patch.object(MachineLearningPage, "refresh"):
            page = MachineLearningPage(
                _FakeSession(),
                trainer_manager=manager,
            )
        page.refresh_timer.stop()

        def immediate(function, completed):
            completed(function(), None)
            return object()

        package_path = Path("test-trainer.inttrainer")
        with patch.object(page, "_start", side_effect=immediate), patch(
            "integrated_client.ui.machine_learning_page.QFileDialog.getOpenFileName",
            return_value=(str(package_path), "IntDemo 强化组件"),
        ), patch(
            "integrated_client.ui.machine_learning_page.QMessageBox.question",
            return_value=QMessageBox.Yes,
        ), patch(
            "integrated_client.ui.machine_learning_page.QMessageBox.information",
        ):
            page._install_trainer_component()
            self.assertEqual(manager.installed_sources, [package_path])
            self.assertTrue(page.self_test_trainer_btn.isEnabled())
            self.assertTrue(page.uninstall_trainer_btn.isEnabled())

            page._self_test_trainer_component()
            self.assertEqual(manager.self_test_count, 1)

            page._uninstall_trainer_component()

        self.assertEqual(manager.uninstall_count, 1)
        self.assertEqual(page.training_mode_combo.currentData(), "standard")
        self.assertIn("未安装", page.trainer_status_label.text())
        self.assertFalse(page.self_test_trainer_btn.isEnabled())
        self.assertFalse(page.uninstall_trainer_btn.isEnabled())
        page.deleteLater()

    def test_machine_learning_trainer_controls_do_not_overlap(self):
        manager = _FakeTrainerManager(available=True, mode="enhanced")
        with patch.object(MachineLearningPage, "refresh"):
            page = MachineLearningPage(
                _FakeSession(),
                trainer_manager=manager,
            )
        page.refresh_timer.stop()
        page.resize(800, 600)
        page.show()
        self.app.processEvents()

        controls = [
            page.training_mode_combo,
            page.install_trainer_btn,
            page.self_test_trainer_btn,
            page.uninstall_trainer_btn,
        ]
        geometries = [control.geometry() for control in controls]
        self.assertTrue(
            all(
                control.width() >= control.sizeHint().width()
                for control in controls
            )
        )
        self.assertTrue(
            all(
                not first.intersects(second)
                for index, first in enumerate(geometries)
                for second in geometries[index + 1 :]
            )
        )
        self.assertLess(
            page.trainer_status_label.geometry().bottom(),
            min(geometry.top() for geometry in geometries),
        )
        page.close()
        page.deleteLater()

    def test_machine_learning_page_uses_selected_training_backend(self):
        session = _FakeSession()
        manager = _FakeTrainerManager(available=True, mode="enhanced")
        session.api.admin_export_captcha_dataset = lambda _token, _kind: b"dataset"
        session.api.admin_create_captcha_model = lambda _token, payload: payload
        candidate = SimpleNamespace(
            captcha_type="numeric",
            version="numeric-enhanced-test",
            algorithm="tiny-cnn-onnx-v1",
            artifact=b"onnx",
            sample_count=25,
            test_count=5,
            correct_count=4,
            metrics={
                "training_mode": "enhanced",
                "trainer_version": "1.2.3",
            },
        )
        with patch.object(MachineLearningPage, "refresh"):
            page = MachineLearningPage(session, trainer_manager=manager)
        page.refresh_timer.stop()

        def immediate(function, completed):
            completed(function(), None)
            return object()

        with patch.object(
            page,
            "_start",
            side_effect=immediate,
        ), patch(
            "integrated_client.ui.machine_learning_page.QMessageBox.question",
            return_value=QMessageBox.Yes,
        ), patch(
            "integrated_client.ui.machine_learning_page.QMessageBox.information",
        ), patch(
            "integrated_client.ui.machine_learning_page.train_enhanced_candidate",
            return_value=candidate,
        ) as enhanced, patch(
            "integrated_client.ui.machine_learning_page.train_candidate",
        ) as standard:
            page._train_model("numeric")

        enhanced.assert_called_once_with(b"dataset", "numeric", manager)
        standard.assert_not_called()
        self.assertTrue(page.train_numeric_btn.isEnabled())
        self.assertTrue(page.train_click_btn.isEnabled())
        page.deleteLater()

    def test_workers_emit_success_metrics_without_sample_data(self):
        numeric_worker = Worker(
            "unused.xlsx",
            True,
            False,
            2,
            True,
            2,
            False,
            captcha_collection_enabled=lambda: True,
            captcha_sample_collection_enabled=lambda: False,
        )
        numeric_events = []
        numeric_worker.captcha_attempt_signal.connect(numeric_events.append)
        numeric_worker._report_successful_captcha_attempt(
            model_version="ddddocr-builtin",
            assisted=False,
            image_bytes=_numeric_image("1234"),
            answer={"value": "1234"},
        )
        self.assertTrue(numeric_events[0]["success"])
        self.assertNotIn("image_bytes", numeric_events[0])
        self.assertNotIn("answer", numeric_events[0])

        click_worker = BusinessBackfillWorker(
            "unused.xlsx",
            True,
            True,
            2,
            False,
            captcha_collection_enabled=lambda: True,
            captcha_sample_collection_enabled=lambda: False,
        )
        click_events = []
        click_worker.captcha_attempt_signal.connect(click_events.append)
        click_worker._report_successful_click_captcha(
            {
                "image_bytes": _click_image(),
                "prompt": ["甲", "乙"],
            },
            [{"x": 0.25, "y": 0.5}, {"x": 0.75, "y": 0.5}],
            model_version="ddddocr-builtin",
            assisted=False,
        )
        self.assertTrue(click_events[0]["success"])
        self.assertNotIn("image_bytes", click_events[0])
        self.assertNotIn("answer", click_events[0])

    def test_business_workers_use_the_current_custom_models(self):
        class NumericModel:
            version = "numeric-current-1"

            @staticmethod
            def predict_numeric(_image):
                return "4826"

        class ClickModel:
            version = "click-current-1"

            def __init__(self):
                self.calls = []

            def predict_click_regions(self, image, bboxes):
                self.calls.append((image, bboxes))
                return {
                    "甲": (20, 20),
                    "乙": (80, 40),
                }

        numeric_model = NumericModel()
        click_model = ClickModel()

        class Manager:
            @staticmethod
            def get(captcha_type):
                return {
                    "numeric": numeric_model,
                    "click": click_model,
                }.get(captcha_type)

        class Locator:
            @staticmethod
            def screenshot():
                return _numeric_image("4826")

        class Page:
            @staticmethod
            def locator(_selector):
                return Locator()

        numeric_worker = Worker(
            "unused.xlsx",
            True,
            False,
            2,
            True,
            2,
            False,
            captcha_model_manager=Manager(),
        )
        code, _image, version = ocr_code(
            Page(),
            ".captcha",
            model=numeric_worker._active_model("numeric"),
            return_details=True,
        )
        self.assertEqual((code, version), ("4826", "numeric-current-1"))

        click_worker = BusinessBackfillWorker(
            "unused.xlsx",
            True,
            True,
            2,
            False,
            captcha_model_manager=Manager(),
        )
        logs = []
        click_worker.log.connect(logs.append)
        prediction = click_worker._predict_with_active_click_model(
            b"click-image",
            [(0, 0, 40, 40), (60, 20, 100, 60)],
            2,
        )

        self.assertEqual(
            prediction,
            (
                {"甲": (20, 20), "乙": (80, 40)},
                "click-current-1",
            ),
        )
        self.assertEqual(len(click_model.calls), 1)
        self.assertTrue(any("当前点选验证码模型" in line for line in logs))

    def test_builtin_numeric_ocr_returns_unprocessed_capture_for_sample(self):
        original_image = _numeric_image("4826")

        class Locator:
            @staticmethod
            def screenshot():
                return original_image

        class Page:
            @staticmethod
            def locator(_selector):
                return Locator()

        class BuiltinOcr:
            def __init__(self):
                self.inputs = []

            def classification(self, image):
                self.inputs.append(image)
                return "4826"

        builtin_ocr = BuiltinOcr()
        with patch(
            "integrated_client.tools.transport_tool.ocr",
            builtin_ocr,
        ):
            code, sample_image, version = ocr_code(
                Page(),
                ".captcha",
                return_details=True,
            )

        self.assertEqual((code, version), ("4826", "ddddocr-builtin"))
        self.assertEqual(sample_image, original_image)
        self.assertEqual(len(builtin_ocr.inputs), 12)
        with Image.open(io.BytesIO(original_image)) as original:
            self.assertEqual(original.size, (96, 32))
        for recognition_image in builtin_ocr.inputs:
            self.assertNotEqual(recognition_image, original_image)
            with Image.open(io.BytesIO(recognition_image)) as processed:
                self.assertEqual(processed.size, (136, 32))

    def test_automatic_click_captcha_sample_uses_unprocessed_capture(self):
        original_image = _click_image(3)
        recognition_calls = {"count": 0}
        detection_inputs = []

        class ClickModel:
            version = "click-current-raw-image"

            def __init__(self):
                self.calls = []

            def predict_click_regions(self, image, bboxes):
                self.calls.append((image, bboxes))
                return {"甲": (30, 30), "乙": (90, 30)}

        click_model = ClickModel()

        class Manager:
            @staticmethod
            def get(captcha_type):
                return click_model if captcha_type == "click" else None

        class FakeDdddOcr:
            def __init__(self, *, det=False, ocr=True, show_ad=False):
                self.detector = det and not ocr

            @staticmethod
            def detection(image):
                detection_inputs.append(image)
                return [[12, 12, 48, 48], [72, 12, 108, 48]]

            @staticmethod
            def classification(_image):
                index = recognition_calls["count"]
                recognition_calls["count"] += 1
                return "甲" if index < 8 else "乙"

        class Prompt:
            @staticmethod
            def count():
                return 1

            @staticmethod
            def inner_text():
                return "请依次点击【甲,乙】"

            @staticmethod
            def is_visible():
                return False

        class CaptchaImage:
            first = None

            def __init__(self):
                self.first = self
                self.clicks = []

            @staticmethod
            def screenshot():
                return original_image

            def click(self, *, position, timeout):
                self.clicks.append((position, timeout))

        captcha_image = CaptchaImage()

        class Page:
            @staticmethod
            def locator(selector):
                if selector == ".verify-msg":
                    return Prompt()
                if selector == ".back-img":
                    return captcha_image
                raise AssertionError(f"unexpected selector: {selector}")

            @staticmethod
            def wait_for_timeout(_milliseconds):
                return None

        worker = BusinessBackfillWorker(
            "unused.xlsx",
            True,
            True,
            1,
            False,
            captcha_collection_enabled=lambda: True,
            captcha_model_manager=Manager(),
        )
        worker.page = Page()
        events = []
        worker.captcha_attempt_signal.connect(events.append)

        with patch(
            "integrated_client.tools.transport_tool.DdddOcr",
            FakeDdddOcr,
        ), patch(
            "integrated_client.tools.transport_tool.get_pinyin",
            side_effect=lambda value: value,
        ), patch("integrated_client.tools.transport_tool.time.sleep"):
            self.assertTrue(worker.solve_captcha())

        self.assertEqual(len(events), 1)
        self.assertTrue(events[0]["success"])
        self.assertFalse(events[0]["assisted"])
        self.assertEqual(events[0]["image_bytes"], original_image)
        self.assertEqual(events[0]["answer"]["prompt"], ["甲", "乙"])
        self.assertEqual(len(events[0]["answer"]["points"]), 2)
        self.assertEqual(len(captcha_image.clicks), 2)
        self.assertEqual(len(detection_inputs), 1)
        self.assertNotEqual(detection_inputs[0], original_image)
        self.assertEqual(click_model.calls[0][0], original_image)
        self.assertEqual(
            click_model.calls[0][1],
            [[12, 12, 48, 48], [72, 12, 108, 48]],
        )

    def test_numeric_custom_model_failure_is_counted_before_builtin_fallback(self):
        class Locator:
            @staticmethod
            def screenshot():
                return _numeric_image("1234")

        class Page:
            @staticmethod
            def locator(_selector):
                return Locator()

        class Model:
            version = "numeric-candidate"

            @staticmethod
            def predict_numeric(_image):
                return ""

        failures = []
        with patch("integrated_client.tools.transport_tool.ocr", None), patch(
            "builtins.print",
        ):
            code, image, version = ocr_code(
                Page(),
                ".captcha",
                model=Model(),
                return_details=True,
                model_failure_callback=failures.append,
            )
        self.assertEqual(code, "")
        self.assertTrue(image.startswith(b"\x89PNG"))
        self.assertEqual(version, "ddddocr-unavailable")
        self.assertEqual(failures, ["numeric-candidate"])

    def test_admin_capability_detection_and_overview_rendering(self):
        session = _FakeSession()
        target = SimpleNamespace(
            session_manager=session,
            offline_business_mode=False,
            account=SimpleNamespace(is_admin=True),
            captcha_learning_client_available=True,
        )
        self.assertTrue(
            MainWindow._captcha_learning_client_is_available(target)
        )
        self.assertTrue(
            MainWindow._captcha_learning_admin_is_available(target)
        )

        with patch.object(MachineLearningPage, "refresh"):
            page = MachineLearningPage(session)
        page.refresh_timer.stop()
        with patch.object(page, "_refresh_samples"):
            page._overview_loaded(
                {
                    "policy": {
                        "upload_mode": "samples_and_metrics",
                        "upload_enabled": True,
                        "revision": 4,
                        "updated_at": "2026-07-30T10:00:00+08:00",
                        "active_models": {},
                    },
                    "dataset": {
                        "total_count": 12,
                        "total_bytes": 2048,
                        "numeric_count": 8,
                        "numeric_bytes": 1024,
                        "click_count": 4,
                        "click_bytes": 1024,
                    },
                    "attempts": [
                        {
                            "captcha_type": "numeric",
                            "model_version": "ddddocr-builtin",
                            "attempt_count": 4,
                            "success_count": 3,
                            "success_rate": 0.75,
                        },
                        {
                            "captcha_type": "click",
                            "model_version": "ddddocr-builtin",
                            "attempt_count": 2,
                            "success_count": 1,
                            "success_rate": 0.5,
                        },
                        {
                            "captcha_type": "numeric",
                            "model_version": "external-ocr-1",
                            "attempt_count": 1,
                            "success_count": 1,
                            "success_rate": 1.0,
                        },
                    ],
                    "models": [],
                },
                None,
            )
        self.assertEqual(
            page.upload_mode_combo.currentData(),
            "samples_and_metrics",
        )
        self.assertEqual(page.total_count_value.text(), "12")
        self.assertIn("2.0 KB", page.total_size_value.text())
        self.assertNotIn("策略修订", page.policy_detail.text())
        self.assertIn("75.0%", page.numeric_current_value.text())
        self.assertIn("50.0%", page.click_current_value.text())
        self.assertEqual(page.model_table.rowCount(), 2)
        self.assertEqual(page.model_table.item(0, 1).text(), "ddddocr-builtin")
        self.assertEqual(page.model_table.item(0, 5).text(), "75.0%")
        self.assertEqual(page.model_table.item(1, 5).text(), "50.0%")
        versions = [
            page.model_table.item(row, 1).text()
            for row in range(page.model_table.rowCount())
        ]
        self.assertNotIn("external-ocr-1", versions)
        page.deleteLater()

    def test_machine_learning_page_can_delete_selected_samples(self):
        session = _FakeSession()
        session.api.sample_rows = [
            {
                "id": "11111111-1111-1111-1111-111111111111",
                "captcha_type": "numeric",
                "answer": {"value": "4826"},
                "model_version": "ddddocr-builtin",
                "origin": "client",
                "image_size": 1024,
                "captured_at": "2026-08-11T10:00:00+08:00",
            },
            {
                "id": "22222222-2222-2222-2222-222222222222",
                "captcha_type": "click",
                "answer": {
                    "prompt": ["甲", "乙"],
                    "points": [
                        {"x": 0.2, "y": 0.3},
                        {"x": 0.7, "y": 0.6},
                    ],
                },
                "model_version": "human-manual",
                "origin": "client",
                "image_size": 2048,
                "captured_at": "2026-08-11T10:01:00+08:00",
            },
        ]
        with patch.object(MachineLearningPage, "refresh"):
            page = MachineLearningPage(session)
        page.refresh_timer.stop()
        page._samples_loaded(
            session.api.admin_captcha_samples("token", limit=100, offset=0),
            None,
        )
        self.assertEqual(page.sample_table.rowCount(), 2)
        self.assertEqual(page.sample_table.item(0, 2).text(), "4826")
        self.assertEqual(page.sample_table.item(1, 2).text(), "甲、乙")
        self.assertEqual(page.sample_table.item(0, 3).text(), "自动")
        self.assertEqual(page.sample_table.item(1, 3).text(), "人工")

        selection = page.sample_table.selectionModel()
        for row in (0, 1):
            selection.select(
                page.sample_table.model().index(row, 0),
                QItemSelectionModel.Select | QItemSelectionModel.Rows,
            )

        def immediate(function, completed):
            completed(function(), None)

        with patch.object(
            page,
            "_start",
            side_effect=immediate,
        ), patch.object(
            page,
            "_refresh_samples",
        ), patch.object(
            page,
            "refresh",
        ), patch(
            "integrated_client.ui.machine_learning_page.QMessageBox.question",
            return_value=QMessageBox.Yes,
        ), patch(
            "integrated_client.ui.machine_learning_page.QMessageBox.information"
        ):
            page._delete_selected_samples()

        self.assertEqual(
            session.api.deleted_sample_ids,
            [
                "11111111-1111-1111-1111-111111111111",
                "22222222-2222-2222-2222-222222222222",
            ],
        )
        self.assertTrue(page.delete_samples_btn.isEnabled())
        page.deleteLater()

    def test_machine_learning_page_double_click_opens_sample_image(self):
        session = _FakeSession()
        sample_id = "11111111-1111-1111-1111-111111111111"
        image = _numeric_image("4826")
        session.api.sample_rows = [
            {
                "id": sample_id,
                "captcha_type": "numeric",
                "answer": {"value": "4826"},
                "model_version": "ddddocr-builtin",
                "origin": "client",
                "image_size": len(image),
                "image_mime": "image/png",
                "image_available": True,
                "captured_at": "2026-08-11T10:00:00+08:00",
            }
        ]
        session.api.sample_images[sample_id] = image
        with patch.object(MachineLearningPage, "refresh"):
            page = MachineLearningPage(session)
        page.refresh_timer.stop()
        page._samples_loaded(
            session.api.admin_captcha_samples("token", limit=100, offset=0),
            None,
        )

        def immediate(function, completed):
            completed(function(), None)
            return object()

        with patch.object(
            page,
            "_start",
            side_effect=immediate,
        ), patch(
            "integrated_client.ui.machine_learning_page.ImagePreviewDialog"
        ) as preview_dialog:
            page.sample_table.cellDoubleClicked.emit(0, 2)

        self.assertEqual(session.api.downloaded_sample_ids, [sample_id])
        preview_dialog.assert_called_once_with(
            image,
            f"captcha-sample-{sample_id}.png",
            page,
            save_caption="保存样本图片",
        )
        preview_dialog.return_value.exec_.assert_called_once_with()
        self.assertFalse(page._sample_image_loading)
        self.assertTrue(page.sample_table.isEnabled())
        page.deleteLater()

    def test_machine_learning_page_explains_unavailable_sample_image(self):
        session = _FakeSession()
        session.api.sample_rows = [
            {
                "id": "11111111-1111-1111-1111-111111111111",
                "captcha_type": "numeric",
                "answer": {"value": "4826"},
                "model_version": "ddddocr-builtin",
                "origin": "client",
                "image_size": 0,
                "image_available": False,
                "captured_at": "2026-08-11T10:00:00+08:00",
            }
        ]
        with patch.object(MachineLearningPage, "refresh"):
            page = MachineLearningPage(session)
        page.refresh_timer.stop()
        page._samples_loaded(
            session.api.admin_captcha_samples("token", limit=100, offset=0),
            None,
        )

        with patch(
            "integrated_client.ui.machine_learning_page.QMessageBox.warning"
        ) as warning:
            page.sample_table.cellDoubleClicked.emit(0, 0)

        warning.assert_called_once()
        self.assertEqual(warning.call_args.args[1], "图片不可用")
        self.assertEqual(session.api.downloaded_sample_ids, [])
        page.deleteLater()

    def test_machine_learning_page_uses_image_header_when_mime_is_missing(self):
        session = _FakeSession()
        sample_id = "33333333-3333-3333-3333-333333333333"
        image = b"\xff\xd8\xff" + b"jpeg-data"
        session.api.sample_rows = [
            {
                "id": sample_id,
                "captcha_type": "numeric",
                "answer": {"value": "4826"},
                "model_version": "legacy",
                "origin": "import",
                "image_size": len(image),
                "image_available": True,
                "captured_at": "2026-08-11T10:00:00+08:00",
            }
        ]
        session.api.sample_images[sample_id] = image
        with patch.object(MachineLearningPage, "refresh"):
            page = MachineLearningPage(session)
        page.refresh_timer.stop()
        page._samples_loaded(
            session.api.admin_captcha_samples("token", limit=100, offset=0),
            None,
        )

        def immediate(function, completed):
            completed(function(), None)
            return object()

        with patch.object(page, "_start", side_effect=immediate), patch(
            "integrated_client.ui.machine_learning_page.ImagePreviewDialog"
        ) as preview_dialog:
            page.sample_table.cellDoubleClicked.emit(0, 0)

        self.assertTrue(
            preview_dialog.call_args.args[1].endswith(".jpg")
        )
        page.deleteLater()

    def test_machine_learning_page_restores_table_when_image_task_submit_fails(self):
        session = _FakeSession()
        session.api.sample_rows = [
            {
                "id": "44444444-4444-4444-4444-444444444444",
                "captcha_type": "numeric",
                "answer": {"value": "4826"},
                "image_available": True,
            }
        ]
        with patch.object(MachineLearningPage, "refresh"):
            page = MachineLearningPage(session)
        page.refresh_timer.stop()
        page._samples_loaded(
            session.api.admin_captcha_samples("token", limit=100, offset=0),
            None,
        )

        with patch.object(
            page,
            "_start",
            side_effect=RuntimeError("submit failed"),
        ), patch(
            "integrated_client.ui.machine_learning_page.QMessageBox.warning"
        ) as warning:
            page.sample_table.cellDoubleClicked.emit(0, 0)

        self.assertFalse(page._sample_image_loading)
        self.assertTrue(page.sample_table.isEnabled())
        self.assertIn("submit failed", warning.call_args.args[2])
        page.deleteLater()

    def test_machine_learning_page_ignores_image_result_after_shutdown(self):
        session = _FakeSession()
        with patch.object(MachineLearningPage, "refresh"):
            page = MachineLearningPage(session)
        page.refresh_timer.stop()
        sample = {
            "id": "55555555-5555-5555-5555-555555555555",
            "image_mime": "image/png",
        }
        page._sample_image_loading = True
        page.shutdown()

        with patch(
            "integrated_client.ui.machine_learning_page.ImagePreviewDialog"
        ) as preview_dialog, patch(
            "integrated_client.ui.machine_learning_page.QMessageBox.warning"
        ) as warning:
            page._sample_image_loaded(sample, _numeric_image("4826"), None)

        self.assertFalse(page._sample_image_loading)
        preview_dialog.assert_not_called()
        warning.assert_not_called()
        page.deleteLater()

    def test_machine_learning_page_rejects_non_binary_image_response(self):
        session = _FakeSession()
        with patch.object(MachineLearningPage, "refresh"):
            page = MachineLearningPage(session)
        page.refresh_timer.stop()
        sample = {"id": "66666666-6666-6666-6666-666666666666"}

        with patch(
            "integrated_client.ui.machine_learning_page.ImagePreviewDialog"
        ) as preview_dialog, patch(
            "integrated_client.ui.machine_learning_page.QMessageBox.warning"
        ) as warning:
            page._sample_image_loaded(sample, {"content": "not-bytes"}, None)

        self.assertIn("格式无效", warning.call_args.args[2])
        preview_dialog.assert_not_called()
        self.assertTrue(page.sample_table.isEnabled())
        page.deleteLater()

    def test_machine_learning_page_ignores_double_click_after_shutdown(self):
        session = _FakeSession()
        with patch.object(MachineLearningPage, "refresh"):
            page = MachineLearningPage(session)
        page.refresh_timer.stop()
        page._sample_rows = [{"id": "77777777-7777-7777-7777-777777777777"}]
        page.shutdown()

        with patch.object(page, "_start") as start, patch(
            "integrated_client.ui.machine_learning_page.QMessageBox.warning"
        ) as warning:
            page._view_sample_image(0, 0)

        start.assert_not_called()
        warning.assert_not_called()
        page.deleteLater()

    def test_machine_learning_page_explains_missing_image_download_api(self):
        session = _FakeSession()
        session.api.admin_download_captcha_sample_image = None
        with patch.object(MachineLearningPage, "refresh"):
            page = MachineLearningPage(session)
        page.refresh_timer.stop()
        page._sample_rows = [
            {
                "id": "88888888-8888-8888-8888-888888888888",
                "image_available": True,
            }
        ]

        with patch(
            "integrated_client.ui.machine_learning_page.QMessageBox.warning"
        ) as warning:
            page._view_sample_image(0, 0)

        self.assertEqual(warning.call_args.args[1], "图片加载失败")
        self.assertIn("接口不支持", warning.call_args.args[2])
        self.assertFalse(page._sample_image_loading)
        page.deleteLater()

    def test_machine_learning_page_filters_manual_models_and_recounts_selection(self):
        session = _FakeSession()
        with patch.object(MachineLearningPage, "refresh"):
            page = MachineLearningPage(session)
        page.refresh_timer.stop()
        click_model = {
            "id": "click-model",
            "captcha_type": "click",
            "version": "click-knn-1",
            "status": "archived",
            "accuracy": 0.6,
            "sample_count": 30,
            "test_count": 6,
            "correct_count": 4,
            "artifact_size": 1024,
        }
        overview = {
            "policy": {
                "upload_mode": "metrics_only",
                "upload_enabled": False,
                "active_models": {},
            },
            "dataset": {},
            "attempts": [
                {
                    "captcha_type": "numeric",
                    "model_version": "ddddocr-builtin",
                    "attempt_count": 4,
                    "success_count": 3,
                    "success_rate": 0.75,
                },
                {
                    "captcha_type": "click",
                    "model_version": "human-manual",
                    "attempt_count": 12,
                    "success_count": 12,
                    "success_rate": 1.0,
                },
                {
                    "captcha_type": "numeric",
                    "model_version": "human_legacy",
                    "attempt_count": 7,
                    "success_count": 7,
                    "success_rate": 1.0,
                },
                {
                    "captcha_type": "click",
                    "model_version": "click-knn-1",
                    "attempt_count": 2,
                    "success_count": 1,
                    "success_rate": 0.5,
                },
            ],
            "models": [
                {
                    "id": "manual",
                    "captcha_type": "click",
                    "version": "human-manual",
                    "status": "candidate",
                    "accuracy": 1.0,
                    "sample_count": 1,
                    "test_count": 1,
                    "correct_count": 1,
                    "artifact_size": 1,
                },
                {
                    "id": "legacy-manual",
                    "captcha_type": "numeric",
                    "version": "human_legacy",
                    "status": "candidate",
                    "accuracy": 1.0,
                    "sample_count": 1,
                    "test_count": 1,
                    "correct_count": 1,
                    "artifact_size": 1,
                },
                click_model,
            ],
        }
        with patch.object(page, "_refresh_samples"):
            page._overview_loaded(overview, None)

        versions = [
            page.model_table.item(row, 1).text()
            for row in range(page.model_table.rowCount())
        ]
        self.assertNotIn("human-manual", versions)
        self.assertNotIn("human_legacy", versions)
        self.assertEqual(page.model_table.rowCount(), 3)

        page.model_type_combo.setCurrentIndex(
            page.model_type_combo.findData("numeric")
        )
        self.assertEqual(page.model_table.rowCount(), 1)
        self.assertEqual(page.model_table.item(0, 0).text(), "数字")
        page.model_type_combo.setCurrentIndex(0)

        click_row = next(
            row
            for row in range(page.model_table.rowCount())
            if page.model_table.item(row, 1).text() == "click-knn-1"
        )
        page.model_table.selectRow(click_row)
        self.assertIn("暂无自动识别记录", page.click_current_value.text())
        self.assertIn("50.0%", page.model_hint.text())
        self.assertTrue(page.recalculate_model_btn.isEnabled())

        overview_calls = []

        def filtered_overview(_token, **filters):
            overview_calls.append(filters)
            return {
                "attempts": [
                    {
                        "captcha_type": "click",
                        "model_version": "click-knn-1",
                        "attempt_count": 5,
                        "success_count": 4,
                        "success_rate": 0.8,
                    }
                ],
                "models": [click_model],
            }

        def immediate(function, completed):
            completed(function(), None)

        session.api.admin_captcha_learning_overview = filtered_overview
        with patch.object(page, "_start", side_effect=immediate):
            page._recalculate_selected_model()

        self.assertEqual(
            overview_calls,
            [
                {
                    "captcha_type": "click",
                    "model_version": "click-knn-1",
                }
            ],
        )
        self.assertIn("暂无自动识别记录", page.click_current_value.text())
        self.assertIn("80.0%", page.model_hint.text())
        versions = [
            page.model_table.item(row, 1).text()
            for row in range(page.model_table.rowCount())
        ]
        self.assertEqual(
            versions,
            ["ddddocr-builtin", "ddddocr-builtin", "click-knn-1"],
        )
        click_row = versions.index("click-knn-1")
        self.assertEqual(page.model_table.item(click_row, 5).text(), "80.0%")
        self.assertEqual(page.model_table.item(click_row, 6).text(), "4 / 5")
        self.assertEqual(page.model_table.item(0, 5).text(), "75.0%")
        page.deleteLater()

    def test_machine_learning_page_blocks_retired_knn_activation(self):
        session = _FakeSession()
        activation_calls = []
        session.api.admin_activate_captcha_model = (
            lambda _token, model_id: activation_calls.append(model_id)
        )
        with patch.object(MachineLearningPage, "refresh"):
            page = MachineLearningPage(session)
        page.refresh_timer.stop()
        legacy_model = {
            "id": "legacy-knn-model",
            "captcha_type": "numeric",
            "version": "numeric-knn-retired",
            "algorithm": "knn-pixels-v1",
            "status": "archived",
            "accuracy": 0.8,
            "sample_count": 30,
            "test_count": 6,
            "correct_count": 5,
            "artifact_size": 1024,
        }
        page._populate_models([legacy_model], [], {})

        legacy_row = next(
            row
            for row in range(page.model_table.rowCount())
            if page.model_table.item(row, 1).text() == "numeric-knn-retired"
        )
        page.model_table.selectRow(legacy_row)

        self.assertFalse(page.activate_model_btn.isEnabled())
        self.assertIn("不能再应用", page.activate_model_btn.toolTip())
        self.assertIn("仅供历史记录查看", page.model_hint.text())
        with patch(
            "integrated_client.ui.machine_learning_page.QMessageBox.warning"
        ) as warning, patch.object(page, "_start") as start:
            page._activate_selected_model()

        self.assertEqual(warning.call_args.args[1], "不能应用模型")
        self.assertIn("旧版 KNN", warning.call_args.args[2])
        start.assert_not_called()
        self.assertEqual(activation_calls, [])
        page.deleteLater()

    def test_machine_learning_page_allows_supported_candidate_activation(self):
        session = _FakeSession()
        with patch.object(MachineLearningPage, "refresh"):
            page = MachineLearningPage(session)
        page.refresh_timer.stop()
        candidate = {
            "id": "numeric-hog-model",
            "captcha_type": "numeric",
            "version": "numeric-hog-svm-test",
            "algorithm": "hog-linear-svm-v1",
            "status": "candidate",
            "accuracy": 0.8,
            "sample_count": 30,
            "test_count": 6,
            "correct_count": 5,
            "artifact_size": 1024,
        }
        page._populate_models([candidate], [], {})

        candidate_row = next(
            row
            for row in range(page.model_table.rowCount())
            if page.model_table.item(row, 1).text() == "numeric-hog-svm-test"
        )
        page.model_table.selectRow(candidate_row)

        self.assertTrue(page.activate_model_btn.isEnabled())
        self.assertEqual(page.activate_model_btn.toolTip(), "")
        page.deleteLater()

    def test_machine_learning_page_can_rename_selected_custom_model(self):
        session = _FakeSession()
        with patch.object(MachineLearningPage, "refresh"):
            page = MachineLearningPage(session)
        page.refresh_timer.stop()
        model = {
            "id": "numeric-model",
            "captcha_type": "numeric",
            "version": "numeric-knn-1",
            "display_name": None,
            "status": "candidate",
            "accuracy": 0.8,
            "sample_count": 30,
            "test_count": 6,
            "correct_count": 5,
            "artifact_size": 1024,
        }
        overview = {
            "policy": {
                "upload_mode": "metrics_only",
                "upload_enabled": False,
                "active_models": {},
            },
            "dataset": {},
            "attempts": [],
            "models": [model],
        }
        with patch.object(page, "_refresh_samples"):
            page._overview_loaded(overview, None)

        numeric_row = next(
            row
            for row in range(page.model_table.rowCount())
            if page.model_table.item(row, 1).text() == "numeric-knn-1"
        )
        page.model_table.selectRow(numeric_row)
        self.assertTrue(page.rename_model_btn.isEnabled())

        def immediate(function, completed):
            completed(function(), None)

        with patch(
            "integrated_client.ui.machine_learning_page.QInputDialog.getText",
            return_value=("运输证数字模型", True),
        ), patch.object(page, "_start", side_effect=immediate), patch.object(
            page,
            "refresh",
        ), patch(
            "integrated_client.ui.machine_learning_page.QMessageBox.information"
        ):
            page._rename_selected_model()

        self.assertEqual(
            session.api.renamed_model,
            ("numeric-model", "运输证数字模型"),
        )
        page._populate_models(
            [{**model, "display_name": "运输证数字模型"}],
            [],
            {},
        )
        renamed_row = next(
            row
            for row in range(page.model_table.rowCount())
            if page.model_table.item(row, 1).text() == "运输证数字模型"
        )
        self.assertIn(
            "numeric-knn-1",
            page.model_table.item(renamed_row, 1).toolTip(),
        )
        self.assertTrue(page.rename_model_btn.isEnabled())
        page.deleteLater()

    def test_machine_learning_page_accepts_legacy_sample_list_shape(self):
        session = _FakeSession()
        with patch.object(MachineLearningPage, "refresh"):
            page = MachineLearningPage(session)
        page.refresh_timer.stop()
        page._samples_loaded(
            [
                {
                    "id": "legacy",
                    "captcha_type": "numeric",
                    "answer": {"value": "1234"},
                    "model_version": "ddddocr-builtin",
                    "origin": "client",
                    "image_size": 10,
                    "captured_at": "2026-08-11T10:00:00+08:00",
                }
            ],
            None,
        )
        self.assertEqual(page.sample_table.rowCount(), 1)
        self.assertEqual(page.sample_table.item(0, 2).text(), "1234")
        self.assertNotIn("失败", page.sample_hint.text())
        page.deleteLater()

    def test_machine_learning_page_explains_missing_sample_api(self):
        session = _FakeSession()
        with patch.object(MachineLearningPage, "refresh"):
            page = MachineLearningPage(session)
        page.refresh_timer.stop()
        page._samples_loaded(
            None,
            ApiResponseError(
                "not_found",
                "Not Found",
                status_code=404,
            ),
        )

        self.assertIn("服务端版本过旧", page.sample_hint.text())
        self.assertIn("无需删除或重新采集", page.sample_hint.text())
        self.assertFalse(page.delete_samples_btn.isEnabled())
        page.deleteLater()

    def test_admin_main_window_exposes_machine_learning_navigation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "client.db")
            database.ensure_default_admin()
            admin = database.authenticate(
                DEFAULT_ADMIN_USERNAME,
                DEFAULT_ADMIN_PASSWORD,
            )
            with patch.object(CaptchaLearningService, "start"), patch.object(
                MachineLearningPage,
                "refresh",
            ):
                window = MainWindow(
                    database,
                    admin,
                    session_manager=_FakeSession(),
                )
            self.assertIn("machine_learning", window._pages)
            self.assertIn("machine_learning", window._nav_buttons)
            self.assertTrue(window.captcha_sync_retry_button.isHidden())
            window._update_captcha_sync_policy(
                {"upload_mode": "samples_and_metrics"}
            )
            self.assertFalse(window.captcha_sync_retry_button.isHidden())
            window._update_captcha_pending_count(3)
            self.assertEqual(
                window.captcha_sync_retry_button.text(),
                "验证码待同步：3",
            )
            window._update_captcha_sync_policy({"upload_mode": "metrics_only"})
            self.assertTrue(window.captcha_sync_retry_button.isHidden())
            window.show_page("machine_learning")
            self.assertIs(
                window.stack.currentWidget(),
                window.machine_learning_page,
            )
            window.workflow_page.shutdown()
            window._prepared_to_close = True
            window.close()
            window.deleteLater()

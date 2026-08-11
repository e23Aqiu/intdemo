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
from PyQt5.QtWidgets import QApplication

from integrated_client.captcha_models import (
    CaptchaModelManager,
    CaptchaTrainingError,
    KnnCaptchaModel,
    train_candidate,
)
from integrated_client.config import DEFAULT_ADMIN_PASSWORD, DEFAULT_ADMIN_USERNAME
from integrated_client.database import Database
from integrated_client.online.captcha_learning import CaptchaLearningService
from integrated_client.tools.transport_tool import (
    BusinessBackfillWorker,
    Worker,
    ocr_code,
)
from integrated_client.ui.machine_learning_page import MachineLearningPage
from integrated_client.ui.main_window import MainWindow


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

    @staticmethod
    def admin_create_captcha_model(_token, _payload):
        return {}

    @staticmethod
    def admin_activate_captcha_model(_token, _model_id):
        return {}

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
        numeric_model = KnnCaptchaModel.from_bytes(numeric.artifact)
        self.assertEqual(
            len(numeric_model.predict_numeric(_numeric_image("0369"))),
            4,
        )

        manager = CaptchaModelManager()
        manager.install("numeric", numeric.version, numeric.artifact)
        self.assertEqual(manager.version("numeric"), numeric.version)

        click = train_candidate(self.archive, "click")
        self.assertEqual(click.sample_count, 35)
        click_model = KnnCaptchaModel.from_bytes(click.artifact)
        positions = click_model.predict_click_regions(
            _click_image(),
            [(18, 18, 42, 42), (78, 18, 102, 42)],
        )
        self.assertTrue(positions)
        self.assertTrue(all(len(point) == 2 for point in positions.values()))

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
                )
            )

    def test_stale_model_download_is_not_installed_after_policy_changes(self):
        candidate = train_candidate(self.archive, "numeric")
        metadata = {
            "version": candidate.version,
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
            service._downloading_models.add("numeric")
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

    def test_manual_click_capture_observes_overlay_and_falls_back_to_markers(self):
        worker = BusinessBackfillWorker(
            "unused.xlsx",
            True,
            True,
            2,
            False,
            captcha_collection_enabled=lambda: True,
        )
        capture_scripts = []
        page_scripts = []

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
                return _click_image()

            @staticmethod
            def evaluate(script):
                capture_scripts.append(script)

        class Page:
            @staticmethod
            def locator(selector):
                if selector == ".verify-msg":
                    return Prompt()
                if selector == ".back-img":
                    return CaptchaImage()
                raise AssertionError(f"unexpected selector: {selector}")

            @staticmethod
            def evaluate(script):
                page_scripts.append(script)
                if "__intdemoCaptchaClicks" in script:
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
        self.assertEqual(len(capture_scripts), 1)
        self.assertIn("documentRoot.addEventListener", capture_scripts[0])
        self.assertIn("ownerWindow.__intdemoCaptchaCaptureHandler", capture_scripts[0])

        points = worker._manual_click_points(capture)
        self.assertEqual(
            points,
            [{"x": 0.25, "y": 0.4}, {"x": 0.75, "y": 0.6}],
        )
        self.assertTrue(any("point-area" in script for script in page_scripts))

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
            self.assertIn("包内分类", page.export_btn.toolTip())
            self.assertIn("原格式分类包", page.import_btn.toolTip())

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
                "attempts": [],
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

import io
import json
import unittest
import zipfile
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
from PIL import Image

from enhanced_trainer.click_engine import (
    CLICK_FEATURE_COUNT,
    ClickCandidate,
    ClickFeatureExtractor,
    ClickFeatures,
    best_click_assignment,
    resolve_click_features,
)
from enhanced_trainer.engine import CaptchaEngine, EngineError
from enhanced_trainer.model_bundle import (
    ModelBundleError,
    build_model_bundle,
    load_model_bundle,
)
from integrated_client.captcha_engine import (
    BuiltInCaptchaEngineClient,
    LocalCaptchaEngineClient,
)
from integrated_client.captcha_models import (
    CAPTCHA_ENGINE_ALGORITHM,
    CaptchaModelManager,
)


def _png(width=120, height=60):
    stream = io.BytesIO()
    Image.new("RGB", (width, height), (235, 235, 235)).save(stream, "PNG")
    return stream.getvalue()


def _write_regular_member(archive, name, payload):
    info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = (0o100000 | 0o644) << 16
    archive.writestr(info, payload)


def _bundle(captcha_type, version, config):
    return build_model_bundle(
        captcha_type=captcha_type,
        version=version,
        model_family=config["model_family"],
        min_component_version="2.0.0",
        files={
            "model.onnx": b"test-onnx-model",
            "config.json": json.dumps(
                config,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8"),
        },
    )


class ModelBundleTests(unittest.TestCase):
    def test_round_trip_is_deterministic_and_checks_identity(self):
        arguments = {
            "captcha_type": "click",
            "version": "click-v1",
            "model_family": "click-prompt-ranker-v1",
            "min_component_version": "2.0.0",
            "files": {"model.onnx": b"onnx", "config.json": b"{}"},
        }
        first = build_model_bundle(**arguments)
        second = build_model_bundle(**arguments)
        self.assertEqual(first, second)

        loaded = load_model_bundle(
            first,
            expected_captcha_type="click",
            expected_version="click-v1",
        )
        self.assertEqual(loaded.model_family, "click-prompt-ranker-v1")
        self.assertEqual(loaded.files["model.onnx"], b"onnx")
        with self.assertRaisesRegex(ModelBundleError, "版本不一致"):
            load_model_bundle(first, expected_version="click-v2")

    def test_rejects_tampered_and_undeclared_members(self):
        valid = build_model_bundle(
            captcha_type="numeric",
            version="numeric-v1",
            model_family="numeric-tiny-cnn-v1",
            min_component_version="2.0.0",
            files={"model.onnx": b"original"},
        )
        source = zipfile.ZipFile(io.BytesIO(valid))
        members = {info.filename: source.read(info) for info in source.infolist()}
        source.close()

        for mutation in ("tampered", "extra"):
            output = io.BytesIO()
            with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
                for name, payload in members.items():
                    if mutation == "tampered" and name == "model.onnx":
                        payload = b"changed"
                    _write_regular_member(archive, name, payload)
                if mutation == "extra":
                    _write_regular_member(archive, "undeclared.bin", b"extra")
            with self.subTest(mutation=mutation), self.assertRaises(
                ModelBundleError
            ):
                load_model_bundle(output.getvalue())

    def test_rejects_legacy_json_duplicate_manifest_keys(self):
        manifest = (
            b'{"schema_version":1,"schema_version":1,'
            b'"engine_id":"intdemo-captcha-engine","engine_api_version":1,'
            b'"captcha_type":"numeric","version":"numeric-v1",'
            b'"model_family":"numeric-tiny-cnn-v1",'
            b'"min_component_version":"2.0.0","files":[]}'
        )
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            _write_regular_member(archive, "manifest.json", manifest)
        with self.assertRaises(ModelBundleError):
            load_model_bundle(output.getvalue())


class ClickFeatureTests(unittest.TestCase):
    def test_ddddocr_15_probability_contract_drives_prompt_matching(self):
        class Detector:
            @staticmethod
            def detection(_image):
                return [[5, 5, 45, 45], [65, 5, 105, 45]]

        class Recognizer:
            def __init__(self):
                self.calls = 0

            def classification(self, _image, *, probability=False):
                self.assert_probability = probability
                candidate = self.calls // 4
                self.calls += 1
                if candidate == 0:
                    matrix = [[0.05, 0.90, 0.05], [0.90, 0.05, 0.05]]
                else:
                    matrix = [[0.05, 0.05, 0.90], [0.90, 0.05, 0.05]]
                return {
                    "charsets": ["", "甲", "乙"],
                    "probability": matrix,
                }

        recognizer = Recognizer()
        extractor = ClickFeatureExtractor(
            detector=Detector(),
            recognizer=recognizer,
        )
        prepared = extractor.extract(_png(), ["甲", "乙"])

        self.assertEqual(prepared.features.shape, (2, 2, CLICK_FEATURE_COUNT))
        self.assertEqual([item.view_votes for item in prepared.candidates], [4, 4])
        self.assertEqual(best_click_assignment(prepared.base_scores), (0, 1))
        self.assertTrue((prepared.features[0, 0, 12:16] == 1).all())
        self.assertTrue((prepared.features[1, 1, 12:16] == 1).all())
        self.assertEqual(recognizer.calls, 8)
        self.assertTrue(recognizer.assert_probability)

    def test_global_assignment_is_one_to_one_and_can_abstain(self):
        scores = np.asarray([[5.0, 4.0], [5.0, 0.0]], dtype=np.float32)
        self.assertEqual(best_click_assignment(scores), (1, 0))
        prepared = ClickFeatures(
            features=np.zeros((2, 2, CLICK_FEATURE_COUNT), dtype=np.float32),
            base_scores=np.zeros((2, 2), dtype=np.float32),
            candidates=(
                ClickCandidate((0, 0, 20, 20)),
                ClickCandidate((80, 0, 100, 20)),
            ),
            width=100,
            height=20,
        )
        accepted = resolve_click_features(
            prepared,
            scores,
            min_probability=0.2,
        )
        rejected = resolve_click_features(
            prepared,
            scores,
            min_probability=0.999,
        )
        self.assertEqual(accepted.status, "ok")
        self.assertEqual(rejected.status, "abstain")


class _Node:
    def __init__(self, name, shape):
        self.name = name
        self.shape = shape
        self.type = "tensor(float)"


class _Session:
    def __init__(self, input_shape, output_shape, output):
        self.input = _Node("input", input_shape)
        self.output = _Node("logits", output_shape)
        self.value = np.asarray(output, dtype=np.float32)

    def get_inputs(self):
        return [self.input]

    def get_outputs(self):
        return [self.output]

    def run(self, _outputs, _feeds):
        return [self.value]


class EngineTests(unittest.TestCase):
    def test_numeric_engine_keeps_the_existing_four_head_contract(self):
        config = {
            "schema_version": 1,
            "engine_api_version": 1,
            "captcha_type": "numeric",
            "version": "numeric-v1",
            "model_family": "numeric-tiny-cnn-v1",
        }
        logits = np.full((1, 4, 10), -10.0, dtype=np.float32)
        for position, digit in enumerate((1, 2, 3, 4)):
            logits[0, position, digit] = 10.0
        session = _Session([1, 1, 48, 112], [1, 4, 10], logits)
        engine = CaptchaEngine()
        with patch.object(engine, "_session", return_value=session):
            loaded = engine.load_model(
                _bundle("numeric", "numeric-v1", config),
                expected_type="numeric",
                expected_version="numeric-v1",
            )
        result = engine.predict_numeric(_png())
        self.assertEqual(loaded["model_family"], "numeric-tiny-cnn-v1")
        self.assertEqual(result["value"], "1234")

    def test_click_engine_honors_bundle_blend_and_version(self):
        config = {
            "schema_version": 1,
            "engine_api_version": 1,
            "captcha_type": "click",
            "version": "click-v1",
            "model_family": "click-prompt-ranker-v1",
            "feature_schema": "click-pair-features-v1",
            "feature_count": CLICK_FEATURE_COUNT,
            "ranker_weight": 0.0,
            "min_probability": 0.0,
            "min_row_margin": 0.0,
            "min_assignment_margin": 0.0,
        }
        artifact = _bundle("click", "click-v1", config)
        engine = CaptchaEngine()
        reverse_scores = [[-10.0], [10.0], [10.0], [-10.0]]
        session = _Session(["N", CLICK_FEATURE_COUNT], ["N", 1], reverse_scores)
        with patch.object(engine, "_session", return_value=session):
            engine.load_model(
                artifact,
                expected_type="click",
                expected_version="click-v1",
            )
        prepared = ClickFeatures(
            features=np.zeros((2, 2, CLICK_FEATURE_COUNT), dtype=np.float32),
            base_scores=np.asarray([[0.9, 0.1], [0.1, 0.9]], dtype=np.float32),
            candidates=(
                ClickCandidate((0, 0, 20, 20)),
                ClickCandidate((80, 0, 100, 20)),
            ),
            width=100,
            height=20,
        )
        engine._click_extractor = SimpleNamespace(
            extract=lambda _image, _targets: prepared
        )
        result = engine.predict_click(b"image", ["甲", "乙"])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(
            [(point["x"], point["y"]) for point in result["points"]],
            [(0.1, 0.5), (0.9, 0.5)],
        )
        with self.assertRaises(EngineError):
            CaptchaEngine().load_model(
                artifact,
                expected_type="click",
                expected_version="click-v2",
            )


class ClientAdapterTests(unittest.TestCase):
    def test_default_model_manager_uses_client_runtime_not_local_component(self):
        manager = CaptchaModelManager(component_manager=object())
        self.assertIsInstance(manager.engine_client, BuiltInCaptchaEngineClient)

    def test_client_runtime_loads_and_executes_synchronized_bundle(self):
        config = {
            "schema_version": 1,
            "engine_api_version": 1,
            "captcha_type": "numeric",
            "version": "numeric-v1",
            "model_family": "numeric-tiny-cnn-v1",
        }
        logits = np.full((1, 4, 10), -10.0, dtype=np.float32)
        for position, digit in enumerate((9, 8, 7, 6)):
            logits[0, position, digit] = 10.0
        runtime = CaptchaEngine()
        client = BuiltInCaptchaEngineClient(engine=runtime)
        manager = CaptchaModelManager(engine_client=client)
        with patch.object(
            runtime,
            "_session",
            return_value=_Session([1, 1, 48, 112], [1, 4, 10], logits),
        ):
            manager.install(
                "numeric",
                "numeric-v1",
                _bundle("numeric", "numeric-v1", config),
                expected_algorithm=CAPTCHA_ENGINE_ALGORITHM,
            )

        self.assertEqual(manager.family("numeric"), "numeric-tiny-cnn-v1")
        self.assertEqual(manager.get("numeric").predict_numeric(_png()), "9876")

    def test_engine_client_sends_outer_model_version(self):
        client = LocalCaptchaEngineClient(component_manager=object())
        with patch.object(client, "_load_locked", return_value={}) as load:
            client.load_model("click", "click-v1", b"bundle")
        load.assert_called_once_with("click", "click-v1", b"bundle")

    def test_model_manager_accepts_runtime_artifact_through_engine_contract(self):
        class Engine:
            def __init__(self):
                self.loaded = []

            def load_model(self, captcha_type, version, artifact):
                self.loaded.append((captcha_type, version, artifact))

            @staticmethod
            def predict_click(_image, _targets):
                return {
                    "status": "ok",
                    "points": [{"x": 0.25, "y": 0.5}],
                    "confidence": 0.9,
                    "version": "click-v1",
                }

        engine = Engine()
        manager = CaptchaModelManager(engine_client=engine)
        manager.install(
            "click",
            "click-v1",
            b"future-private-format",
            expected_algorithm=CAPTCHA_ENGINE_ALGORITHM,
        )
        result = manager.get("click").predict_click_points(b"image", ["甲"])
        self.assertEqual(engine.loaded[0][1], "click-v1")
        self.assertEqual(result["points"], [(0.25, 0.5)])


if __name__ == "__main__":
    unittest.main()

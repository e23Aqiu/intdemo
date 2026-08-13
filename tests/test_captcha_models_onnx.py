import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from integrated_client.captcha_models import (
    CaptchaTrainingError,
    TinyCnnOnnxCaptchaModel,
)


class _FakeSession:
    def __init__(
        self,
        *,
        captcha_type="click",
        input_batch="N",
        output_batch="N",
        class_count=3,
        runtime=None,
    ):
        if captcha_type == "numeric":
            input_shape = [1, 1, 48, 112]
            output_shape = [1, 4, 10]
        else:
            input_shape = [input_batch, 3, 64, 64]
            output_shape = [output_batch, class_count]
        self._inputs = [
            SimpleNamespace(name="input", type="tensor(float)", shape=input_shape)
        ]
        self._outputs = [
            SimpleNamespace(name="logits", type="tensor(float)", shape=output_shape)
        ]
        metadata = {
            "algorithm": "tiny-cnn-onnx-v1",
            "captcha_type": captcha_type,
            "version": "test-version",
        }
        if captcha_type == "click":
            metadata["labels_json"] = '["a","b","c"]'
        self._metadata = SimpleNamespace(custom_metadata_map=metadata)
        self.runtime = runtime
        self.run_shapes = []
        self.class_count = class_count
        self.captcha_type = captcha_type

    def get_inputs(self):
        return self._inputs

    def get_outputs(self):
        return self._outputs

    def get_modelmeta(self):
        return self._metadata

    def run(self, output_names, feeds):
        self.run_shapes.append(tuple(feeds["input"].shape))
        if self.runtime is not None:
            return self.runtime(output_names, feeds)
        batch_size = feeds["input"].shape[0]
        if self.captcha_type == "numeric":
            return [np.zeros((1, 4, 10), dtype=np.float32)]
        return [np.zeros((batch_size, self.class_count), dtype=np.float32)]


def _load(session, captcha_type="click"):
    fake_ort = SimpleNamespace(
        InferenceSession=lambda artifact, providers: session,
    )
    with patch.dict(sys.modules, {"onnxruntime": fake_ort}):
        return TinyCnnOnnxCaptchaModel.from_bytes(
            captcha_type,
            "test-version",
            b"onnx-artifact",
        )


class TinyCnnOnnxContractTests(unittest.TestCase):
    def test_numeric_model_runs_install_time_smoke_test(self):
        session = _FakeSession(captcha_type="numeric")

        model = _load(session, "numeric")

        self.assertEqual(model.captcha_type, "numeric")
        self.assertEqual(session.run_shapes, [(1, 1, 48, 112)])

    def test_click_model_exercises_shared_dynamic_batch(self):
        for dynamic_batch in ("N", None):
            with self.subTest(dynamic_batch=dynamic_batch):
                session = _FakeSession(
                    input_batch=dynamic_batch,
                    output_batch=dynamic_batch,
                )

                model = _load(session)

                self.assertEqual(model.labels, ["a", "b", "c"])
                self.assertEqual(
                    session.run_shapes,
                    [(1, 3, 64, 64), (2, 3, 64, 64)],
                )

    def test_click_model_rejects_fixed_or_mismatched_batch_dimensions(self):
        cases = [
            (1, 1),
            ("input_N", "output_N"),
            ("N", None),
            (None, "N"),
            ("", ""),
        ]
        for input_batch, output_batch in cases:
            with self.subTest(input_batch=input_batch, output_batch=output_batch):
                session = _FakeSession(
                    input_batch=input_batch,
                    output_batch=output_batch,
                )

                with self.assertRaises(CaptchaTrainingError):
                    _load(session)

                self.assertEqual(session.run_shapes, [])

    def test_rejects_runtime_failure_during_install(self):
        def fail(_output_names, _feeds):
            raise RuntimeError("unsupported operator")

        session = _FakeSession(runtime=fail)

        with self.assertRaises(CaptchaTrainingError):
            _load(session)

        self.assertEqual(session.run_shapes, [(1, 3, 64, 64)])

    def test_rejects_model_that_only_runs_with_one_click_crop(self):
        def fixed_runtime(_output_names, feeds):
            batch_size = feeds["input"].shape[0]
            if batch_size != 1:
                raise RuntimeError("runtime only accepts a fixed batch")
            return [np.zeros((1, 3), dtype=np.float32)]

        session = _FakeSession(runtime=fixed_runtime)

        with self.assertRaises(CaptchaTrainingError):
            _load(session)

        self.assertEqual(
            session.run_shapes,
            [(1, 3, 64, 64), (2, 3, 64, 64)],
        )

    def test_rejects_non_finite_or_wrong_runtime_output(self):
        def non_finite(_output_names, feeds):
            batch_size = feeds["input"].shape[0]
            result = np.zeros((batch_size, 3), dtype=np.float32)
            result[0, 0] = np.nan
            return [result]

        def wrong_shape(_output_names, feeds):
            batch_size = feeds["input"].shape[0]
            return [np.zeros((batch_size, 2), dtype=np.float32)]

        def wrong_dtype(_output_names, feeds):
            batch_size = feeds["input"].shape[0]
            return [np.zeros((batch_size, 3), dtype=np.float64)]

        for runtime in (non_finite, wrong_shape, wrong_dtype):
            with self.subTest(
                runtime=runtime.__name__
            ), self.assertRaises(CaptchaTrainingError):
                _load(_FakeSession(runtime=runtime))


if __name__ == "__main__":
    unittest.main()

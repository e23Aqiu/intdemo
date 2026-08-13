import io
import json
import unittest
import zipfile

import numpy as np

from integrated_client.captcha_models import (
    CaptchaModelManager,
    CaptchaTrainingError,
    HogLinearSvmCaptchaModel,
    KnnCaptchaModel,
    _safe_dataset_archive,
)


def _archive(entries):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, value in entries:
            archive.writestr(name, value)
    return output.getvalue()


def _standard_numeric_model():
    labels = np.asarray([["1", "2"]] * 4)
    weights = np.zeros((4, 2, 1_764), dtype=np.float32)
    weights[:, 0, 0] = -1.0
    weights[:, 1, 0] = 1.0
    return HogLinearSvmCaptchaModel(
        "numeric",
        "numeric-hog-test",
        labels,
        np.asarray([2, 2, 2, 2], dtype=np.int32),
        weights,
        np.zeros((4, 2), dtype=np.float32),
    )


def _rewrite_npz(artifact, **replacements):
    with np.load(io.BytesIO(artifact), allow_pickle=False) as source:
        arrays = {name: source[name] for name in source.files}
    arrays.update(replacements)
    output = io.BytesIO()
    np.savez_compressed(output, **arrays)
    return output.getvalue()


class CaptchaDatasetArchiveTests(unittest.TestCase):
    def test_reads_top_level_english_category_directories(self):
        samples = [
            {
                "captcha_type": "numeric",
                "image": "numeric/one.png",
                "answer": {"value": "1234"},
            },
            {
                "captcha_type": "click",
                "image": "click/two.jpg",
                "answer": {"prompt": ["A"], "points": [{"x": 0.5, "y": 0.5}]},
            },
        ]
        archive = _archive(
            [
                ("numeric/", b""),
                ("click/", b""),
                (
                    "manifest.json",
                    json.dumps({"schema_version": 1, "samples": samples}),
                ),
                ("numeric/one.png", b"numeric-image"),
                ("click/two.jpg", b"click-image"),
            ]
        )

        parsed = _safe_dataset_archive(archive)

        self.assertEqual(
            [item["captcha_type"] for item in parsed],
            ["numeric", "click"],
        )
        self.assertEqual(parsed[0]["image"], b"numeric-image")
        self.assertEqual(parsed[1]["image"], b"click-image")

    def test_reads_legacy_nested_images_and_explicit_directories(self):
        samples = [
            {
                "captcha_type": "numeric",
                "image": "images/numeric/one.png",
                "answer": {"value": "1234"},
            },
            {
                "captcha_type": "click",
                "image": "images/click/two.jpg",
                "answer": {"prompt": ["A"], "points": [{"x": 0.5, "y": 0.5}]},
            },
        ]
        archive = _archive(
            [
                ("images/", b""),
                ("images/numeric/", b""),
                ("images/click/", b""),
                (
                    "manifest.json",
                    json.dumps({"schema_version": 1, "samples": samples}),
                ),
                ("images/numeric/one.png", b"numeric-image"),
                ("images/click/two.jpg", b"click-image"),
            ]
        )

        parsed = _safe_dataset_archive(archive)

        self.assertEqual(
            [item["captcha_type"] for item in parsed],
            ["numeric", "click"],
        )
        self.assertEqual(parsed[0]["image"], b"numeric-image")
        self.assertEqual(parsed[1]["image"], b"click-image")

    def test_normalizes_windows_separators_dot_prefix_and_manifest_bom(self):
        samples = [
            {
                "captcha_type": "numeric",
                "image": ".\\images\\numeric\\one.png",
                "answer": {"value": "1234"},
            }
        ]
        manifest = json.dumps(
            {"schema_version": 1, "samples": samples}
        ).encode("utf-8")
        archive = _archive(
            [
                (".\\manifest.json", b"\xef\xbb\xbf" + manifest),
                ("images\\numeric\\one.png", b"numeric-image"),
            ]
        )

        parsed = _safe_dataset_archive(archive)

        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["image"], b"numeric-image")

    def test_rejects_backslash_traversal(self):
        archive = _archive(
            [
                ("manifest.json", json.dumps({"schema_version": 1, "samples": []})),
                ("..\\outside.bin", b"bad"),
            ]
        )

        with self.assertRaises(CaptchaTrainingError):
            _safe_dataset_archive(archive)

    def test_rejects_duplicate_names_after_normalization(self):
        archive = _archive(
            [
                ("manifest.json", json.dumps({"schema_version": 1, "samples": []})),
                ("images/caf\u00e9.png", b"first"),
                ("images/cafe\u0301.png", b"second"),
            ]
        )

        with self.assertRaises(CaptchaTrainingError):
            _safe_dataset_archive(archive)


class CaptchaStandardModelArchiveTests(unittest.TestCase):
    def test_standard_npz_round_trip_and_manager_dispatch(self):
        original = _standard_numeric_model()
        artifact = original.to_bytes()

        loaded = HogLinearSvmCaptchaModel.from_bytes(artifact)
        self.assertEqual(loaded.ALGORITHM, "hog-linear-svm-v1")
        self.assertEqual(loaded.weights.dtype, np.dtype(np.float32))
        manager = CaptchaModelManager()
        manager.install("numeric", original.version, artifact)
        self.assertEqual(manager.version("numeric"), original.version)
        self.assertEqual(manager.algorithm("numeric"), "hog-linear-svm-v1")

    def test_manager_loads_legacy_knn_without_using_it_for_new_training(self):
        legacy = KnnCaptchaModel(
            "numeric",
            "legacy-knn-test",
            np.zeros((1, 24 * 32), dtype=np.float32),
            np.asarray(["1"]),
        )
        manager = CaptchaModelManager()

        manager.install("numeric", legacy.version, legacy.to_bytes())

        self.assertEqual(manager.version("numeric"), legacy.version)
        self.assertEqual(manager.algorithm("numeric"), "knn-pixels-v1")

    def test_manager_binds_artifact_to_expected_algorithm(self):
        model = _standard_numeric_model()
        manager = CaptchaModelManager()

        with self.assertRaises(CaptchaTrainingError):
            manager.install(
                "numeric",
                model.version,
                model.to_bytes(),
                expected_algorithm="tiny-cnn-onnx-v1",
            )

        self.assertEqual(manager.version("numeric"), "ddddocr-builtin")

    def test_standard_model_rejects_non_finite_or_wrong_shape_arrays(self):
        artifact = _standard_numeric_model().to_bytes()
        invalid_weights = np.zeros((4, 2, 1_764), dtype=np.float32)
        invalid_weights[:, :, 0] = 1.0
        invalid_weights[0, 0, 1] = np.nan

        with self.assertRaises(CaptchaTrainingError):
            HogLinearSvmCaptchaModel.from_bytes(
                _rewrite_npz(artifact, weights=invalid_weights)
            )
        with self.assertRaises(CaptchaTrainingError):
            HogLinearSvmCaptchaModel.from_bytes(
                _rewrite_npz(
                    artifact,
                    biases=np.zeros((4, 3), dtype=np.float32),
                )
            )

    def test_standard_model_rejects_pickle_or_unexpected_members(self):
        artifact = _standard_numeric_model().to_bytes()

        with self.assertRaises(CaptchaTrainingError):
            HogLinearSvmCaptchaModel.from_bytes(
                _rewrite_npz(
                    artifact,
                    labels=np.asarray([["1", "2"]] * 4, dtype=object),
                )
            )
        with self.assertRaises(CaptchaTrainingError):
            HogLinearSvmCaptchaModel.from_bytes(
                _rewrite_npz(artifact, unexpected=np.asarray([1]))
            )

if __name__ == "__main__":
    unittest.main()

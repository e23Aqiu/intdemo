import io
import json
import unittest
import zipfile

from integrated_client.captcha_models import (
    CaptchaTrainingError,
    _safe_dataset_archive,
)


def _archive(entries):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, value in entries:
            archive.writestr(name, value)
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
                ("manifest.json", json.dumps({"schema_version": 1, "samples": samples})),
                ("numeric/one.png", b"numeric-image"),
                ("click/two.jpg", b"click-image"),
            ]
        )

        parsed = _safe_dataset_archive(archive)

        self.assertEqual([item["captcha_type"] for item in parsed], ["numeric", "click"])
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
                ("manifest.json", json.dumps({"schema_version": 1, "samples": samples})),
                ("images/numeric/one.png", b"numeric-image"),
                ("images/click/two.jpg", b"click-image"),
            ]
        )

        parsed = _safe_dataset_archive(archive)

        self.assertEqual([item["captcha_type"] for item in parsed], ["numeric", "click"])
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


if __name__ == "__main__":
    unittest.main()

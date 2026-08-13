import hashlib
import io
import json
import stat
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image

from enhanced_trainer import cli, training
from enhanced_trainer.dataset import (
    CaptchaSample,
    DatasetError,
    click_array,
    load_dataset,
    numeric_array,
)
from enhanced_trainer.package_tool import (
    LINUX_ARM64_PLATFORM,
    WINDOWS_RUNTIME_FILES,
    WINDOWS_PLATFORM,
    PackageToolError,
    _synchronize_windows_runtime,
    build_native_bundle,
    package_bundle,
    require_native_platform,
    self_test_bundle,
)
from integrated_client.captcha_models import _cnn_click_tensor
from integrated_client.captcha_tensors import click_candidate_bbox
from enhanced_trainer.protocol import (
    ALGORITHM,
    METADATA_FILE_NAME,
    METRICS_FILE_NAME,
    METRICS_OPTIONAL_FIELDS,
    METRICS_REQUIRED_FIELDS,
    MODEL_FILE_NAME,
    OUTPUT_METADATA_FIELDS,
    TrainerProtocolError,
    atomic_write_bytes,
    atomic_write_json,
    canonical_json_bytes,
    model_metadata,
    output_metadata,
    sha256_bytes,
    validate_generated_outputs,
)
from integrated_client import enhanced_training
from integrated_client.trainer_component import (
    STATUS_AVAILABLE,
    TrainerComponentManager,
)


def _png(width=98, height=41, color=(210, 240, 220)):
    output = io.BytesIO()
    Image.new("RGB", (width, height), color).save(output, format="PNG")
    return output.getvalue()


def _dataset(path, *, traversal=False):
    numeric_image = _png()
    click_image = _png(310, 155, (100, 120, 110))
    samples = [
        {
            "captcha_type": "numeric",
            "image": "numeric/one.png",
            "answer": {"value": "5273"},
            "fingerprint": hashlib.sha256(b"numeric-one").hexdigest(),
        },
        {
            "captcha_type": "click",
            "image": "click/one.png",
            "answer": {
                "prompt": ["深", "比"],
                "points": [{"x": 0.2, "y": 0.2}, {"x": 0.5, "y": 0.6}],
            },
            "fingerprint": hashlib.sha256(b"click-one").hexdigest(),
        },
    ]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "manifest.json",
            json.dumps({"schema_version": 1, "samples": samples}),
        )
        archive.writestr("numeric/one.png", numeric_image)
        archive.writestr("click/one.png", click_image)
        if traversal:
            archive.writestr("..\\outside.bin", b"bad")
    return path


class EnhancedDatasetTests(unittest.TestCase):
    def test_reads_raw_numeric_image_and_prepares_whole_image_tensor(self):
        with tempfile.TemporaryDirectory() as temporary:
            samples = load_dataset(_dataset(Path(temporary) / "data.zip"), "numeric")

        self.assertEqual(len(samples), 1)
        self.assertTrue(samples[0].image.startswith(b"\x89PNG"))
        tensor = numeric_array(
            samples[0],
            augment=False,
            random_state=__import__("random").Random(1),
        )
        self.assertEqual(tensor.shape, (1, 48, 112))
        self.assertEqual(tensor.dtype, np.float32)

    def test_click_candidate_crop_matches_inference_tensor_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            samples = load_dataset(_dataset(Path(temporary) / "data.zip"), "click")
        tensor = click_array(
            samples[0],
            samples[0].answer["points"][0],
            augment=False,
            random_state=__import__("random").Random(1),
        )
        self.assertEqual(tensor.shape, (3, 64, 64))
        self.assertEqual(tensor.dtype, np.float32)
        with Image.open(io.BytesIO(samples[0].image)) as image:
            expected = _cnn_click_tensor(
                image.convert("RGB"),
                click_candidate_bbox(
                    image.size,
                    samples[0].answer["points"][0],
                ),
            )
        np.testing.assert_array_equal(tensor, expected)

    def test_click_candidate_crop_handles_image_edges_and_tiny_images(self):
        random_state = __import__("random").Random(1)
        for size in ((1, 1), (5, 3), (310, 155)):
            image = Image.new("RGB", size, (25, 100, 220))
            for point in (
                {"x": 0.0, "y": 0.0},
                {"x": 1.0, "y": 0.0},
                {"x": 0.0, "y": 1.0},
                {"x": 1.0, "y": 1.0},
            ):
                output = io.BytesIO()
                image.save(output, format="PNG")
                sample = CaptchaSample("click", output.getvalue(), {}, "edge")
                bbox = click_candidate_bbox(size, point)
                tensor = click_array(
                    sample,
                    point,
                    augment=False,
                    random_state=random_state,
                )
                expected = _cnn_click_tensor(image, bbox)

                self.assertEqual(tensor.shape, (3, 64, 64))
                self.assertTrue(np.isfinite(tensor).all())
                np.testing.assert_array_equal(tensor, expected)

    def test_dataset_rejects_backslash_traversal(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = _dataset(Path(temporary) / "data.zip", traversal=True)
            with self.assertRaises(DatasetError):
                load_dataset(path, "numeric")


class EnhancedProtocolTests(unittest.TestCase):
    def test_output_field_sets_match_client_orchestrator(self):
        self.assertEqual(
            OUTPUT_METADATA_FIELDS,
            enhanced_training._METADATA_FIELDS,
        )
        self.assertEqual(
            METRICS_REQUIRED_FIELDS,
            enhanced_training._METRICS_REQUIRED_FIELDS,
        )
        self.assertEqual(
            METRICS_OPTIONAL_FIELDS,
            enhanced_training._METRICS_OPTIONAL_FIELDS,
        )

    def test_model_shapes_match_client_orchestrator_source_contract(self):
        from enhanced_trainer.protocol import (
            CLICK_INPUT_SHAPE,
            NUMERIC_INPUT_SHAPE,
            NUMERIC_OUTPUT_SHAPE,
        )

        self.assertEqual(NUMERIC_INPUT_SHAPE, (1, 1, 48, 112))
        self.assertEqual(NUMERIC_OUTPUT_SHAPE, (1, 4, 10))
        self.assertEqual(CLICK_INPUT_SHAPE, ("N", 3, 64, 64))

    def test_protocol_rejects_client_unknown_metric_fields(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            model = b"fake-onnx"
            metrics = {
                "schema_version": 1,
                "protocol_version": 1,
                "evaluation": "held_out_exact_code_accuracy",
                "train_samples": 16,
                "test_samples": 4,
                "correct_samples": 3,
                "accuracy": 0.75,
                "seed": 1,
                "epochs": 2,
                "batch_size": 4,
                "model_parameters": 123,
            }
            raw = canonical_json_bytes(metrics)
            atomic_write_bytes(root / MODEL_FILE_NAME, model)
            atomic_write_bytes(root / METRICS_FILE_NAME, raw)
            atomic_write_json(
                root / METADATA_FILE_NAME,
                output_metadata(
                    captcha_type="numeric",
                    version="numeric-v1",
                    artifact_sha256=hashlib.sha256(model).hexdigest(),
                    artifact_size=len(model),
                    metrics_sha256=sha256_bytes(raw),
                    sample_count=20,
                    test_count=4,
                    correct_count=3,
                ),
            )
            with self.assertRaisesRegex(TrainerProtocolError, "字段集合"):
                validate_generated_outputs(root)

    def test_training_metric_field_sets_stay_within_protocol_allowlist(self):
        source = Path(training.__file__).read_text(encoding="utf-8")
        for retired_field in (
            '"model_parameters"',
            '"test_crop_count"',
            '"test_crop_accuracy"',
            '"rotation_augmentation_degrees"',
        ):
            self.assertNotIn(retired_field, source)

    def test_click_split_keeps_evaluation_labels_known(self):
        from enhanced_trainer.dataset import CaptchaSample

        samples = []
        for index, label in enumerate(("甲", "甲", "乙", "乙", "甲", "乙")):
            samples.append(
                CaptchaSample(
                    captcha_type="click",
                    image=_png(),
                    answer={
                        "prompt": [label],
                        "points": [{"x": 0.5, "y": 0.5}],
                    },
                    fingerprint=f"{index:064x}",
                )
            )
        train_samples, test_samples = training._split_click_samples(samples)
        known = {
            label
            for sample in train_samples
            for label in sample.answer["prompt"]
        }
        self.assertTrue(test_samples)
        self.assertTrue(
            all(set(sample.answer["prompt"]) <= known for sample in test_samples)
        )

    def test_cli_self_test_uses_protocol_v1_json(self):
        with patch(
            "enhanced_trainer.cli.self_test",
            return_value={
                "ok": True,
                "protocol_version": 1,
                "component_version": "1.0.0",
            },
        ), patch("builtins.print") as output:
            code = cli.main(["self-test", "--protocol-version", "1"])
        self.assertEqual(code, 0)
        payload = json.loads(output.call_args.args[0])
        self.assertTrue(payload["ok"])

    def test_model_metadata_matches_desktop_inference_adapter(self):
        numeric = model_metadata(captcha_type="numeric", version="numeric-v1")
        click = model_metadata(
            captcha_type="click",
            version="click-v1",
            labels=["深", "比"],
        )
        self.assertEqual(numeric["algorithm"], ALGORITHM)
        self.assertEqual(numeric["captcha_type"], "numeric")
        self.assertEqual(json.loads(click["labels_json"]), ["深", "比"])

    def test_output_hash_chain_is_strictly_validated(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            model = b"fake-onnx-for-contract-test"
            metrics = {
                "schema_version": 1,
                "protocol_version": 1,
                "evaluation": "held_out_exact_code_accuracy",
                "train_samples": 16,
                "test_samples": 4,
                "correct_samples": 3,
                "accuracy": 0.75,
                "seed": 1,
                "epochs": 2,
                "batch_size": 4,
                "per_position_accuracy": [1.0, 1.0, 0.75, 0.75],
            }
            metrics_bytes = canonical_json_bytes(metrics)
            atomic_write_bytes(root / MODEL_FILE_NAME, model)
            atomic_write_bytes(root / METRICS_FILE_NAME, metrics_bytes)
            metadata = output_metadata(
                captcha_type="numeric",
                version="numeric-v1",
                artifact_sha256=hashlib.sha256(model).hexdigest(),
                artifact_size=len(model),
                metrics_sha256=sha256_bytes(metrics_bytes),
                sample_count=20,
                test_count=4,
                correct_count=3,
            )
            atomic_write_json(root / METADATA_FILE_NAME, metadata)

            result = validate_generated_outputs(root)
            self.assertEqual(result["metadata"]["correct_count"], 3)
            (root / MODEL_FILE_NAME).write_bytes(b"tampered")
            with self.assertRaisesRegex(TrainerProtocolError, "SHA-256"):
                validate_generated_outputs(root)


class EnhancedPackageToolTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def _bundle(self):
        bundle = self.root / "bundle"
        bundle.mkdir()
        (bundle / "intdemo-trainer.exe").write_bytes(b"native-trainer")
        (bundle / "_internal").mkdir()
        (bundle / "_internal" / "torch.dll").write_bytes(b"runtime")
        return bundle

    def test_native_unsigned_package_installs_through_component_manager(self):
        package = self.root / "trainer.inttrainer"
        with patch(
            "enhanced_trainer.package_tool.native_platform_key",
            return_value=WINDOWS_PLATFORM,
        ):
            package_bundle(
                self._bundle(),
                package,
                requested_platform=WINDOWS_PLATFORM,
            )
        with zipfile.ZipFile(package) as archive:
            manifest = json.loads(archive.read("manifest.json"))
        self.assertNotIn("signature", manifest)

        def runner(command, **_kwargs):
            manifest = json.loads(
                (Path(command[0]).parents[1] / "manifest.json").read_text("utf-8")
            )
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps(
                    {
                        "ok": True,
                        "protocol_version": 1,
                        "component_version": manifest["version"],
                    }
                ),
                stderr="",
            )

        manager = TrainerComponentManager(
            data_dir=self.root / "data",
            platform_key=WINDOWS_PLATFORM,
            current_version="1.1.0",
            runner=runner,
        )
        self.assertEqual(manager.install(package).status.code, STATUS_AVAILABLE)

    def test_cross_package_is_rejected(self):
        with patch(
            "enhanced_trainer.package_tool.native_platform_key",
            return_value=WINDOWS_PLATFORM,
        ), self.assertRaisesRegex(PackageToolError, "禁止交叉打包"):
            require_native_platform(LINUX_ARM64_PLATFORM)

    def test_package_refuses_to_overwrite_existing_artifact(self):
        output = self.root / "existing.inttrainer"
        output.write_bytes(b"keep-me")
        with patch(
            "enhanced_trainer.package_tool.native_platform_key",
            return_value=WINDOWS_PLATFORM,
        ), self.assertRaisesRegex(PackageToolError, "拒绝覆盖"):
            package_bundle(
                self._bundle(),
                output,
                requested_platform=WINDOWS_PLATFORM,
            )
        self.assertEqual(output.read_bytes(), b"keep-me")

    def test_package_materializes_internal_file_symlink_and_rejects_escape(self):
        bundle = self._bundle()
        internal_target = bundle / "_internal" / "torch.dll"
        internal_link = bundle / "_internal" / "torch-alias.dll"
        outside_target = self.root / "outside.dll"
        outside_target.write_bytes(b"outside")
        outside_link = bundle / "_internal" / "outside.dll"
        try:
            internal_link.symlink_to(internal_target.name)
            outside_link.symlink_to(outside_target)
        except (NotImplementedError, OSError) as exc:
            self.skipTest(f"当前测试环境不能创建符号链接：{exc}")

        with patch(
            "enhanced_trainer.package_tool.native_platform_key",
            return_value=WINDOWS_PLATFORM,
        ), self.assertRaisesRegex(PackageToolError, "指向目录外"):
            package_bundle(
                bundle,
                self.root / "escape.inttrainer",
                requested_platform=WINDOWS_PLATFORM,
            )

        outside_link.unlink()
        package = self.root / "symlink.inttrainer"
        with patch(
            "enhanced_trainer.package_tool.native_platform_key",
            return_value=WINDOWS_PLATFORM,
        ):
            package_bundle(
                bundle,
                package,
                requested_platform=WINDOWS_PLATFORM,
            )
        with zipfile.ZipFile(package) as archive:
            alias = archive.getinfo("bin/_internal/torch-alias.dll")
            self.assertEqual(archive.read(alias), internal_target.read_bytes())
            self.assertEqual(
                stat.S_IFMT((alias.external_attr >> 16) & 0xFFFF),
                stat.S_IFREG,
            )

    def test_frozen_bundle_self_test_contract(self):
        bundle = self._bundle()
        def runner(command, **options):
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps(
                    {
                        "ok": True,
                        "protocol_version": 1,
                        "component_version": "1.0.0",
                    }
                ),
                stderr="",
            )

        with patch(
                "enhanced_trainer.package_tool.native_platform_key",
                return_value=WINDOWS_PLATFORM,
            ):
            result = self_test_bundle(
                bundle,
                requested_platform=WINDOWS_PLATFORM,
                runner=runner,
            )
        self.assertTrue(result["ok"])

    def test_native_build_uses_package_hooks_without_collect_all(self):
        commands = []

        def runner(command, **_options):
            commands.append(command)
            dist = Path(command[command.index("--distpath") + 1])
            bundle = dist / "intdemo-trainer"
            bundle.mkdir(parents=True)
            (bundle / "intdemo-trainer.exe").write_bytes(b"trainer")
            return subprocess.CompletedProcess(command, 0)

        build_root = self.root / "native-build"
        with patch(
            "enhanced_trainer.package_tool.native_platform_key",
            return_value=WINDOWS_PLATFORM,
        ), patch(
            "enhanced_trainer.package_tool._require_build_dependencies",
        ), patch(
            "enhanced_trainer.package_tool.self_test_bundle",
        ), patch(
            "enhanced_trainer.package_tool._synchronize_windows_runtime",
        ) as synchronize_runtime:
            bundle = build_native_bundle(
                build_root,
                requested_platform=WINDOWS_PLATFORM,
                runner=runner,
            )

        self.assertEqual(bundle, build_root / "dist" / "intdemo-trainer")
        synchronize_runtime.assert_called_once_with(bundle)
        command = commands[0]
        self.assertNotIn("--collect-all", command)
        hidden = [
            command[index + 1]
            for index, value in enumerate(command)
            if value == "--hidden-import"
        ]
        self.assertEqual(hidden, ["torch", "onnx", "onnxruntime"])

    def test_windows_runtime_set_is_copied_and_hash_verified(self):
        system_root = self.root / "Windows"
        source_root = system_root / "System32"
        source_root.mkdir(parents=True)
        bundle = self.root / "bundle-runtime"
        internal = bundle / "_internal"
        internal.mkdir(parents=True)

        pe_offset = 64
        for index, name in enumerate(WINDOWS_RUNTIME_FILES):
            payload = bytearray(72)
            payload[:2] = b"MZ"
            payload[0x3C:0x40] = pe_offset.to_bytes(4, "little")
            payload[pe_offset:pe_offset + 4] = b"PE\0\0"
            payload[pe_offset + 4:pe_offset + 6] = (0x8664).to_bytes(2, "little")
            payload[-1] = index
            (source_root / name).write_bytes(payload)

        _synchronize_windows_runtime(bundle, system_root=system_root)

        for name in WINDOWS_RUNTIME_FILES:
            self.assertEqual(
                (internal / name).read_bytes(),
                (source_root / name).read_bytes(),
            )

        (source_root / WINDOWS_RUNTIME_FILES[0]).write_bytes(b"not-a-pe")
        with self.assertRaisesRegex(PackageToolError, "Windows 运行库架构"):
            _synchronize_windows_runtime(bundle, system_root=system_root)


if __name__ == "__main__":
    unittest.main()

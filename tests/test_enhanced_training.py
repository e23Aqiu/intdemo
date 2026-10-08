import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from enhanced_trainer.protocol import (
    COMPONENT_VERSION,
    canonical_json_bytes,
    output_metadata,
)
from integrated_client.captcha_models import CaptchaTrainingError
from integrated_client.enhanced_training import (
    EnhancedTrainingCancelled,
    EnhancedTrainingError,
    _run_component_streaming,
    train_enhanced_candidate,
)


class _FakeComponentManager:
    def __init__(self, *, trainer_version=COMPONENT_VERSION):
        self.trainer_version = trainer_version
        self.locked = False
        self.command_calls = []
        self.environment_calls = 0

    @contextmanager
    def training_lock(self):
        if self.locked:
            raise AssertionError("recursive lock")
        self.locked = True
        try:
            yield
        finally:
            self.locked = False

    def command_for(
        self,
        dataset_path,
        captcha_type,
        output_dir,
        base_model_path=None,
    ):
        if not self.locked:
            raise AssertionError("command must be resolved while training is locked")
        command = [
            "intdemo-trainer",
            "train",
            "--protocol-version",
            "2",
            "--dataset",
            str(dataset_path),
            "--captcha-type",
            captcha_type,
            "--output",
            str(output_dir),
        ]
        if base_model_path is not None:
            command.extend(["--base-model", str(base_model_path)])
        self.command_calls.append(command)
        return command

    def training_environment(self):
        if not self.locked:
            raise AssertionError("environment must be resolved while training is locked")
        self.environment_calls += 1
        return {"INTDEMO_TRAINER_PROTOCOL_VERSION": "2", "PATH": "safe"}

    def training_output_paths(self, output_dir):
        if not self.locked:
            raise AssertionError("outputs must be validated while training is locked")
        root = Path(output_dir)
        return {
            "model": root / "candidate.intmodel",
            "metadata": root / "metadata.json",
            "metrics": root / "metrics.json",
        }

    def status(self, *, verify_files=True):
        if not self.locked:
            raise AssertionError("component status must be checked while training is locked")
        return SimpleNamespace(available=True, version=self.trainer_version)


class _OutputRunner:
    def __init__(
        self,
        manager,
        *,
        mutate_metadata=None,
        mutate_metrics=None,
        extra_file=None,
        returncode=0,
        stderr=b"",
    ):
        self.manager = manager
        self.mutate_metadata = mutate_metadata
        self.mutate_metrics = mutate_metrics
        self.extra_file = extra_file
        self.returncode = returncode
        self.stderr = stderr
        self.calls = []
        self.dataset_path = None
        self.base_model_path = None

    def __call__(self, command, **kwargs):
        self.calls.append((list(command), dict(kwargs)))
        if not self.manager.locked:
            raise AssertionError("trainer must run while component is locked")
        dataset_path = Path(command[command.index("--dataset") + 1])
        output_dir = Path(command[command.index("--output") + 1])
        captcha_type = command[command.index("--captcha-type") + 1]
        self.dataset_path = dataset_path
        if dataset_path.read_bytes() != b"dataset-archive":
            raise AssertionError("dataset bytes changed")
        if "--base-model" in command:
            self.base_model_path = Path(
                command[command.index("--base-model") + 1]
            )
            if self.base_model_path.read_bytes() != b"base-artifact":
                raise AssertionError("base model bytes changed")
        if self.returncode:
            kwargs["stderr"].write(self.stderr)
            kwargs["stderr"].flush()
            return subprocess.CompletedProcess(
                command,
                self.returncode,
                stdout=b"untrusted stdout",
                stderr=None,
            )

        artifact = b"portable-engine-model-bundle"
        metrics = {
            "schema_version": 1,
            "protocol_version": 2,
            "evaluation": "held_out_exact_code_accuracy",
            "train_samples": 20,
            "test_samples": 5,
            "correct_samples": 4,
            "accuracy": 0.8,
            "seed": 2_147_483_647,
            "epochs": 8,
            "batch_size": 16,
            "per_position_accuracy": [1.0, 0.8, 0.8, 0.6],
            "right_edge_augmentation": True,
        }
        if self.base_model_path is not None:
            metrics.update(
                {
                    "training_strategy": "continued",
                    "base_model_version": "numeric-base-test",
                }
            )
        if captcha_type == "click":
            metrics.update(
                {
                    "evaluation": "held_out_ordered_click_accuracy",
                    "class_count": 12,
                    "crop_count": 50,
                    "feature_schema": "click-pair-features-v1",
                    "feature_count": 33,
                    "pair_count": 120,
                    "proposal_recall": 0.9,
                    "target_accuracy": 0.85,
                    "baseline_accuracy": 0.75,
                }
            )
            metrics.pop("per_position_accuracy")
            metrics.pop("right_edge_augmentation")
        if self.mutate_metrics:
            self.mutate_metrics(metrics)
        try:
            metrics_raw = canonical_json_bytes(metrics)
        except ValueError:
            # Some negative tests deliberately create a non-finite JSON token.
            metrics_raw = json.dumps(
                metrics,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=True,
            ).encode("utf-8")
        metadata = output_metadata(
            captcha_type=captcha_type,
            version=f"{captcha_type}-enhanced-test",
            artifact_sha256=hashlib.sha256(artifact).hexdigest(),
            artifact_size=len(artifact),
            metrics_sha256=hashlib.sha256(metrics_raw).hexdigest(),
            sample_count=25,
            test_count=5,
            correct_count=4,
        )
        if self.mutate_metadata:
            self.mutate_metadata(metadata)
        (output_dir / "candidate.intmodel").write_bytes(artifact)
        (output_dir / "metadata.json").write_bytes(canonical_json_bytes(metadata))
        (output_dir / "metrics.json").write_bytes(metrics_raw)
        if self.extra_file:
            (output_dir / self.extra_file).write_bytes(b"unexpected")
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=b"this is deliberately not JSON and is ignored",
            stderr=b"",
        )


class EnhancedTrainingTests(unittest.TestCase):
    def test_streaming_runner_reads_live_json_lines_from_real_process(self):
        events = []
        script = (
            "import json\n"
            "print(json.dumps({'event':'started','protocol_version':2,"
            "'sample_count':30,'epochs':2,'batch_size':16,'cpu_threads':4}),"
            " flush=True)\n"
            "print(json.dumps({'event':'epoch','protocol_version':2,"
            "'epoch':1,'epochs':2,'loss':0.25,'train_accuracy':0.75,"
            "'character_accuracy':0.9,'batches':3,'processed_samples':20,"
            "'duration_seconds':1.25,'learning_rate':0.0015}), flush=True)\n"
            "print(json.dumps({'event':'evaluation','protocol_version':2,"
            "'current':1,'total':2,'predicted':'1234','expected':'1234',"
            "'correct':True}), flush=True)\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            with (Path(directory) / "stderr.log").open("w+b") as stderr_stream:
                options = {
                    "cwd": directory,
                    "stdin": subprocess.DEVNULL,
                    "stdout": subprocess.DEVNULL,
                    "stderr": stderr_stream,
                    "text": False,
                    "timeout": 10.0,
                    "check": False,
                    "shell": False,
                }
                if os.name == "nt":
                    options["creationflags"] = getattr(
                        subprocess,
                        "CREATE_NO_WINDOW",
                        0,
                    )
                result = _run_component_streaming(
                    [sys.executable, "-c", script],
                    options,
                    progress_callback=events.append,
                )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            [event["event"] for event in events],
            ["started", "epoch", "evaluation"],
        )
        self.assertIn("1/2", events[1]["message"])
        self.assertIn("训练准确率 75.0%", events[1]["message"])
        self.assertIn("识别结果：1234", events[2]["message"])
        self.assertIn("真实结果：1234", events[2]["message"])
        self.assertEqual(events[1]["progress"], 56)

    def test_streaming_runner_discards_oversized_progress_line(self):
        events = []
        script = (
            "import json\n"
            "print('x' * 20000, flush=True)\n"
            "print(json.dumps({'event':'epoch','protocol_version':2,"
            "'epoch':2,'epochs':2,'loss':0.125}), flush=True)\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            with (Path(directory) / "stderr.log").open("w+b") as stderr_stream:
                options = {
                    "cwd": directory,
                    "stdin": subprocess.DEVNULL,
                    "stdout": subprocess.DEVNULL,
                    "stderr": stderr_stream,
                    "text": False,
                    "timeout": 10.0,
                    "check": False,
                    "shell": False,
                }
                if os.name == "nt":
                    options["creationflags"] = getattr(
                        subprocess,
                        "CREATE_NO_WINDOW",
                        0,
                    )
                result = _run_component_streaming(
                    [sys.executable, "-c", script],
                    options,
                    progress_callback=events.append,
                )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "epoch")
        self.assertEqual(events[0]["progress"], 80)

    def test_streaming_runner_terminates_process_when_cancelled(self):
        cancel_event = threading.Event()
        timer = threading.Timer(0.2, cancel_event.set)
        script = "import time\ntime.sleep(30)\n"
        with tempfile.TemporaryDirectory() as directory:
            with (Path(directory) / "stderr.log").open("w+b") as stderr_stream:
                options = {
                    "cwd": directory,
                    "stdin": subprocess.DEVNULL,
                    "stdout": subprocess.DEVNULL,
                    "stderr": stderr_stream,
                    "text": False,
                    "check": False,
                    "shell": False,
                }
                if os.name == "nt":
                    options["creationflags"] = getattr(
                        subprocess,
                        "CREATE_NO_WINDOW",
                        0,
                    )
                timer.start()
                try:
                    with self.assertRaisesRegex(
                        EnhancedTrainingCancelled,
                        "用户中止",
                    ):
                        _run_component_streaming(
                            [sys.executable, "-c", script],
                            options,
                            progress_callback=lambda _event: None,
                            cancel_event=cancel_event,
                        )
                finally:
                    timer.cancel()

    def test_success_runs_locked_without_shell_and_returns_verified_candidate(self):
        manager = _FakeComponentManager()
        runner = _OutputRunner(manager)

        with patch(
            "integrated_client.enhanced_training.LocalCaptchaEngineClient",
        ) as engine_client:
            candidate = train_enhanced_candidate(
                b"dataset-archive",
                "numeric",
                manager,
                runner=runner,
            )

        self.assertEqual(candidate.algorithm, "captcha-engine-v1")
        self.assertEqual(candidate.version, "numeric-enhanced-test")
        self.assertEqual(candidate.sample_count, 25)
        self.assertEqual(candidate.test_count, 5)
        self.assertEqual(candidate.correct_count, 4)
        self.assertEqual(candidate.metrics["training_mode"], "enhanced")
        self.assertEqual(candidate.metrics["trainer_version"], COMPONENT_VERSION)
        engine_client.assert_called_once_with(manager)
        engine_client.return_value.load_model.assert_called_once_with(
            "numeric",
            "numeric-enhanced-test",
            b"portable-engine-model-bundle",
        )
        engine_client.return_value.stop.assert_called_once_with()
        command, options = runner.calls[0]
        self.assertEqual(command[1:4], ["train", "--protocol-version", "2"])
        self.assertIs(options["stdin"], subprocess.DEVNULL)
        self.assertIs(options["stdout"], subprocess.DEVNULL)
        self.assertTrue(hasattr(options["stderr"], "write"))
        self.assertIs(options["text"], False)
        self.assertIs(options["check"], False)
        self.assertIs(options["shell"], False)
        self.assertNotIn("timeout", options)
        self.assertEqual(options["env"]["INTDEMO_TRAINER_PROTOCOL_VERSION"], "2")
        if os.name == "nt":
            self.assertEqual(
                options["creationflags"],
                getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        else:
            self.assertNotIn("creationflags", options)
        self.assertFalse(manager.locked)
        self.assertFalse(runner.dataset_path.exists())

    def test_selected_epochs_are_forwarded_and_verified(self):
        manager = _FakeComponentManager()
        runner = _OutputRunner(
            manager,
            mutate_metrics=lambda value: value.update({"epochs": 37}),
        )

        with patch(
            "integrated_client.enhanced_training.LocalCaptchaEngineClient",
        ):
            candidate = train_enhanced_candidate(
                b"dataset-archive",
                "numeric",
                manager,
                runner=runner,
                epochs=37,
            )

        self.assertEqual(candidate.metrics["epochs"], 37)
        self.assertEqual(
            runner.calls[0][1]["env"]["INTDEMO_TRAINER_EPOCHS"],
            "37",
        )
        for invalid in (True, 0, 201):
            with self.subTest(epochs=invalid), self.assertRaises(
                EnhancedTrainingError
            ):
                train_enhanced_candidate(
                    b"dataset-archive",
                    "numeric",
                    manager,
                    runner=runner,
                    epochs=invalid,
                )

    def test_base_model_is_passed_to_component_for_continued_training(self):
        manager = _FakeComponentManager()
        runner = _OutputRunner(manager)

        with patch(
            "integrated_client.enhanced_training.LocalCaptchaEngineClient",
        ):
            candidate = train_enhanced_candidate(
                b"dataset-archive",
                "numeric",
                manager,
                runner=runner,
                base_artifact=b"base-artifact",
            )

        self.assertIn("--base-model", runner.calls[0][0])
        self.assertEqual(candidate.metrics["training_strategy"], "continued")
        self.assertEqual(
            candidate.metrics["base_model_version"],
            "numeric-base-test",
        )
        self.assertIsNotNone(runner.base_model_path)
        self.assertFalse(runner.base_model_path.exists())

    def test_click_protocol_supports_class_and_crop_metrics(self):
        manager = _FakeComponentManager()
        runner = _OutputRunner(manager)

        with patch(
            "integrated_client.enhanced_training.LocalCaptchaEngineClient",
        ) as engine_client:
            candidate = train_enhanced_candidate(
                b"dataset-archive",
                "click",
                manager,
                runner=runner,
            )

        self.assertEqual(candidate.captcha_type, "click")
        self.assertEqual(candidate.metrics["class_count"], 12)
        self.assertEqual(candidate.metrics["crop_count"], 50)
        self.assertEqual(candidate.metrics["feature_count"], 33)
        self.assertNotIn("per_position_accuracy", candidate.metrics)
        engine_client.return_value.load_model.assert_called_once_with(
            "click",
            "click-enhanced-test",
            b"portable-engine-model-bundle",
        )

    def test_progress_callback_translates_component_json_lines(self):
        manager = _FakeComponentManager()
        runner = _OutputRunner(manager)
        events = []
        original_call = runner.__call__

        def event_runner(command, **kwargs):
            result = original_call(command, **kwargs)
            result.stdout = (
                b'{"event":"started","protocol_version":2,'
                b'"sample_count":25,"epochs":24,"batch_size":16,'
                b'"cpu_threads":4}\n'
                b'{"event":"epoch","protocol_version":2,'
                b'"epoch":12,"epochs":24,"loss":0.125}\n'
                b'{"event":"evaluation","protocol_version":2,'
                b'"current":1,"total":5,"predicted":"4826",'
                b'"expected":"4828","correct":false}\n'
                b'{"event":"completed","protocol_version":2,'
                b'"accuracy":0.8,"correct_count":4,"test_count":5}\n'
            )
            return result

        with patch(
            "integrated_client.enhanced_training.LocalCaptchaEngineClient",
        ):
            train_enhanced_candidate(
                b"dataset-archive",
                "numeric",
                manager,
                runner=event_runner,
                progress_callback=events.append,
            )

        event_names = [event["event"] for event in events]
        self.assertEqual(
            event_names,
            [
                "preparing",
                "started",
                "epoch",
                "evaluation",
                "completed",
                "validating",
                "validated",
            ],
        )
        self.assertIn("12/24", events[2]["message"])
        self.assertIn("0.125000", events[2]["message"])
        self.assertIn("识别结果：4826", events[3]["message"])
        self.assertIn("真实结果：4828", events[3]["message"])
        self.assertIn("正确 4/5", events[4]["message"])
        self.assertEqual(events[-1]["progress"], 90)

    def test_failure_uses_bounded_sanitized_stderr_and_ignores_stdout(self):
        manager = _FakeComponentManager()
        runner = _OutputRunner(
            manager,
            returncode=9,
            stderr=(b"bad\x00 message\n" + b"x" * 2_000 + b"secret-tail"),
        )

        with self.assertRaises(EnhancedTrainingError) as captured:
            train_enhanced_candidate(
                b"dataset-archive",
                "numeric",
                manager,
                runner=runner,
            )

        message = str(captured.exception)
        self.assertIn("bad message", message)
        self.assertNotIn("secret-tail", message)
        self.assertLessEqual(len(message), 1_050)
        self.assertFalse(manager.locked)

    def test_cancelled_before_start_does_not_run_or_hold_training_lock(self):
        manager = _FakeComponentManager()
        cancel_event = threading.Event()
        cancel_event.set()

        def unexpected_runner(*_args, **_kwargs):
            raise AssertionError("cancelled training must not start")

        with self.assertRaisesRegex(EnhancedTrainingCancelled, "用户中止"):
            train_enhanced_candidate(
                b"dataset-archive",
                "numeric",
                manager,
                runner=unexpected_runner,
                cancel_event=cancel_event,
            )
        self.assertFalse(manager.locked)

    def test_rejects_unknown_output_file_and_unknown_metadata_field(self):
        cases = [
            _OutputRunner(_FakeComponentManager(), extra_file="untrusted.bin"),
            _OutputRunner(
                _FakeComponentManager(),
                mutate_metadata=lambda value: value.update({"unknown": True}),
            ),
        ]
        for runner in cases:
            with self.subTest(extra=runner.extra_file), patch(
                "integrated_client.enhanced_training.LocalCaptchaEngineClient",
            ), self.assertRaises(EnhancedTrainingError):
                train_enhanced_candidate(
                    b"dataset-archive",
                    "numeric",
                    runner.manager,
                    runner=runner,
                )

    def test_rejects_conflicting_counts_hashes_and_non_finite_metrics(self):
        mutations = [
            (
                lambda value: value.update({"sample_count": 26}),
                None,
            ),
            (
                lambda value: value.update({"artifact_sha256": "0" * 64}),
                None,
            ),
            (
                None,
                lambda value: value.update({"accuracy": float("nan")}),
            ),
            (
                None,
                lambda value: value.update({"correct_samples": 6}),
            ),
        ]
        for metadata_mutation, metrics_mutation in mutations:
            manager = _FakeComponentManager()
            runner = _OutputRunner(
                manager,
                mutate_metadata=metadata_mutation,
                mutate_metrics=metrics_mutation,
            )
            with self.subTest(
                metadata=metadata_mutation is not None,
                metrics=metrics_mutation is not None,
            ), patch(
                "integrated_client.enhanced_training.LocalCaptchaEngineClient",
            ), self.assertRaises(EnhancedTrainingError):
                train_enhanced_candidate(
                    b"dataset-archive",
                    "numeric",
                    manager,
                    runner=runner,
                )

    def test_rejects_component_version_mismatch_and_invalid_model_bundle(self):
        manager = _FakeComponentManager()
        mismatch = _OutputRunner(
            manager,
            mutate_metadata=lambda value: value.update({"trainer_version": "9.9.9"}),
        )
        with self.assertRaises(EnhancedTrainingError):
            train_enhanced_candidate(
                b"dataset-archive",
                "numeric",
                manager,
                runner=mismatch,
            )

        runner = _OutputRunner(manager)
        with patch(
            "integrated_client.enhanced_training.LocalCaptchaEngineClient",
        ) as engine_client, self.assertRaisesRegex(
            EnhancedTrainingError,
            "模型加载验证失败",
        ):
            engine_client.return_value.load_model.side_effect = CaptchaTrainingError(
                "invalid model bundle"
            )
            train_enhanced_candidate(
                b"dataset-archive",
                "numeric",
                manager,
                runner=runner,
            )

    def test_rejects_invalid_arguments_before_starting_component(self):
        manager = _FakeComponentManager()
        runner = _OutputRunner(manager)
        cases = [
            (b"", "numeric", None),
            (b"dataset-archive", "unknown", None),
            (b"dataset-archive", "numeric", 0),
            (b"dataset-archive", "numeric", float("inf")),
        ]
        for archive, captcha_type, epochs in cases:
            with self.subTest(
                captcha_type=captcha_type,
                epochs=epochs,
            ), self.assertRaises(EnhancedTrainingError):
                train_enhanced_candidate(
                    archive,
                    captcha_type,
                    manager,
                    runner=runner,
                    epochs=epochs,
                )
        self.assertFalse(runner.calls)


if __name__ == "__main__":
    unittest.main()

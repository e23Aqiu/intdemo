"""CPU training, evaluation and ONNX export for the enhanced component."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import random
import shutil
import tempfile
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from .dataset import (
    CaptchaSample,
    click_array,
    load_dataset,
    numeric_array,
    split_samples,
)
from .models import build_click_model, build_numeric_model
from .protocol import (
    ALGORITHM,
    COMPONENT_VERSION,
    METADATA_FILE_NAME,
    METRICS_FILE_NAME,
    MODEL_FILE_NAME,
    OUTPUT_SCHEMA_VERSION,
    PROTOCOL_VERSION,
    TRAINING_MODE,
    atomic_write_bytes,
    atomic_write_json,
    canonical_json_bytes,
    model_metadata,
    output_metadata,
    sha256_bytes,
    sha256_file,
    validate_generated_outputs,
)

MIN_NUMERIC_SAMPLES = 20
MIN_CLICK_SAMPLES = 30
DEFAULT_EPOCHS = 24
MAX_EPOCHS = 200
DEFAULT_BATCH_SIZE = 32
MAX_BATCH_SIZE = 128
ONNX_OPSET_VERSION = 17


class TrainingError(RuntimeError):
    pass


def _dependency(name: str):
    try:
        return __import__(name)
    except ImportError as exc:
        raise TrainingError(f"强化训练组件缺少依赖：{name}") from exc


def _bounded_environment_integer(name: str, default: int, maximum: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise TrainingError(f"环境变量 {name} 必须是整数") from exc
    if not 1 <= value <= maximum:
        raise TrainingError(f"环境变量 {name} 必须在 1 到 {maximum} 之间")
    return value


def _emit(event: str, **payload) -> None:
    print(
        json.dumps(
            {"event": event, "protocol_version": PROTOCOL_VERSION, **payload},
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ),
        flush=True,
    )


def _dataset_seed(samples: list[CaptchaSample], captcha_type: str) -> int:
    digest = hashlib.sha256(captcha_type.encode("ascii"))
    for sample in samples:
        digest.update(sample.fingerprint.encode("ascii"))
    return int.from_bytes(digest.digest()[:8], "big") & 0x7FFF_FFFF


def _model_version(
    samples: list[CaptchaSample],
    captcha_type: str,
) -> str:
    digest = hashlib.sha256(captcha_type.encode("ascii"))
    for sample in samples:
        digest.update(sample.fingerprint.encode("ascii"))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    return f"{captcha_type}-tiny-cnn-{stamp}-{digest.hexdigest()[:8]}"


def _prepare_torch(seed: int):
    torch = _dependency("torch")
    torch.manual_seed(seed)
    threads_default = max(1, min(4, os.cpu_count() or 1))
    threads = _bounded_environment_integer(
        "INTDEMO_TRAINER_THREADS",
        threads_default,
        64,
    )
    torch.set_num_threads(threads)
    try:
        torch.set_num_interop_threads(1)
    except RuntimeError:
        pass
    return torch, threads


class _NumericDataset:
    def __init__(self, samples, *, augment, seed, torch):
        self.samples = list(samples)
        self.augment = augment
        self.seed = int(seed)
        self.epoch = 0
        self.torch = torch

    def __len__(self):
        return len(self.samples)

    def __getitem__(self, index):
        sample = self.samples[index]
        state = random.Random(self.seed + self.epoch * 1_000_003 + index)
        image = numeric_array(sample, augment=self.augment, random_state=state)
        labels = [int(value) for value in sample.answer["value"]]
        return self.torch.from_numpy(image), self.torch.tensor(
            labels,
            dtype=self.torch.long,
        )


class _ClickDataset:
    def __init__(self, entries, label_indexes, *, augment, seed, torch):
        self.entries = list(entries)
        self.label_indexes = dict(label_indexes)
        self.augment = augment
        self.seed = int(seed)
        self.epoch = 0
        self.torch = torch

    def __len__(self):
        return len(self.entries)

    def __getitem__(self, index):
        sample, label, point = self.entries[index]
        state = random.Random(self.seed + self.epoch * 1_000_003 + index)
        image = click_array(
            sample,
            point,
            augment=self.augment,
            random_state=state,
        )
        return self.torch.from_numpy(image), self.torch.tensor(
            self.label_indexes[label],
            dtype=self.torch.long,
        )


def _loader(torch, dataset, *, batch_size, shuffle, seed):
    generator = torch.Generator()
    generator.manual_seed(seed)
    return torch.utils.data.DataLoader(
        dataset,
        batch_size=min(batch_size, max(1, len(dataset))),
        shuffle=shuffle,
        num_workers=0,
        generator=generator,
        pin_memory=False,
        drop_last=False,
    )


def _train_numeric(torch, samples, epochs, batch_size, seed):
    train_samples, test_samples = split_samples(samples)
    train_dataset = _NumericDataset(
        train_samples,
        augment=True,
        seed=seed,
        torch=torch,
    )
    test_dataset = _NumericDataset(
        test_samples,
        augment=False,
        seed=seed,
        torch=torch,
    )
    model = build_numeric_model(torch)
    optimizer = torch.optim.AdamW(model.parameters(), lr=1.5e-3, weight_decay=1e-4)
    criterion = torch.nn.CrossEntropyLoss()
    model.train()
    for epoch in range(epochs):
        epoch_started = time.perf_counter()
        train_dataset.epoch = epoch
        loader = _loader(
            torch,
            train_dataset,
            batch_size=batch_size,
            shuffle=True,
            seed=seed + epoch,
        )
        loss_total = 0.0
        item_count = 0
        batch_count = 0
        exact_correct = 0
        character_correct = 0
        for images, targets in loader:
            optimizer.zero_grad(set_to_none=True)
            logits = model(images)
            loss = sum(
                criterion(logits[:, position, :], targets[:, position])
                for position in range(4)
            ) / 4
            loss.backward()
            optimizer.step()
            loss_total += float(loss.detach()) * len(images)
            item_count += len(images)
            batch_count += 1
            with torch.no_grad():
                predictions = logits.detach().argmax(dim=2)
                matches = predictions.eq(targets)
                exact_correct += int(matches.all(dim=1).sum().item())
                character_correct += int(matches.sum().item())
        _emit(
            "epoch",
            captcha_type="numeric",
            epoch=epoch + 1,
            epochs=epochs,
            loss=loss_total / max(1, item_count),
            train_accuracy=exact_correct / max(1, item_count),
            character_accuracy=character_correct / max(1, item_count * 4),
            batches=batch_count,
            processed_samples=item_count,
            duration_seconds=time.perf_counter() - epoch_started,
            learning_rate=float(optimizer.param_groups[0]["lr"]),
        )

    model.eval()
    correct = 0
    evaluated = 0
    position_correct = [0, 0, 0, 0]
    with torch.no_grad():
        for images, targets in _loader(
            torch,
            test_dataset,
            batch_size=batch_size,
            shuffle=False,
            seed=seed,
        ):
            predictions = model(images).argmax(dim=2)
            matches = predictions.eq(targets)
            correct += int(matches.all(dim=1).sum().item())
            predicted_rows = predictions.tolist()
            expected_rows = targets.tolist()
            for predicted_row, expected_row in zip(
                predicted_rows,
                expected_rows,
            ):
                predicted_text = "".join(str(int(value)) for value in predicted_row)
                expected_text = "".join(str(int(value)) for value in expected_row)
                evaluated += 1
                _emit(
                    "evaluation",
                    captcha_type="numeric",
                    current=evaluated,
                    total=len(test_samples),
                    predicted=predicted_text,
                    expected=expected_text,
                    correct=predicted_text == expected_text,
                )
            for position in range(4):
                position_correct[position] += int(matches[:, position].sum().item())
    metrics = {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "evaluation": "held_out_exact_code_accuracy",
        "train_samples": len(train_samples),
        "test_samples": evaluated,
        "correct_samples": correct,
        "accuracy": correct / evaluated,
        "seed": seed,
        "epochs": epochs,
        "batch_size": batch_size,
        "per_position_accuracy": [value / evaluated for value in position_correct],
        "right_edge_augmentation": True,
    }
    return model, None, metrics


def _click_entries(samples):
    entries = []
    for sample in samples:
        for label, point in zip(
            sample.answer["prompt"],
            sample.answer["points"],
        ):
            entries.append((sample, str(label), point))
    return entries


def _split_click_samples(samples):
    """Keep every evaluation label represented in the training partition."""

    train_samples, test_samples = split_samples(samples)
    known_labels = {
        str(label)
        for sample in train_samples
        for label in sample.answer["prompt"]
    }
    evaluation = []
    for sample in test_samples:
        labels = {str(label) for label in sample.answer["prompt"]}
        if labels <= known_labels:
            evaluation.append(sample)
        else:
            train_samples.append(sample)
            known_labels.update(labels)
    if not evaluation:
        coverage = Counter(
            label
            for sample in train_samples
            for label in {str(value) for value in sample.answer["prompt"]}
        )
        for sample in reversed(train_samples):
            labels = {str(label) for label in sample.answer["prompt"]}
            if all(coverage[label] > 1 for label in labels):
                train_samples.remove(sample)
                evaluation.append(sample)
                break
    if not evaluation:
        raise TrainingError("点选验证码缺少可独立留出的已知字符测试样本")
    return train_samples, evaluation


def _train_click(torch, samples, epochs, batch_size, seed):
    train_samples, test_samples = _split_click_samples(samples)
    train_entries = _click_entries(train_samples)
    labels = sorted({label for _sample, label, _point in train_entries})
    if len(labels) < 2:
        raise TrainingError("点选验证码至少需要两种训练字符")
    label_indexes = {label: index for index, label in enumerate(labels)}
    train_dataset = _ClickDataset(
        train_entries,
        label_indexes,
        augment=True,
        seed=seed,
        torch=torch,
    )
    counts = Counter(label for _sample, label, _point in train_entries)
    class_weights = torch.tensor(
        [len(train_entries) / (len(labels) * counts[label]) for label in labels],
        dtype=torch.float32,
    )
    model = build_click_model(len(labels), torch)
    optimizer = torch.optim.AdamW(model.parameters(), lr=1.5e-3, weight_decay=1e-4)
    criterion = torch.nn.CrossEntropyLoss(weight=class_weights)
    model.train()
    for epoch in range(epochs):
        epoch_started = time.perf_counter()
        train_dataset.epoch = epoch
        loader = _loader(
            torch,
            train_dataset,
            batch_size=batch_size,
            shuffle=True,
            seed=seed + epoch,
        )
        loss_total = 0.0
        item_count = 0
        batch_count = 0
        item_correct = 0
        for images, targets in loader:
            optimizer.zero_grad(set_to_none=True)
            logits = model(images)
            loss = criterion(logits, targets)
            loss.backward()
            optimizer.step()
            loss_total += float(loss.detach()) * len(images)
            item_count += len(images)
            batch_count += 1
            with torch.no_grad():
                item_correct += int(
                    logits.detach().argmax(dim=1).eq(targets).sum().item()
                )
        _emit(
            "epoch",
            captcha_type="click",
            epoch=epoch + 1,
            epochs=epochs,
            loss=loss_total / max(1, item_count),
            train_accuracy=item_correct / max(1, item_count),
            batches=batch_count,
            processed_samples=item_count,
            duration_seconds=time.perf_counter() - epoch_started,
            learning_rate=float(optimizer.param_groups[0]["lr"]),
        )

    model.eval()
    correct_samples = 0
    evaluated_samples = 0
    with torch.no_grad():
        for sample in test_samples:
            expected = [str(label) for label in sample.answer["prompt"]]
            if any(label not in label_indexes for label in expected):
                continue
            arrays = [
                click_array(
                    sample,
                    point,
                    augment=False,
                    random_state=random.Random(seed),
                )
                for point in sample.answer["points"]
            ]
            if not arrays:
                continue
            tensor = torch.from_numpy(np.stack(arrays))
            predictions = model(tensor).argmax(dim=1).tolist()
            predicted = [labels[index] for index in predictions]
            evaluated_samples += 1
            matched = predicted == expected
            correct_samples += int(matched)
            _emit(
                "evaluation",
                captcha_type="click",
                current=evaluated_samples,
                total=len(test_samples),
                predicted="、".join(predicted),
                expected="、".join(expected),
                correct=matched,
            )
    if not evaluated_samples:
        raise TrainingError("点选验证码测试集没有可评估的已知字符")
    metrics = {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "evaluation": "held_out_target_crop_sequence_accuracy",
        "train_samples": len(train_samples),
        "test_samples": evaluated_samples,
        "correct_samples": correct_samples,
        "accuracy": correct_samples / evaluated_samples,
        "seed": seed,
        "epochs": epochs,
        "batch_size": batch_size,
        "class_count": len(labels),
        "crop_count": len(train_entries),
    }
    return model, labels, metrics


def _export_onnx(torch, model, captcha_type, labels, version, destination):
    onnx = _dependency("onnx")
    model.eval()
    if captcha_type == "numeric":
        example = torch.zeros((1, 1, 48, 112), dtype=torch.float32)
        dynamic_axes = None
    else:
        example = torch.zeros((1, 3, 64, 64), dtype=torch.float32)
        dynamic_axes = {"input": {0: "N"}, "logits": {0: "N"}}
    export_arguments = {
        "input_names": ["input"],
        "output_names": ["logits"],
        "opset_version": ONNX_OPSET_VERSION,
        "do_constant_folding": True,
        "dynamic_axes": dynamic_axes,
    }
    try:
        torch.onnx.export(
            model,
            example,
            str(destination),
            dynamo=False,
            **export_arguments,
        )
    except TypeError:
        torch.onnx.export(model, example, str(destination), **export_arguments)

    graph = onnx.load_model(str(destination), load_external_data=False)
    while graph.metadata_props:
        graph.metadata_props.pop()
    metadata = model_metadata(
        captcha_type=captcha_type,
        version=version,
        labels=labels if captcha_type == "click" else None,
    )
    metadata["labels_json"] = json.dumps(
        labels if captcha_type == "click" else [str(value) for value in range(10)],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    metadata["output_layout"] = (
        "batch,position,digit" if captcha_type == "numeric" else "batch,class"
    )
    metadata["onnx_opset"] = str(ONNX_OPSET_VERSION)
    for key, value in metadata.items():
        property_value = graph.metadata_props.add()
        property_value.key = key
        property_value.value = value
    graph.producer_name = "intdemo-enhanced-trainer"
    graph.producer_version = COMPONENT_VERSION
    onnx.checker.check_model(graph, full_check=True)
    onnx.save_model(graph, str(destination), save_as_external_data=False)
    if destination.stat().st_size > 20 * 1024 * 1024:
        raise TrainingError("导出的强化 ONNX 模型超过 20 MiB")


def _verify_onnx_runtime_contract(
    captcha_type: str,
    version: str,
    model_path: Path,
) -> None:
    """Exercise the exported artifact with the same ORT contract as client."""

    ort = _dependency("onnxruntime")
    try:
        session = ort.InferenceSession(
            str(model_path),
            providers=["CPUExecutionProvider"],
        )
        inputs = session.get_inputs()
        outputs = session.get_outputs()
        metadata = dict(session.get_modelmeta().custom_metadata_map or {})
        if (
            len(inputs) != 1
            or len(outputs) != 1
            or inputs[0].type != "tensor(float)"
            or outputs[0].type != "tensor(float)"
            or metadata.get("algorithm") != ALGORITHM
            or metadata.get("captcha_type") != captcha_type
            or metadata.get("version") != version
        ):
            raise ValueError("I/O or metadata mismatch")
        if captcha_type == "numeric":
            tensor = np.zeros((1, 1, 48, 112), dtype=np.float32)
            expected = (1, 4, 10)
        else:
            tensor = np.zeros((2, 3, 64, 64), dtype=np.float32)
            labels = json.loads(metadata.get("labels_json") or "")
            if not isinstance(labels, list) or len(labels) < 2:
                raise ValueError("click labels missing")
            expected = (2, len(labels))
        result = np.asarray(
            session.run([outputs[0].name], {inputs[0].name: tensor})[0],
            dtype=np.float32,
        )
        if result.shape != expected or not np.isfinite(result).all():
            raise ValueError("inference output mismatch")
    except Exception as exc:
        raise TrainingError(f"导出的强化 ONNX 模型运行时自检失败：{exc}") from exc


def train(
    dataset_path: Path | str,
    captcha_type: str,
    output_dir: Path | str,
) -> dict[str, object]:
    """Train one model and atomically publish protocol-v1 output files."""

    selected_type = str(captcha_type or "").strip().casefold()
    samples = load_dataset(dataset_path, selected_type)
    minimum = MIN_NUMERIC_SAMPLES if selected_type == "numeric" else MIN_CLICK_SAMPLES
    if len(samples) < minimum:
        raise TrainingError(
            f"{selected_type} 强化训练至少需要 {minimum} 个有效成功样本"
        )
    seed = _dataset_seed(samples, selected_type)
    torch, threads = _prepare_torch(seed)
    epochs = _bounded_environment_integer(
        "INTDEMO_TRAINER_EPOCHS",
        DEFAULT_EPOCHS,
        MAX_EPOCHS,
    )
    default_batch = 16 if platform.machine().casefold() in {"aarch64", "arm64"} else (
        DEFAULT_BATCH_SIZE
    )
    batch_size = _bounded_environment_integer(
        "INTDEMO_TRAINER_BATCH_SIZE",
        default_batch,
        MAX_BATCH_SIZE,
    )
    _emit(
        "started",
        captcha_type=selected_type,
        sample_count=len(samples),
        epochs=epochs,
        batch_size=batch_size,
        cpu_threads=threads,
    )
    if selected_type == "numeric":
        model, labels, metrics = _train_numeric(
            torch,
            samples,
            epochs,
            batch_size,
            seed,
        )
    else:
        model, labels, metrics = _train_click(
            torch,
            samples,
            epochs,
            batch_size,
            seed,
        )
    version = _model_version(samples, selected_type)

    output = Path(output_dir).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    final_paths = [
        output / MODEL_FILE_NAME,
        output / METADATA_FILE_NAME,
        output / METRICS_FILE_NAME,
    ]
    if any(path.exists() for path in final_paths):
        raise TrainingError("强化训练输出目录中已存在候选模型文件")
    staging = Path(tempfile.mkdtemp(prefix=".training-", dir=str(output)))
    moved = []
    try:
        model_path = staging / MODEL_FILE_NAME
        _export_onnx(
            torch,
            model,
            selected_type,
            labels,
            version,
            model_path,
        )
        _verify_onnx_runtime_contract(selected_type, version, model_path)
        metrics_bytes = canonical_json_bytes(metrics)
        atomic_write_bytes(staging / METRICS_FILE_NAME, metrics_bytes)
        metadata = output_metadata(
            captcha_type=selected_type,
            version=version,
            artifact_sha256=sha256_file(model_path),
            artifact_size=model_path.stat().st_size,
            metrics_sha256=sha256_bytes(metrics_bytes),
            sample_count=len(samples),
            test_count=int(metrics["test_samples"]),
            correct_count=int(metrics["correct_samples"]),
        )
        atomic_write_json(staging / METADATA_FILE_NAME, metadata)
        validate_generated_outputs(staging)
        for name in (MODEL_FILE_NAME, METRICS_FILE_NAME, METADATA_FILE_NAME):
            source = staging / name
            target = output / name
            os.replace(source, target)
            moved.append(target)
        result = validate_generated_outputs(output)
    except Exception:
        for path in moved:
            path.unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    del model
    _emit(
        "completed",
        captcha_type=selected_type,
        version=version,
        accuracy=float(metrics["accuracy"]),
        test_count=int(metrics["test_samples"]),
        correct_count=int(metrics["correct_samples"]),
    )
    return result


def self_test() -> dict[str, object]:
    """Exercise Torch, ONNX export and ONNX Runtime without training."""

    torch, threads = _prepare_torch(20260813)
    _dependency("onnx")
    _dependency("onnxruntime")
    numeric = build_numeric_model(torch).eval()
    click = build_click_model(2, torch).eval()
    with torch.no_grad():
        numeric_output = numeric(torch.zeros((1, 1, 48, 112)))
        click_output = click(torch.zeros((2, 3, 64, 64)))
    if tuple(numeric_output.shape) != (1, 4, 10):
        raise TrainingError("数字 Tiny CNN 自检输出形状无效")
    if tuple(click_output.shape) != (2, 2):
        raise TrainingError("点选 Tiny CNN 自检输出形状无效")
    with tempfile.TemporaryDirectory(prefix="intdemo-trainer-self-test-") as root:
        directory = Path(root)
        numeric_path = directory / "numeric.onnx"
        click_path = directory / "click.onnx"
        _export_onnx(
            torch,
            numeric,
            "numeric",
            None,
            "numeric-self-test",
            numeric_path,
        )
        _verify_onnx_runtime_contract(
            "numeric",
            "numeric-self-test",
            numeric_path,
        )
        _export_onnx(
            torch,
            click,
            "click",
            ["甲", "乙"],
            "click-self-test",
            click_path,
        )
        _verify_onnx_runtime_contract(
            "click",
            "click-self-test",
            click_path,
        )
    return {
        "ok": True,
        "protocol_version": PROTOCOL_VERSION,
        "component_version": COMPONENT_VERSION,
        "algorithm": ALGORITHM,
        "training_mode": TRAINING_MODE,
        "torch_version": str(torch.__version__),
        "cpu_threads": threads,
    }

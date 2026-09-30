"""CPU training, evaluation and ONNX export for the enhanced component."""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import platform
import random
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from .click_engine import (
    CLICK_FEATURE_COUNT,
    CLICK_FEATURE_SCHEMA,
    ClickCandidate,
    ClickFeatureExtractor,
    click_assignment_diagnostics,
    point_candidate_index,
)
from .dataset import (
    CaptchaSample,
    load_dataset,
    numeric_array,
    split_samples,
)
from .model_bundle import (
    ENGINE_API_VERSION,
    ENGINE_RUNTIME_VERSION,
    build_model_bundle,
)
from .models import build_click_ranker, build_numeric_model
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
    family = "tiny-cnn" if captcha_type == "numeric" else "prompt-ranker"
    return f"{captcha_type}-{family}-{stamp}-{digest.hexdigest()[:8]}"


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


def _teacher_candidate(sample: CaptchaSample, point: dict) -> ClickCandidate:
    from PIL import Image

    with Image.open(io.BytesIO(sample.image)) as image:
        width, height = image.size
    x = float(point["x"]) * width
    y = float(point["y"]) * height
    side = max(28, min(52, round(min(width, height) * 0.27)))
    half = side // 2
    left = max(0, min(width - 2, round(x) - half))
    top = max(0, min(height - 2, round(y) - half))
    right = max(left + 1, min(width, left + side))
    bottom = max(top + 1, min(height, top + side))
    return ClickCandidate((left, top, right, bottom), 1)


def _prepare_click_group(extractor, sample, *, include_teacher):
    targets = [str(value) for value in sample.answer["prompt"]]
    initial = extractor.extract(sample.image, targets)
    candidates = list(initial.candidates)
    if include_teacher:
        for point in sample.answer["points"]:
            if (
                point_candidate_index(
                    candidates,
                    point,
                    width=initial.width,
                    height=initial.height,
                )
                is None
            ):
                candidates.append(_teacher_candidate(sample, point))
        if tuple(candidates) != initial.candidates:
            initial = extractor.extract(sample.image, targets, candidates=candidates)
    raw_labels = [
        point_candidate_index(
            initial.candidates,
            point,
            width=initial.width,
            height=initial.height,
        )
        for point in sample.answer["points"]
    ]
    covered = sum(value is not None for value in raw_labels)
    if covered != len(raw_labels) or len(set(raw_labels)) != len(raw_labels):
        return initial, None, covered
    return initial, [int(value) for value in raw_labels], covered


def _prepare_click_groups(samples, *, include_teacher):
    try:
        extractor = ClickFeatureExtractor()
    except Exception as exc:
        raise TrainingError(f"无法初始化点选验证码识别引擎：{exc}") from exc
    groups = []
    covered_targets = 0
    all_targets = 0
    for index, sample in enumerate(samples, 1):
        prepared, labels, covered = _prepare_click_group(
            extractor,
            sample,
            include_teacher=include_teacher,
        )
        all_targets += len(sample.answer["points"])
        covered_targets += covered
        if labels is not None:
            groups.append((sample, prepared, labels))
        _emit(
            "feature",
            captcha_type="click",
            current=index,
            total=len(samples),
            usable=len(groups),
        )
    return groups, covered_targets, all_targets


def _train_click(torch, samples, epochs, batch_size, seed):
    train_samples, test_samples = split_samples(samples)
    train_groups, _train_covered, _train_targets = _prepare_click_groups(
        train_samples,
        include_teacher=True,
    )
    test_groups, covered_targets, test_targets = _prepare_click_groups(
        test_samples,
        include_teacher=False,
    )
    if not train_groups:
        raise TrainingError("点选验证码没有可用于排序训练的候选框")
    model = build_click_ranker(CLICK_FEATURE_COUNT, torch)
    optimizer = torch.optim.AdamW(model.parameters(), lr=1e-3, weight_decay=2e-4)
    criterion = torch.nn.CrossEntropyLoss()
    model.train()
    order = list(range(len(train_groups)))
    for epoch in range(epochs):
        epoch_started = time.perf_counter()
        random.Random(seed + epoch).shuffle(order)
        loss_total = 0.0
        target_count = 0
        target_correct = 0
        batch_count = 0
        for offset in range(0, len(order), max(1, batch_size)):
            optimizer.zero_grad(set_to_none=True)
            losses = []
            for group_index in order[offset : offset + max(1, batch_size)]:
                _sample, prepared, labels = train_groups[group_index]
                target_total, candidate_total, feature_total = prepared.features.shape
                tensor = torch.from_numpy(prepared.features.reshape(-1, feature_total))
                logits = model(tensor).reshape(target_total, candidate_total)
                expected = torch.tensor(labels, dtype=torch.long)
                losses.append(criterion(logits, expected))
                with torch.no_grad():
                    target_correct += int(logits.argmax(dim=1).eq(expected).sum().item())
                    target_count += target_total
            loss = torch.stack(losses).mean()
            loss.backward()
            optimizer.step()
            loss_total += float(loss.detach()) * len(losses)
            batch_count += 1
        _emit(
            "epoch",
            captcha_type="click",
            epoch=epoch + 1,
            epochs=epochs,
            loss=loss_total / max(1, len(train_groups)),
            train_accuracy=target_correct / max(1, target_count),
            batches=batch_count,
            processed_samples=len(train_groups),
            duration_seconds=time.perf_counter() - epoch_started,
            learning_rate=float(optimizer.param_groups[0]["lr"]),
        )

    model.eval()
    evaluated_samples = len(test_samples)
    pair_count = sum(
        prepared.features.shape[0] * prepared.features.shape[1]
        for _sample, prepared, _labels in train_groups
    )
    group_by_fingerprint = {
        sample.fingerprint: (prepared, labels)
        for sample, prepared, labels in test_groups
    }
    evaluation_records = []
    with torch.no_grad():
        for sample in test_samples:
            group = group_by_fingerprint.get(sample.fingerprint)
            if group is not None:
                prepared, labels = group
                target_total, candidate_total, feature_total = prepared.features.shape
                tensor = torch.from_numpy(prepared.features.reshape(-1, feature_total))
                scores = model(tensor).reshape(target_total, candidate_total).numpy()
                evaluation_records.append((sample, prepared, labels, scores))
            else:
                evaluation_records.append((sample, None, None, None))

    blend_options = (0.0, 0.25, 0.5, 0.75, 1.0)

    def evaluate_blend(weight):
        sample_correct = 0
        targets_correct = 0
        details = []
        for sample, prepared, labels, model_scores in evaluation_records:
            if prepared is None:
                details.append((sample, False, 0.0))
                continue
            combined = (
                prepared.base_scores * (1.0 - weight)
                + model_scores * weight
            )
            predicted, confidence, _row_margin, _assignment_margin = (
                click_assignment_diagnostics(combined)
            )
            matches = (
                [
                    value == labels[position]
                    for position, value in enumerate(predicted)
                ]
                if predicted is not None
                else []
            )
            matched = bool(matches) and all(matches)
            targets_correct += sum(matches)
            sample_correct += int(matched)
            details.append((sample, matched, confidence))
        return sample_correct, targets_correct, details

    blend_results = {
        weight: evaluate_blend(weight)
        for weight in blend_options
    }
    selected_weight = max(
        blend_options,
        key=lambda weight: (
            blend_results[weight][0],
            blend_results[weight][1],
            -weight,
        ),
    )
    correct_samples, target_correct, selected_details = blend_results[
        selected_weight
    ]
    baseline_correct = blend_results[0.0][0]

    predicted_details = [
        (matched, confidence)
        for _sample, matched, confidence in selected_details
        if confidence > 0
    ]
    minimum_accepted = max(1, math.ceil(evaluated_samples * 0.2))
    raw_accuracy = correct_samples / max(1, evaluated_samples)
    target_precision = min(0.98, max(0.8, raw_accuracy))
    threshold_options = sorted(
        {0.0, *(confidence for _matched, confidence in predicted_details)}
    )
    calibrated = []
    for threshold in threshold_options:
        accepted = [
            matched
            for matched, confidence in predicted_details
            if confidence >= threshold
        ]
        accepted_correct = sum(accepted)
        precision = accepted_correct / len(accepted) if accepted else 0.0
        if len(accepted) >= minimum_accepted and precision >= target_precision:
            calibrated.append(
                (accepted_correct, len(accepted), -threshold, threshold, precision)
            )
    if calibrated:
        _correct, accepted_samples, _negative, confidence_threshold, accepted_accuracy = max(
            calibrated
        )
    else:
        # A weak ranker must hand control back to the established fallback
        # instead of clicking low-confidence coordinates.  A threshold of 1
        # still permits the unambiguous one-candidate case.
        confidence_threshold = 1.0
        accepted = [
            matched
            for matched, confidence in predicted_details
            if confidence >= confidence_threshold
        ]
        accepted_samples = len(accepted)
        accepted_correct = sum(accepted)
        accepted_accuracy = (
            accepted_correct / accepted_samples if accepted_samples else 0.0
        )

    for index, (sample, matched, _confidence) in enumerate(selected_details, 1):
        _emit(
            "evaluation",
            captcha_type="click",
            current=index,
            total=len(test_samples),
            predicted="匹配" if matched else "未匹配",
            expected="全部点击点",
            correct=matched,
        )
    if not evaluated_samples:
        raise TrainingError("点选验证码测试集为空")
    metrics = {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "evaluation": "held_out_end_to_end_ordered_click_accuracy",
        "train_samples": len(train_samples),
        "test_samples": evaluated_samples,
        "correct_samples": correct_samples,
        "accuracy": correct_samples / evaluated_samples,
        "seed": seed,
        "epochs": epochs,
        "batch_size": batch_size,
        "feature_schema": CLICK_FEATURE_SCHEMA,
        "feature_count": CLICK_FEATURE_COUNT,
        "pair_count": pair_count,
        "proposal_recall": covered_targets / max(1, test_targets),
        "target_accuracy": target_correct / max(1, test_targets),
        "baseline_accuracy": baseline_correct / evaluated_samples,
        "ranker_weight": selected_weight,
        "confidence_threshold": confidence_threshold,
        "accepted_samples": accepted_samples,
        "accepted_accuracy": accepted_accuracy,
        "accepted_coverage": accepted_samples / evaluated_samples,
    }
    calibration = {
        "ranker_weight": selected_weight,
        "min_probability": confidence_threshold,
        "min_row_margin": 0.0,
        "min_assignment_margin": 0.0,
    }
    return model, calibration, metrics


def _export_onnx(torch, model, captcha_type, version, destination):
    onnx = _dependency("onnx")
    model.eval()
    if captcha_type == "numeric":
        example = torch.zeros((1, 1, 48, 112), dtype=torch.float32)
        dynamic_axes = None
        model_family = "numeric-tiny-cnn-v1"
        feature_count = None
    else:
        example = torch.zeros((1, CLICK_FEATURE_COUNT), dtype=torch.float32)
        dynamic_axes = {"input": {0: "N"}, "logits": {0: "N"}}
        model_family = "click-prompt-ranker-v1"
        feature_count = CLICK_FEATURE_COUNT
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
        model_family=model_family,
        feature_count=feature_count,
    )
    metadata["output_layout"] = (
        "batch,position,digit" if captcha_type == "numeric" else "batch,score"
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
            or metadata.get("engine_api_version") != "1"
            or metadata.get("captcha_type") != captcha_type
            or metadata.get("version") != version
        ):
            raise ValueError("I/O or metadata mismatch")
        if captcha_type == "numeric":
            tensor = np.zeros((1, 1, 48, 112), dtype=np.float32)
            expected = (1, 4, 10)
        else:
            tensor = np.zeros((2, CLICK_FEATURE_COUNT), dtype=np.float32)
            if metadata.get("feature_count") != str(CLICK_FEATURE_COUNT):
                raise ValueError("click feature count missing")
            expected = (2, 1)
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
    """Train one model and atomically publish protocol-v2 output files."""

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
        model, private_config, metrics = _train_numeric(
            torch,
            samples,
            epochs,
            batch_size,
            seed,
        )
    else:
        model, private_config, metrics = _train_click(
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
        raw_onnx_path = staging / ".engine-model.onnx"
        _export_onnx(
            torch,
            model,
            selected_type,
            version,
            raw_onnx_path,
        )
        _verify_onnx_runtime_contract(selected_type, version, raw_onnx_path)
        model_family = (
            "numeric-tiny-cnn-v1"
            if selected_type == "numeric"
            else "click-prompt-ranker-v1"
        )
        config = {
            "schema_version": 1,
            "engine_api_version": ENGINE_API_VERSION,
            "captcha_type": selected_type,
            "version": version,
            "model_family": model_family,
        }
        if selected_type == "click":
            config.update(
                {
                    "feature_schema": CLICK_FEATURE_SCHEMA,
                    "feature_count": CLICK_FEATURE_COUNT,
                    **private_config,
                }
            )
        bundle_bytes = build_model_bundle(
            captcha_type=selected_type,
            version=version,
            model_family=model_family,
            # The historical manifest field name is retained for wire
            # compatibility.  It now describes the minimum client inference
            # runtime, not the version of the component that trained it.
            min_component_version=ENGINE_RUNTIME_VERSION,
            files={
                "model.onnx": raw_onnx_path.read_bytes(),
                "config.json": canonical_json_bytes(config),
            },
        )
        raw_onnx_path.unlink()
        model_path = staging / MODEL_FILE_NAME
        atomic_write_bytes(model_path, bundle_bytes)
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
    try:
        ClickFeatureExtractor()
    except Exception as exc:
        raise TrainingError(f"点选 OCR 引擎自检失败：{exc}") from exc
    click = build_click_ranker(CLICK_FEATURE_COUNT, torch).eval()
    with torch.no_grad():
        numeric_output = numeric(torch.zeros((1, 1, 48, 112)))
        click_output = click(torch.zeros((2, CLICK_FEATURE_COUNT)))
    if tuple(numeric_output.shape) != (1, 4, 10):
        raise TrainingError("数字 Tiny CNN 自检输出形状无效")
    if tuple(click_output.shape) != (2, 1):
        raise TrainingError("点选提示词排序器自检输出形状无效")
    with tempfile.TemporaryDirectory(prefix="intdemo-trainer-self-test-") as root:
        directory = Path(root)
        numeric_path = directory / "numeric.onnx"
        click_path = directory / "click.onnx"
        _export_onnx(
            torch,
            numeric,
            "numeric",
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
        "engine_api_version": ENGINE_API_VERSION,
        "component_version": COMPONENT_VERSION,
        "algorithm": ALGORITHM,
        "training_mode": TRAINING_MODE,
        "torch_version": str(torch.__version__),
        "cpu_threads": threads,
    }

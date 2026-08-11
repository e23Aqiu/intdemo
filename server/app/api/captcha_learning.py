from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import re
import unicodedata
import uuid
import zipfile
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Query, Request, Response
from sqlalchemy import case, func, select, update
from sqlalchemy.exc import IntegrityError

from ..database import utcnow
from ..dependencies import AdminContext, BusinessContext, Db
from ..errors import ApiError
from ..models import (
    CaptchaAttempt,
    CaptchaLearningPolicy,
    CaptchaModel,
    CaptchaSample,
)
from ..schemas import (
    CaptchaAttemptCreate,
    CaptchaAttemptResult,
    CaptchaDatasetImportResult,
    CaptchaLearningOverview,
    CaptchaLearningPolicyUpdate,
    CaptchaLearningPolicyView,
    CaptchaModelCreate,
    CaptchaModelView,
    CaptchaSampleDeleteRequest,
    CaptchaSampleDeleteResult,
    CaptchaSamplePage,
)
from ..services import audit

router = APIRouter(tags=["captcha-learning"])

MAX_IMAGE_BYTES = 1 * 1024 * 1024
MAX_ARCHIVE_BYTES = 100 * 1024 * 1024
MAX_ARCHIVE_FILES = 50_003
MAX_MANIFEST_BYTES = 10 * 1024 * 1024
MAX_MODEL_BYTES = 20 * 1024 * 1024
DATASET_SCHEMA_VERSION = 1
CAPTCHA_TYPES = {"numeric", "click"}
UPLOAD_MODE_OFF = "off"
UPLOAD_MODE_METRICS_ONLY = "metrics_only"
UPLOAD_MODE_SAMPLES_AND_METRICS = "samples_and_metrics"
UPLOAD_MODES = {
    UPLOAD_MODE_OFF,
    UPLOAD_MODE_METRICS_ONLY,
    UPLOAD_MODE_SAMPLES_AND_METRICS,
}
CAPTCHA_SOURCE_BY_TYPE = {
    "numeric": "transport_numeric",
    "click": "business_click",
}
DATASET_DIRECTORY_BY_TYPE = {
    "numeric": "数字验证码",
    "click": "文字点选验证码",
}
MANUAL_MODEL_PREFIXES = ("human-", "human_")
ARCHIVE_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")


def _is_manual_model_version(value: Any) -> bool:
    """Return whether a model label represents a human-assisted operation.

    ``assisted`` is the authoritative flag for new clients, but older clients
    have emitted ``human-manual`` as a model version without setting it.  Keep
    those records out of automatic recognition metrics as well.
    """

    normalized = str(value or "").strip().lower()
    return normalized == "human" or normalized.startswith(MANUAL_MODEL_PREFIXES)


def _validate_captcha_filters(
    captcha_type: str | None,
    model_version: str | None,
) -> tuple[str | None, str | None]:
    if captcha_type is not None:
        captcha_type = str(captcha_type).strip().lower()
        if captcha_type not in CAPTCHA_TYPES:
            raise ApiError(
                "invalid_captcha_type",
                "验证码类型无效",
                status_code=422,
            )
    if model_version is not None:
        model_version = str(model_version).strip()
        if not model_version:
            raise ApiError(
                "invalid_model_version",
                "模型版本不能为空",
                status_code=422,
            )
        if len(model_version) > 80:
            raise ApiError(
                "invalid_model_version",
                "模型版本长度超出限制",
                status_code=422,
            )
    return captcha_type, model_version


def _normalize_archive_member(value: Any) -> str:
    text = unicodedata.normalize("NFC", str(value or "")).replace("\\", "/")
    if (
        not text
        or "\x00" in text
        or text.startswith("/")
        or ARCHIVE_DRIVE_PREFIX.match(text)
    ):
        raise ValueError("unsafe archive path")
    directory = text.endswith("/")
    parts = []
    for part in text.split("/"):
        if part in {"", "."}:
            continue
        if part == "..":
            raise ValueError("unsafe archive path")
        parts.append(part)
    if not parts:
        raise ValueError("empty archive path")
    normalized = "/".join(parts)
    return normalized + ("/" if directory else "")


def _policy(db: Db) -> CaptchaLearningPolicy:
    row = db.get(CaptchaLearningPolicy, 1)
    if row is None:
        row = CaptchaLearningPolicy(
            id=1,
            upload_mode=UPLOAD_MODE_OFF,
            upload_enabled=False,
            revision=1,
        )
        db.add(row)
        db.flush()
    return row


def _policy_mode(row: CaptchaLearningPolicy) -> str:
    mode = str(row.upload_mode or "")
    return mode if mode in UPLOAD_MODES else UPLOAD_MODE_OFF


def _model_view(model: CaptchaModel) -> dict[str, Any]:
    return {
        "id": model.id,
        "captcha_type": model.captcha_type,
        "version": model.version,
        "algorithm": model.algorithm,
        "status": model.status,
        "artifact_sha256": model.artifact_sha256,
        "artifact_size": model.artifact_size,
        "sample_count": model.sample_count,
        "test_count": model.test_count,
        "correct_count": model.correct_count,
        "accuracy": model.accuracy,
        "metrics": model.metrics or {},
        "created_at": model.created_at,
        "activated_at": model.activated_at,
    }


def _sample_view(sample: CaptchaSample) -> dict[str, Any]:
    captcha_type = str(sample.captcha_type or "")
    image_data = bytes(sample.image_data or b"")
    try:
        image_mime = _image_mime(image_data) if image_data else None
    except ApiError:
        image_mime = None
    source = str(
        sample.source
        or CAPTCHA_SOURCE_BY_TYPE.get(captcha_type, "")
    )
    answer = sample.answer if isinstance(sample.answer, dict) else {}
    captured_at = sample.captured_at or sample.created_at or utcnow()
    created_at = sample.created_at or captured_at
    return {
        "id": sample.id,
        "captcha_type": captcha_type,
        "source": source,
        "answer": answer,
        "model_version": str(sample.model_version or "imported"),
        "origin": str(sample.origin or "import"),
        "image_size": int(sample.image_size or len(image_data)),
        "image_mime": image_mime,
        "image_available": bool(image_data),
        "captured_at": captured_at,
        "created_at": created_at,
    }


def _active_models(db: Db) -> dict[str, dict[str, Any]]:
    rows = db.scalars(
        select(CaptchaModel).where(CaptchaModel.status == "current")
    ).all()
    return {
        row.captcha_type: _model_view(row)
        for row in rows
        if not _is_manual_model_version(row.version)
    }


def _policy_view(db: Db, row: CaptchaLearningPolicy) -> dict[str, Any]:
    mode = _policy_mode(row)
    return {
        "upload_mode": mode,
        "upload_enabled": mode == UPLOAD_MODE_SAMPLES_AND_METRICS,
        "revision": row.revision,
        "updated_at": row.updated_at,
        "active_models": _active_models(db),
    }


def _decode_base64(value: str, *, label: str, maximum: int) -> bytes:
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ApiError(
            "invalid_base64",
            f"{label}不是有效的 Base64 数据",
            status_code=422,
        ) from exc
    if not decoded or len(decoded) > maximum:
        raise ApiError(
            "payload_too_large",
            f"{label}大小超出限制",
            status_code=413,
        )
    return decoded


def _image_mime(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    raise ApiError(
        "invalid_captcha_image",
        "验证码图片只允许 PNG 或 JPEG",
        status_code=422,
    )


def _normalize_answer(captcha_type: str, answer: Any) -> dict[str, Any]:
    if not isinstance(answer, dict):
        raise ApiError(
            "invalid_captcha_answer",
            "验证码答案格式无效",
            status_code=422,
        )
    if captcha_type == "numeric":
        if set(answer) != {"value"}:
            raise ApiError(
                "invalid_captcha_answer",
                "数字验证码答案只允许 value 字段",
                status_code=422,
            )
        value = str(answer.get("value") or "").strip()
        if not re.fullmatch(r"[0-9]{4}", value):
            raise ApiError(
                "invalid_captcha_answer",
                "数字验证码答案必须是 4 位数字",
                status_code=422,
            )
        return {"value": value}

    if set(answer) != {"prompt", "points"}:
        raise ApiError(
            "invalid_captcha_answer",
            "点选验证码答案只允许 prompt 和 points 字段",
            status_code=422,
        )
    prompt = answer.get("prompt")
    points = answer.get("points")
    if (
        not isinstance(prompt, list)
        or not isinstance(points, list)
        or not 1 <= len(prompt) <= 8
        or len(prompt) != len(points)
    ):
        raise ApiError(
            "invalid_captcha_answer",
            "点选验证码文字和坐标数量必须一致",
            status_code=422,
        )
    normalized_prompt = []
    normalized_points = []
    for character, point in zip(prompt, points):
        character = str(character or "").strip()
        if not character or len(character) > 4:
            raise ApiError(
                "invalid_captcha_answer",
                "点选验证码文字无效",
                status_code=422,
            )
        if not isinstance(point, dict) or set(point) != {"x", "y"}:
            raise ApiError(
                "invalid_captcha_answer",
                "点选验证码坐标格式无效",
                status_code=422,
            )
        try:
            x = float(point["x"])
            y = float(point["y"])
        except (TypeError, ValueError) as exc:
            raise ApiError(
                "invalid_captcha_answer",
                "点选验证码坐标无效",
                status_code=422,
            ) from exc
        if not 0 <= x <= 1 or not 0 <= y <= 1:
            raise ApiError(
                "invalid_captcha_answer",
                "点选验证码坐标必须使用 0 到 1 的相对值",
                status_code=422,
            )
        normalized_prompt.append(character)
        normalized_points.append({"x": round(x, 6), "y": round(y, 6)})
    return {"prompt": normalized_prompt, "points": normalized_points}


def _fingerprint(
    captcha_type: str,
    image: bytes,
    answer: dict[str, Any],
) -> str:
    digest = hashlib.sha256()
    digest.update(captcha_type.encode("ascii"))
    digest.update(b"\0")
    digest.update(image)
    digest.update(b"\0")
    digest.update(
        json.dumps(
            answer,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    return digest.hexdigest()


def _aware_datetime(value: str | datetime, label: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError) as exc:
            raise ApiError(
                "invalid_dataset",
                f"{label}不是有效时间",
                status_code=422,
            ) from exc
    if parsed.tzinfo is None:
        raise ApiError(
            "invalid_dataset",
            f"{label}必须包含时区",
            status_code=422,
        )
    return parsed.astimezone(UTC)


def _iso_utc(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


@router.get("/captcha/policy", response_model=CaptchaLearningPolicyView)
def captcha_policy(
    context: BusinessContext,
    db: Db,
) -> dict[str, Any]:
    del context
    row = _policy(db)
    db.commit()
    return _policy_view(db, row)


@router.post("/captcha/attempts", response_model=CaptchaAttemptResult)
def record_captcha_attempt(
    payload: CaptchaAttemptCreate,
    context: BusinessContext,
    db: Db,
) -> dict[str, Any]:
    del context
    policy = _policy(db)
    mode = _policy_mode(policy)
    if mode == UPLOAD_MODE_OFF:
        db.commit()
        return {
            "stored": False,
            "sample_stored": False,
            "sample_id": None,
            "policy_revision": policy.revision,
        }

    if mode == UPLOAD_MODE_METRICS_ONLY and (
        payload.assisted or _is_manual_model_version(payload.model_version)
    ):
        db.commit()
        return {
            "stored": False,
            "sample_stored": False,
            "sample_id": None,
            "policy_revision": policy.revision,
        }

    attempt = CaptchaAttempt(
        captcha_type=payload.captcha_type,
        source=CAPTCHA_SOURCE_BY_TYPE[payload.captcha_type],
        model_version=payload.model_version.strip(),
        success=payload.success,
        assisted=payload.assisted,
        occurred_at=payload.occurred_at.astimezone(UTC),
    )
    db.add(attempt)
    db.flush()

    sample_id = None
    sample_stored = False
    if (
        mode == UPLOAD_MODE_SAMPLES_AND_METRICS
        and payload.success
        and payload.image_base64 is not None
    ):
        image = _decode_base64(
            str(payload.image_base64),
            label="验证码图片",
            maximum=MAX_IMAGE_BYTES,
        )
        detected_mime = _image_mime(image)
        if payload.image_mime != detected_mime:
            raise ApiError(
                "captcha_image_type_mismatch",
                "验证码图片类型与内容不一致",
                status_code=422,
            )
        answer = _normalize_answer(payload.captcha_type, payload.answer)
        fingerprint = _fingerprint(payload.captcha_type, image, answer)
        existing = db.scalar(
            select(CaptchaSample).where(
                CaptchaSample.sample_fingerprint == fingerprint
            )
        )
        if existing is not None:
            sample_id = existing.id
        else:
            sample = CaptchaSample(
                attempt_id=attempt.id,
                captcha_type=payload.captcha_type,
                source=CAPTCHA_SOURCE_BY_TYPE[payload.captcha_type],
                sample_fingerprint=fingerprint,
                image_mime=detected_mime,
                image_size=len(image),
                image_data=image,
                answer=answer,
                model_version=payload.model_version.strip(),
                origin="client",
                captured_at=payload.occurred_at.astimezone(UTC),
            )
            try:
                with db.begin_nested():
                    db.add(sample)
                    db.flush()
                sample_id = sample.id
                sample_stored = True
            except IntegrityError:
                existing = db.scalar(
                    select(CaptchaSample).where(
                        CaptchaSample.sample_fingerprint == fingerprint
                    )
                )
                if existing is None:
                    raise
                sample_id = existing.id
    db.commit()
    return {
        "stored": True,
        "sample_stored": sample_stored,
        "sample_id": sample_id,
        "policy_revision": policy.revision,
    }


@router.get("/captcha/models/{captcha_type}/current")
def download_current_captcha_model(
    captcha_type: str,
    context: BusinessContext,
    db: Db,
) -> Response:
    del context
    if captcha_type not in CAPTCHA_TYPES:
        raise ApiError("invalid_captcha_type", "验证码类型无效", status_code=404)
    model = db.scalar(
        select(CaptchaModel).where(
            CaptchaModel.captcha_type == captcha_type,
            CaptchaModel.status == "current",
        )
    )
    if model is None:
        raise ApiError("captcha_model_not_found", "当前没有自定义模型", status_code=404)
    return Response(
        content=model.artifact,
        media_type="application/octet-stream",
        headers={
            "X-Captcha-Model-Version": model.version,
            "X-Content-SHA256": model.artifact_sha256,
            "Cache-Control": "no-store",
        },
    )


def _dataset_stats(db: Db) -> dict[str, int]:
    row = db.execute(
        select(
            func.count(CaptchaSample.id),
            func.coalesce(func.sum(CaptchaSample.image_size), 0),
            func.coalesce(
                func.sum(case((CaptchaSample.captcha_type == "numeric", 1), else_=0)),
                0,
            ),
            func.coalesce(
                func.sum(
                    case(
                        (
                            CaptchaSample.captcha_type == "numeric",
                            CaptchaSample.image_size,
                        ),
                        else_=0,
                    )
                ),
                0,
            ),
            func.coalesce(
                func.sum(case((CaptchaSample.captcha_type == "click", 1), else_=0)),
                0,
            ),
            func.coalesce(
                func.sum(
                    case(
                        (
                            CaptchaSample.captcha_type == "click",
                            CaptchaSample.image_size,
                        ),
                        else_=0,
                    )
                ),
                0,
            ),
        )
    ).one()
    return {
        "total_count": int(row[0] or 0),
        "total_bytes": int(row[1] or 0),
        "numeric_count": int(row[2] or 0),
        "numeric_bytes": int(row[3] or 0),
        "click_count": int(row[4] or 0),
        "click_bytes": int(row[5] or 0),
    }


def _attempt_metrics(
    db: Db,
    *,
    captcha_type: str | None = None,
    model_version: str | None = None,
) -> list[dict[str, Any]]:
    # Keep the aggregation in SQL so filtering a selected model remains cheap
    # on PostgreSQL installations with a large attempt history.  The explicit
    # human-version guard covers old clients that forgot ``assisted=true``.
    normalized_version = func.lower(func.trim(CaptchaAttempt.model_version))
    filters = [
        CaptchaAttempt.assisted.is_(False),
        normalized_version != "human",
        ~normalized_version.like("human-%"),
        ~normalized_version.like("human\\_%", escape="\\"),
    ]
    if captcha_type is not None:
        filters.append(CaptchaAttempt.captcha_type == captcha_type)
    if model_version is not None:
        filters.append(func.trim(CaptchaAttempt.model_version) == model_version)
    rows = db.execute(
        select(
            CaptchaAttempt.captcha_type,
            CaptchaAttempt.model_version,
            func.count(CaptchaAttempt.id),
            func.coalesce(
                func.sum(case((CaptchaAttempt.success.is_(True), 1), else_=0)),
                0,
            ),
        )
        .where(*filters)
        .group_by(CaptchaAttempt.captcha_type, CaptchaAttempt.model_version)
        .order_by(CaptchaAttempt.captcha_type, CaptchaAttempt.model_version)
    ).all()
    result = []
    for captcha_type, model_version, attempt_count, success_count in rows:
        total = int(attempt_count or 0)
        successes = int(success_count or 0)
        result.append(
            {
                "captcha_type": captcha_type,
                "model_version": model_version,
                "attempt_count": total,
                "success_count": successes,
                "success_rate": successes / total if total else 0.0,
            }
        )
    return result


@router.get("/admin/ml/overview", response_model=CaptchaLearningOverview)
def learning_overview(
    context: AdminContext,
    db: Db,
    captcha_type: str | None = Query(default=None),
    model_version: str | None = Query(default=None, max_length=80),
) -> dict[str, Any]:
    del context
    captcha_type, model_version = _validate_captcha_filters(
        captcha_type,
        model_version,
    )
    policy = _policy(db)
    models = db.scalars(
        select(CaptchaModel).order_by(CaptchaModel.created_at.desc())
    ).all()
    models = [
        model
        for model in models
        if not _is_manual_model_version(model.version)
    ]
    db.commit()
    return {
        "policy": _policy_view(db, policy),
        "dataset": _dataset_stats(db),
        "attempts": _attempt_metrics(
            db,
            captcha_type=captcha_type,
            model_version=model_version,
        ),
        "models": [_model_view(model) for model in models],
    }


@router.get("/admin/ml/samples", response_model=CaptchaSamplePage)
def list_captcha_samples(
    context: AdminContext,
    db: Db,
    captcha_type: str | None = Query(default=None),
    model_version: str | None = Query(default=None, max_length=80),
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    del context
    captcha_type, model_version = _validate_captcha_filters(
        captcha_type,
        model_version,
    )
    filters = []
    if captcha_type:
        filters.append(CaptchaSample.captcha_type == captcha_type)
    if model_version:
        filters.append(func.trim(CaptchaSample.model_version) == model_version)
    filters.append(CaptchaSample.captcha_type.in_(CAPTCHA_TYPES))
    count_statement = select(func.count(CaptchaSample.id))
    statement = select(CaptchaSample)
    if filters:
        count_statement = count_statement.where(*filters)
        statement = statement.where(*filters)
    total = int(db.scalar(count_statement) or 0)
    samples = db.scalars(
        statement.order_by(
            CaptchaSample.captured_at.desc(),
            CaptchaSample.created_at.desc(),
            CaptchaSample.id.desc(),
        )
        .offset(offset)
        .limit(limit)
    ).all()
    return {
        "items": [_sample_view(sample) for sample in samples],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/admin/ml/samples/{sample_id}/image")
def download_captcha_sample_image(
    sample_id: uuid.UUID,
    context: AdminContext,
    db: Db,
) -> Response:
    """Read one stored sample image without putting binary data in listings."""

    del context
    sample = db.get(CaptchaSample, sample_id)
    if sample is None:
        raise ApiError(
            "captcha_sample_not_found",
            "验证码样本不存在",
            status_code=404,
        )
    image = bytes(sample.image_data or b"")
    if not image:
        raise ApiError(
            "captcha_sample_image_missing",
            "验证码样本图片不可用",
            status_code=404,
        )
    image_mime = _image_mime(image)
    return Response(
        content=image,
        media_type=image_mime,
        headers={
            "Cache-Control": "no-store",
            "X-Captcha-Sample-ID": str(sample.id),
        },
    )


@router.delete(
    "/admin/ml/samples",
    response_model=CaptchaSampleDeleteResult,
)
def delete_captcha_samples(
    payload: CaptchaSampleDeleteRequest,
    request: Request,
    context: AdminContext,
    db: Db,
) -> dict[str, int]:
    samples = db.scalars(
        select(CaptchaSample).where(CaptchaSample.id.in_(payload.sample_ids))
    ).all()
    found_ids = {sample.id for sample in samples}
    for sample in samples:
        db.delete(sample)
    audit(
        db,
        request,
        actor_id=context.account.id,
        action="captcha.sample.delete",
        target_type="captcha_sample",
        target_id="multiple",
        details={
            "sample_ids": [str(sample_id) for sample_id in payload.sample_ids],
            "deleted_count": len(samples),
            "missing_count": len(payload.sample_ids) - len(found_ids),
        },
    )
    db.commit()
    return {
        "deleted_count": len(samples),
        "missing_count": len(payload.sample_ids) - len(found_ids),
    }


@router.patch("/admin/ml/policy", response_model=CaptchaLearningPolicyView)
def update_learning_policy(
    payload: CaptchaLearningPolicyUpdate,
    request: Request,
    context: AdminContext,
    db: Db,
) -> dict[str, Any]:
    policy = _policy(db)
    upload_mode = payload.resolved_upload_mode()
    if _policy_mode(policy) != upload_mode:
        policy.upload_mode = upload_mode
        policy.upload_enabled = (
            upload_mode == UPLOAD_MODE_SAMPLES_AND_METRICS
        )
        policy.revision += 1
        policy.updated_by_id = context.account.id
        policy.updated_at = utcnow()
        audit(
            db,
            request,
            actor_id=context.account.id,
            action="captcha.policy.update",
            target_type="captcha_learning_policy",
            target_id="1",
            details={"upload_mode": upload_mode},
        )
    db.commit()
    return _policy_view(db, policy)


@router.get("/admin/ml/dataset/export")
def export_captcha_dataset(
    request: Request,
    context: AdminContext,
    db: Db,
    captcha_type: str | None = Query(default=None),
) -> Response:
    captcha_type, _ = _validate_captcha_filters(captcha_type, None)
    statement = select(CaptchaSample).order_by(CaptchaSample.created_at)
    if captcha_type:
        statement = statement.where(CaptchaSample.captcha_type == captcha_type)
    samples = db.scalars(statement).all()
    manifest_samples = []
    category_counts = {kind: 0 for kind in sorted(CAPTCHA_TYPES)}
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for directory in DATASET_DIRECTORY_BY_TYPE.values():
            archive.writestr(f"{directory}/", b"")
        for sample in samples:
            if sample.captcha_type not in CAPTCHA_TYPES:
                continue
            image = bytes(sample.image_data or b"")
            if not image:
                # A partially migrated row must not make the complete export
                # unreadable.  It remains visible in the sample list with
                # ``image_available=false`` and can be removed or re-uploaded.
                continue
            try:
                detected_mime = _image_mime(image)
            except ApiError:
                continue
            extension = ".png" if detected_mime == "image/png" else ".jpg"
            directory = DATASET_DIRECTORY_BY_TYPE[sample.captcha_type]
            image_name = f"{directory}/{sample.id}{extension}"
            archive.writestr(image_name, image)
            category_counts[sample.captcha_type] += 1
            captured_at = sample.captured_at or sample.created_at or utcnow()
            answer = sample.answer if isinstance(sample.answer, dict) else {}
            manifest_samples.append(
                {
                    "id": str(sample.id),
                    "captcha_type": sample.captcha_type,
                    "source": sample.source,
                    "image": image_name,
                    "image_mime": detected_mime,
                    "answer": answer,
                    "model_version": str(sample.model_version or "imported"),
                    "captured_at": _iso_utc(captured_at),
                    "fingerprint": sample.sample_fingerprint
                    or _fingerprint(sample.captcha_type, image, answer),
                }
            )
        manifest = {
            "schema_version": DATASET_SCHEMA_VERSION,
            "captcha_type": captcha_type or "mixed",
            "categories": {
                "numeric": {
                    "label": "数字验证码",
                    "image_directory": "数字验证码/",
                    "sample_count": category_counts["numeric"],
                },
                "click": {
                    "label": "文字点选验证码",
                    "image_directory": "文字点选验证码/",
                    "sample_count": category_counts["click"],
                },
            },
            "exported_at": utcnow().isoformat(),
            "sample_count": len(manifest_samples),
            "samples": manifest_samples,
        }
        archive.writestr(
            "manifest.json",
            json.dumps(
                manifest,
                ensure_ascii=False,
                indent=2,
            ).encode("utf-8"),
        )
    audit(
        db,
        request,
        actor_id=context.account.id,
        action="captcha.dataset.export",
        target_type="captcha_dataset",
        target_id=captcha_type or "all",
        details={
            "captcha_type": captcha_type,
            "sample_count": len(manifest_samples),
        },
    )
    db.commit()
    suffix = captcha_type or "all"
    return Response(
        content=output.getvalue(),
        media_type="application/zip",
        headers={
            "Content-Disposition": (
                f'attachment; filename="intdemo-captcha-dataset-{suffix}.zip"'
            ),
            "Cache-Control": "no-store",
        },
    )


def _safe_archive(
    archive_bytes: bytes,
) -> tuple[zipfile.ZipFile, dict[str, Any], dict[str, zipfile.ZipInfo]]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(archive_bytes), "r")
    except (OSError, zipfile.BadZipFile) as exc:
        raise ApiError(
            "invalid_dataset_archive",
            "导入文件不是有效的数据集压缩包",
            status_code=422,
        ) from exc
    infos = archive.infolist()
    if len(infos) > MAX_ARCHIVE_FILES:
        archive.close()
        raise ApiError(
            "dataset_archive_too_large",
            "数据集压缩包文件数量超出限制",
            status_code=413,
        )
    total_size = 0
    members: dict[str, zipfile.ZipInfo] = {}
    for info in infos:
        if (
            info.flag_bits & 0x1
            or info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
        ):
            archive.close()
            raise ApiError(
                "invalid_dataset_archive",
                "数据集压缩包包含不安全路径",
                status_code=422,
            )
        try:
            member_name = _normalize_archive_member(info.filename)
        except ValueError as exc:
            archive.close()
            raise ApiError(
                "invalid_dataset_archive",
                "数据集压缩包包含不安全路径",
                status_code=422,
            ) from exc
        if not member_name.endswith("/"):
            if member_name in members:
                archive.close()
                raise ApiError(
                    "invalid_dataset_archive",
                    "数据集压缩包包含重复路径",
                    status_code=422,
                )
            members[member_name] = info
        total_size += int(info.file_size)
        if total_size > MAX_ARCHIVE_BYTES:
            archive.close()
            raise ApiError(
                "dataset_archive_too_large",
                "数据集解压后大小超出限制",
                status_code=413,
            )
    try:
        manifest_info = members["manifest.json"]
        if manifest_info.file_size > MAX_MANIFEST_BYTES:
            raise ValueError("manifest too large")
        manifest = json.loads(
            archive.read(manifest_info).decode("utf-8-sig")
        )
    except (
        KeyError,
        UnicodeError,
        ValueError,
        json.JSONDecodeError,
        OSError,
        RuntimeError,
        zipfile.BadZipFile,
    ) as exc:
        archive.close()
        raise ApiError(
            "invalid_dataset_manifest",
            "数据集缺少有效的 manifest.json",
            status_code=422,
        ) from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != DATASET_SCHEMA_VERSION
        or (
            manifest.get("captcha_type") is not None
            and (
                not isinstance(manifest.get("captcha_type"), str)
                or manifest.get("captcha_type")
                not in CAPTCHA_TYPES | {"mixed"}
            )
        )
        or not isinstance(manifest.get("samples"), list)
        or len(manifest.get("samples")) > MAX_ARCHIVE_FILES - 1
    ):
        archive.close()
        raise ApiError(
            "invalid_dataset_manifest",
            "数据集清单版本或样本列表无效",
            status_code=422,
        )
    return archive, manifest, members


@router.post(
    "/admin/ml/dataset/import",
    response_model=CaptchaDatasetImportResult,
)
async def import_captcha_dataset(
    request: Request,
    context: AdminContext,
    db: Db,
) -> dict[str, int]:
    try:
        content_length = max(
            0,
            int(request.headers.get("content-length") or 0),
        )
    except ValueError as exc:
        raise ApiError(
            "invalid_content_length",
            "数据集请求长度无效",
            status_code=400,
        ) from exc
    if content_length > MAX_ARCHIVE_BYTES:
        raise ApiError(
            "dataset_archive_too_large",
            "数据集压缩包大小超出限制",
            status_code=413,
        )
    chunks = []
    received = 0
    async for chunk in request.stream():
        received += len(chunk)
        if received > MAX_ARCHIVE_BYTES:
            raise ApiError(
                "dataset_archive_too_large",
                "数据集压缩包大小超出限制",
                status_code=413,
            )
        chunks.append(chunk)
    archive_bytes = b"".join(chunks)
    if not archive_bytes:
        raise ApiError(
            "dataset_archive_too_large",
            "数据集压缩包不能为空",
            status_code=413,
        )
    archive, manifest, members = _safe_archive(archive_bytes)
    declared_captcha_type = manifest.get("captcha_type")
    imported = 0
    duplicates = 0
    skipped = 0
    try:
        for item in manifest["samples"]:
            try:
                if not isinstance(item, dict):
                    raise ValueError("sample is not an object")
                captcha_type = str(item.get("captcha_type") or "")
                source = str(item.get("source") or "")
                model_version = str(item.get("model_version") or "imported")[:80]
                image_name = _normalize_archive_member(item.get("image"))
                if (
                    declared_captcha_type in CAPTCHA_TYPES
                    and captcha_type != declared_captcha_type
                ):
                    raise ValueError("sample type does not match dataset type")
                if source != CAPTCHA_SOURCE_BY_TYPE.get(captcha_type):
                    raise ValueError("invalid type or source")
                image_info = members[image_name]
                if image_info.file_size <= 0 or image_info.file_size > MAX_IMAGE_BYTES:
                    raise ValueError("invalid image size")
                image = archive.read(image_info)
                image_mime = _image_mime(image)
                answer = _normalize_answer(captcha_type, item.get("answer"))
                captured_at = _aware_datetime(
                    item.get("captured_at") or utcnow(),
                    "captured_at",
                )
                fingerprint = _fingerprint(captcha_type, image, answer)
            except (KeyError, OSError, ValueError, ApiError):
                skipped += 1
                continue
            existing = db.scalar(
                select(CaptchaSample.id).where(
                    CaptchaSample.sample_fingerprint == fingerprint
                )
            )
            if existing is not None:
                duplicates += 1
                continue
            db.add(
                CaptchaSample(
                    attempt_id=None,
                    captcha_type=captcha_type,
                    source=source,
                    sample_fingerprint=fingerprint,
                    image_mime=image_mime,
                    image_size=len(image),
                    image_data=image,
                    answer=answer,
                    model_version=model_version or "imported",
                    origin="import",
                    captured_at=captured_at,
                )
            )
            imported += 1
        audit(
            db,
            request,
            actor_id=context.account.id,
            action="captcha.dataset.import",
            target_type="captcha_dataset",
            details={
                "imported_count": imported,
                "duplicate_count": duplicates,
                "skipped_count": skipped,
            },
        )
        db.commit()
    finally:
        archive.close()
    return {
        "imported_count": imported,
        "duplicate_count": duplicates,
        "skipped_count": skipped,
    }


@router.post("/admin/ml/models", response_model=CaptchaModelView)
def create_captcha_model(
    payload: CaptchaModelCreate,
    request: Request,
    context: AdminContext,
    db: Db,
) -> dict[str, Any]:
    artifact = _decode_base64(
        payload.artifact_base64,
        label="模型文件",
        maximum=MAX_MODEL_BYTES,
    )
    if not artifact.startswith(b"PK"):
        raise ApiError(
            "invalid_captcha_model",
            "候选模型必须使用受支持的 NPZ 格式",
            status_code=422,
        )
    metrics_encoded = json.dumps(
        payload.metrics,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(metrics_encoded) > 64 * 1024:
        raise ApiError(
            "invalid_captcha_model",
            "模型指标数据过大",
            status_code=422,
        )
    model = CaptchaModel(
        captcha_type=payload.captcha_type,
        version=payload.version,
        algorithm=payload.algorithm.strip(),
        status="candidate",
        artifact_sha256=hashlib.sha256(artifact).hexdigest(),
        artifact_size=len(artifact),
        artifact=artifact,
        sample_count=payload.sample_count,
        test_count=payload.test_count,
        correct_count=payload.correct_count,
        accuracy=payload.correct_count / payload.test_count,
        metrics=payload.metrics,
        created_by_id=context.account.id,
    )
    db.add(model)
    audit(
        db,
        request,
        actor_id=context.account.id,
        action="captcha.model.create",
        target_type="captcha_model",
        target_id=payload.version,
        details={
            "captcha_type": payload.captcha_type,
            "sample_count": payload.sample_count,
            "accuracy": model.accuracy,
        },
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ApiError(
            "captcha_model_version_exists",
            "同类型的模型版本已经存在",
            status_code=409,
        ) from exc
    db.refresh(model)
    return _model_view(model)


@router.post(
    "/admin/ml/models/{model_id}/activate",
    response_model=CaptchaModelView,
)
def activate_captcha_model(
    model_id: uuid.UUID,
    request: Request,
    context: AdminContext,
    db: Db,
) -> dict[str, Any]:
    model = db.get(CaptchaModel, model_id)
    if model is None:
        raise ApiError("captcha_model_not_found", "候选模型不存在", status_code=404)
    db.execute(
        update(CaptchaModel)
        .where(
            CaptchaModel.captcha_type == model.captcha_type,
            CaptchaModel.status == "current",
            CaptchaModel.id != model.id,
        )
        .values(status="archived")
    )
    model.status = "current"
    model.activated_at = utcnow()
    policy = _policy(db)
    policy.revision += 1
    policy.updated_by_id = context.account.id
    policy.updated_at = utcnow()
    audit(
        db,
        request,
        actor_id=context.account.id,
        action="captcha.model.activate",
        target_type="captcha_model",
        target_id=str(model.id),
        details={
            "captcha_type": model.captcha_type,
            "version": model.version,
        },
    )
    db.commit()
    db.refresh(model)
    return _model_view(model)


@router.post(
    "/admin/ml/models/{captcha_type}/use-builtin",
    response_model=CaptchaLearningPolicyView,
)
def use_builtin_captcha_model(
    captcha_type: str,
    request: Request,
    context: AdminContext,
    db: Db,
) -> dict[str, Any]:
    if captcha_type not in CAPTCHA_TYPES:
        raise ApiError(
            "invalid_captcha_type",
            "验证码类型无效",
            status_code=422,
        )
    current = db.scalar(
        select(CaptchaModel).where(
            CaptchaModel.captcha_type == captcha_type,
            CaptchaModel.status == "current",
        )
    )
    policy = _policy(db)
    if current is not None:
        current.status = "archived"
        policy.revision += 1
        policy.updated_by_id = context.account.id
        policy.updated_at = utcnow()
        audit(
            db,
            request,
            actor_id=context.account.id,
            action="captcha.model.use_builtin",
            target_type="captcha_model",
            target_id=captcha_type,
            details={
                "captcha_type": captcha_type,
                "previous_version": current.version,
            },
        )
    db.commit()
    return _policy_view(db, policy)


@router.delete("/admin/ml/models/{model_id}", status_code=204)
def delete_captcha_model(
    model_id: uuid.UUID,
    request: Request,
    context: AdminContext,
    db: Db,
) -> None:
    model = db.get(CaptchaModel, model_id)
    if model is None:
        raise ApiError("captcha_model_not_found", "候选模型不存在", status_code=404)
    if model.status == "current":
        raise ApiError(
            "captcha_model_is_current",
            "当前正在使用的模型不能删除",
            status_code=409,
        )
    audit(
        db,
        request,
        actor_id=context.account.id,
        action="captcha.model.delete",
        target_type="captcha_model",
        target_id=str(model.id),
        details={
            "captcha_type": model.captcha_type,
            "version": model.version,
        },
    )
    db.delete(model)
    db.commit()

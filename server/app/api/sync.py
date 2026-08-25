from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime
from typing import Any

from fastapi import APIRouter, Query
from sqlalchemy import or_, select

from ..config import get_settings
from ..database import utcnow
from ..dependencies import BusinessContext, Db
from ..errors import ApiError
from ..models import (
    Account,
    ActivityEvent,
    ChangeLog,
    MetricDefinition,
    Road,
    SyncReceipt,
    WorkflowBatch,
    WorkflowRun,
)
from ..permissions import (
    ROAD_ADMIN,
    account_type,
    effective_data_scope,
    legacy_stats_scope,
    participates_in_statistics,
    scoped_account_ids,
)
from ..realtime import update_hub
from ..schemas import (
    ChangeView,
    SnapshotResponse,
    SyncItem,
    SyncItemResult,
    SyncPullResponse,
    SyncPushRequest,
    SyncPushResponse,
)
from ..services import append_change, aware, latest_revision

router = APIRouter(prefix="/sync", tags=["synchronization"])

ALLOWED_PAYLOAD_KEYS = {
    "activity_event": {
        "metric_key",
        "amount",
        "yellow_amount",
        "business_date",
        "source",
        "task_id",
        "summary",
    },
    "workflow_batch_snapshot": {
        "status",
        "input_fingerprint",
        "business_date",
        "started_at",
        "completed_at",
        "elapsed_ms",
        "active_ms",
        "paused_ms",
        "retry_count",
        "counters",
    },
    "workflow_run_snapshot": {
        "batch_id",
        "status",
        "step_index",
        "started_at",
        "ended_at",
        "elapsed_ms",
        "active_ms",
        "paused_ms",
        "retry_count",
    },
}
TERMINAL_STATUSES = {"succeeded", "cancelled", "stopped", "failed", "interrupted"}
STATUS_VALUES = {
    "incomplete",
    "running",
    "paused",
    "stopping",
    "stopped",
    "succeeded",
    "failed",
    "interrupted",
    "cancelled",
}
HEX_64 = re.compile(r"^[0-9a-fA-F]{64}$")
KEY_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")


def _require_int(
    payload: dict[str, Any],
    name: str,
    *,
    minimum: int = 0,
    maximum: int = 2_147_483_647,
    default: int | None = None,
) -> int:
    value = payload.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ApiError("invalid_sync_payload", f"{name} 必须是整数", status_code=422)
    if not minimum <= value <= maximum:
        raise ApiError("invalid_sync_payload", f"{name} 超出允许范围", status_code=422)
    return value


def _parse_date(value: Any, name: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise ApiError("invalid_sync_payload", f"{name} 不是有效日期", status_code=422) from exc


def _parse_datetime(value: Any, name: str, *, optional: bool = False) -> datetime | None:
    if value in (None, "") and optional:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ApiError("invalid_sync_payload", f"{name} 不是有效时间", status_code=422) from exc
    if parsed.tzinfo is None:
        raise ApiError("invalid_sync_payload", f"{name} 必须包含时区", status_code=422)
    return parsed.astimezone(UTC)


def _validate_common(item: SyncItem) -> None:
    settings = get_settings()
    encoded = json.dumps(
        item.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > settings.max_sync_item_bytes:
        raise ApiError(
            "sync_item_too_large",
            "单个同步项不能超过 64 KiB",
            status_code=413,
        )
    if item.occurred_at.tzinfo is None:
        raise ApiError(
            "invalid_sync_payload",
            "occurred_at 必须包含时区",
            status_code=422,
        )
    unknown = set(item.payload) - ALLOWED_PAYLOAD_KEYS[item.kind]
    if unknown:
        raise ApiError(
            "unknown_sync_fields",
            "同步载荷包含未允许字段",
            status_code=422,
            details={"fields": sorted(unknown)},
        )


def _validate_status(payload: dict[str, Any]) -> str:
    status = payload.get("status")
    if status not in STATUS_VALUES:
        raise ApiError("invalid_sync_payload", "status 无效", status_code=422)
    return str(status)


def _validate_activity(payload: dict[str, Any]) -> dict[str, Any]:
    metric_key = str(payload.get("metric_key") or "")
    if not KEY_RE.fullmatch(metric_key):
        raise ApiError("invalid_sync_payload", "metric_key 无效", status_code=422)
    amount = _require_int(payload, "amount", minimum=0, maximum=10_000_000)
    business_date = _parse_date(payload.get("business_date"), "business_date")
    source = str(payload.get("source") or "client")
    if len(source) > 60 or not KEY_RE.fullmatch(source):
        raise ApiError("invalid_sync_payload", "source 无效", status_code=422)
    task_id = payload.get("task_id")
    if task_id is not None and (
        not isinstance(task_id, str)
        or len(task_id) > 120
        or not re.fullmatch(r"[A-Za-z0-9_.:-]+", task_id)
    ):
        raise ApiError("invalid_sync_payload", "task_id 无效", status_code=422)
    yellow_amount = payload.get("yellow_amount")
    if yellow_amount is not None:
        yellow_amount = _require_int(
            payload,
            "yellow_amount",
            maximum=amount,
        )
    summary = payload.get("summary") or {}
    if not isinstance(summary, dict) or set(summary) - {
        "row_count",
        "violation_counts",
        "yellow_violation_counts",
    }:
        raise ApiError("invalid_sync_payload", "summary 只允许汇总数量", status_code=422)
    normalized_summary: dict[str, Any] = {}
    if "row_count" in summary:
        normalized_summary["row_count"] = _require_int(summary, "row_count", maximum=10_000_000)
    for field_name in ("violation_counts", "yellow_violation_counts"):
        if field_name not in summary:
            continue
        values = summary[field_name]
        if not isinstance(values, dict) or len(values) > 200:
            raise ApiError("invalid_sync_payload", "违规原因汇总无效", status_code=422)
        normalized_counts: dict[str, dict[str, int]] = {}
        for reason, count in values.items():
            if not isinstance(reason, str) or not reason.strip() or len(reason) > 120:
                raise ApiError("invalid_sync_payload", "违规原因无效", status_code=422)
            if isinstance(count, int) and not isinstance(count, bool):
                count = {"total": count, "has_phone": 0, "other": count}
            if not isinstance(count, dict) or set(count) - {"total", "has_phone", "other"}:
                raise ApiError("invalid_sync_payload", "违规原因数量无效", status_code=422)
            normalized_values = {}
            for key in ("total", "has_phone", "other"):
                number = count.get(key, 0)
                if (
                    isinstance(number, bool)
                    or not isinstance(number, int)
                    or not 0 <= number <= 10_000_000
                ):
                    raise ApiError(
                        "invalid_sync_payload",
                        "违规原因数量无效",
                        status_code=422,
                    )
                normalized_values[key] = number
            normalized_counts[reason.strip()] = normalized_values
        normalized_summary[field_name] = normalized_counts
    return {
        "metric_key": metric_key,
        "amount": amount,
        "yellow_amount": yellow_amount,
        "business_date": business_date,
        "source": source,
        "task_id": task_id,
        "summary": normalized_summary,
    }


def _timing_fields(payload: dict[str, Any]) -> dict[str, int]:
    return {
        name: _require_int(payload, name, default=0)
        for name in ("elapsed_ms", "active_ms", "paused_ms", "retry_count")
    }


def _validate_batch(payload: dict[str, Any]) -> dict[str, Any]:
    fingerprint = str(payload.get("input_fingerprint") or "")
    if not HEX_64.fullmatch(fingerprint):
        raise ApiError(
            "invalid_sync_payload",
            "input_fingerprint 必须是不可逆 SHA-256 指纹",
            status_code=422,
        )
    counters = payload.get("counters") or {}
    if not isinstance(counters, dict) or len(counters) > 200:
        raise ApiError("invalid_sync_payload", "counters 无效", status_code=422)
    normalized_counters = {}
    for key, value in counters.items():
        if not KEY_RE.fullmatch(str(key)):
            raise ApiError("invalid_sync_payload", "counters 指标名无效", status_code=422)
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 10_000_000:
            raise ApiError("invalid_sync_payload", "counters 数量无效", status_code=422)
        normalized_counters[str(key)] = value
    return {
        "status": _validate_status(payload),
        "input_fingerprint": fingerprint.lower(),
        "business_date": _parse_date(payload.get("business_date"), "business_date"),
        "started_at": _parse_datetime(payload.get("started_at"), "started_at", optional=True),
        "completed_at": _parse_datetime(payload.get("completed_at"), "completed_at", optional=True),
        "counters": normalized_counters,
        **_timing_fields(payload),
    }


def _validate_run(payload: dict[str, Any]) -> dict[str, Any]:
    batch_id = str(payload.get("batch_id") or "")
    if not batch_id or len(batch_id) > 128:
        raise ApiError("invalid_sync_payload", "batch_id 无效", status_code=422)
    return {
        "batch_id": batch_id,
        "status": _validate_status(payload),
        "step_index": _require_int(payload, "step_index", maximum=1000, default=0),
        "started_at": _parse_datetime(payload.get("started_at"), "started_at", optional=True),
        "ended_at": _parse_datetime(payload.get("ended_at"), "ended_at", optional=True),
        **_timing_fields(payload),
    }


def _change_payload_for_activity(row: ActivityEvent) -> dict[str, Any]:
    return {
        "account_id": str(row.account_id),
        "event_uid": str(row.event_uid),
        "metric_key": row.metric_key,
        "amount": row.amount,
        "yellow_amount": row.yellow_amount,
        "business_date": row.business_date.isoformat(),
        "source": row.source,
        "task_id": row.task_id,
        "summary": row.summary,
        "occurred_at": row.occurred_at.isoformat(),
    }


def _process_item(
    db: Db,
    account: Account,
    item: SyncItem,
) -> SyncItemResult:
    _validate_common(item)
    receipt = db.scalar(
        select(SyncReceipt).where(
            SyncReceipt.account_id == account.id,
            SyncReceipt.event_uid == item.event_uid,
        )
    )
    if receipt:
        return SyncItemResult(
            event_uid=item.event_uid,
            status="duplicate",
            server_revision=receipt.server_revision,
        )
    # Road managers and test accounts do not contribute business statistics.
    # Their items are still acknowledged so older clients can drain their
    # outbox without creating server-side metric rows.
    if not participates_in_statistics(account):
        server_revision = latest_revision(db)
        db.add(
            SyncReceipt(
                account_id=account.id,
                event_uid=item.event_uid,
                kind=item.kind,
                entity_id=item.entity_id,
                server_revision=server_revision,
            )
        )
        return SyncItemResult(
            event_uid=item.event_uid,
            status="accepted",
            server_revision=server_revision,
        )
    occurred_at = item.occurred_at.astimezone(UTC)
    if aware(account.data_reset_at) and occurred_at <= aware(account.data_reset_at):
        raise ApiError(
            "data_reset_tombstone",
            "该同步项早于最近一次数据重置，已永久拒绝",
            status_code=409,
        )
    if item.kind == "activity_event":
        if item.revision != 1:
            raise ApiError("invalid_revision", "活动事件修订号必须为 1", status_code=422)
        values = _validate_activity(item.payload)
        if db.get(MetricDefinition, values["metric_key"]) is None:
            db.add(
                MetricDefinition(
                    metric_key=values["metric_key"],
                    label=values["metric_key"],
                    unit="条",
                    sort_order=1000,
                )
            )
        row = ActivityEvent(
            account_id=account.id,
            event_uid=item.event_uid,
            occurred_at=occurred_at,
            **values,
        )
        db.add(row)
        db.flush()
        change = append_change(
            db,
            account_id=account.id,
            kind=item.kind,
            entity_id=item.entity_id,
            entity_revision=1,
            payload=_change_payload_for_activity(row),
            occurred_at=occurred_at,
        )
    elif item.kind == "workflow_batch_snapshot":
        values = _validate_batch(item.payload)
        row = db.scalar(
            select(WorkflowBatch).where(
                WorkflowBatch.account_id == account.id,
                WorkflowBatch.entity_id == item.entity_id,
            )
        )
        if row and item.revision <= row.revision:
            return SyncItemResult(event_uid=item.event_uid, status="stale")
        if row and row.status == "succeeded" and values["status"] != "succeeded":
            raise ApiError(
                "terminal_state_regression",
                "成功终态不能回退",
                status_code=409,
            )
        if row is None:
            row = WorkflowBatch(
                account_id=account.id,
                entity_id=item.entity_id,
                event_uid=item.event_uid,
                revision=item.revision,
                occurred_at=occurred_at,
                **values,
            )
            db.add(row)
        else:
            row.event_uid = item.event_uid
            row.revision = item.revision
            row.occurred_at = occurred_at
            for name, value in values.items():
                setattr(row, name, value)
        db.flush()
        change = append_change(
            db,
            account_id=account.id,
            kind=item.kind,
            entity_id=item.entity_id,
            entity_revision=item.revision,
            payload={
                "account_id": str(account.id),
                "event_uid": str(item.event_uid),
                **{
                    key: value.isoformat() if isinstance(value, (date, datetime)) else value
                    for key, value in values.items()
                },
            },
            occurred_at=occurred_at,
        )
    else:
        values = _validate_run(item.payload)
        row = db.scalar(
            select(WorkflowRun).where(
                WorkflowRun.account_id == account.id,
                WorkflowRun.entity_id == item.entity_id,
            )
        )
        if row and item.revision <= row.revision:
            return SyncItemResult(event_uid=item.event_uid, status="stale")
        if row and row.status == "succeeded" and values["status"] != "succeeded":
            raise ApiError(
                "terminal_state_regression",
                "成功终态不能回退",
                status_code=409,
            )
        if row is None:
            row = WorkflowRun(
                account_id=account.id,
                entity_id=item.entity_id,
                event_uid=item.event_uid,
                revision=item.revision,
                occurred_at=occurred_at,
                batch_entity_id=values.pop("batch_id"),
                **values,
            )
            db.add(row)
        else:
            row.event_uid = item.event_uid
            row.revision = item.revision
            row.occurred_at = occurred_at
            row.batch_entity_id = values.pop("batch_id")
            for name, value in values.items():
                setattr(row, name, value)
        db.flush()
        payload_values = {
            "batch_id": row.batch_entity_id,
            "status": row.status,
            "step_index": row.step_index,
            "started_at": row.started_at.isoformat() if row.started_at else None,
            "ended_at": row.ended_at.isoformat() if row.ended_at else None,
            "elapsed_ms": row.elapsed_ms,
            "active_ms": row.active_ms,
            "paused_ms": row.paused_ms,
            "retry_count": row.retry_count,
        }
        change = append_change(
            db,
            account_id=account.id,
            kind=item.kind,
            entity_id=item.entity_id,
            entity_revision=item.revision,
            payload={
                "account_id": str(account.id),
                "event_uid": str(item.event_uid),
                **payload_values,
            },
            occurred_at=occurred_at,
        )

    db.add(
        SyncReceipt(
            account_id=account.id,
            event_uid=item.event_uid,
            kind=item.kind,
            entity_id=item.entity_id,
            server_revision=change.revision,
        )
    )
    return SyncItemResult(
        event_uid=item.event_uid,
        status="accepted",
        server_revision=change.revision,
    )


@router.post("/push", response_model=SyncPushResponse)
async def push(
    payload: SyncPushRequest,
    context: BusinessContext,
    db: Db,
) -> SyncPushResponse:
    settings = get_settings()
    if len(payload.items) > settings.max_sync_items:
        raise ApiError(
            "sync_batch_too_large",
            "每批最多允许 100 个同步项",
            status_code=413,
        )
    results: list[SyncItemResult] = []
    for item in payload.items:
        try:
            with db.begin_nested():
                result = _process_item(db, context.account, item)
            results.append(result)
        except ApiError as exc:
            results.append(
                SyncItemResult(
                    event_uid=item.event_uid,
                    status="rejected",
                    code=exc.code,
                    message=exc.message,
                    retryable=exc.retryable,
                )
            )
    db.commit()
    revision = latest_revision(db)
    if any(item.status == "accepted" for item in results):
        await update_hub.broadcast_revision(revision)
    return SyncPushResponse(items=results, latest_revision=revision)


@router.get("/pull", response_model=SyncPullResponse)
def pull(
    context: BusinessContext,
    db: Db,
    after_revision: int = Query(default=0, ge=0),
    limit: int = Query(default=500, ge=1, le=500),
) -> SyncPullResponse:
    scope = effective_data_scope(context.account, context.device.client_version)
    visible_account_ids = scoped_account_ids(
        db,
        context.account,
        context.device.client_version,
    )
    query = select(ChangeLog).where(ChangeLog.revision > after_revision)
    non_statistic_account_ids = select(Account.id).where(
        or_(
            Account.is_test.is_(True),
            Account.account_type == ROAD_ADMIN,
        )
    )
    query = query.where(
        or_(
            ChangeLog.account_id.is_(None),
            ChangeLog.account_id.not_in(non_statistic_account_ids),
            ChangeLog.kind == "account",
        )
    )
    if scope != "all":
        query = query.where(
            or_(
                ChangeLog.account_id.in_(visible_account_ids),
                ChangeLog.account_id.is_(None),
            )
        )
    rows = db.scalars(query.order_by(ChangeLog.revision).limit(limit + 1)).all()
    has_more = len(rows) > limit
    rows = rows[:limit]

    def visible_payload(row: ChangeLog) -> dict[str, Any]:
        payload = dict(row.payload or {})
        if row.kind == "road_membership_changed":
            affected_roads = {
                str(payload.get("old_road_id") or ""),
                str(payload.get("new_road_id") or ""),
            }
            if (
                scope == "road"
                and context.account.road_id is not None
                and str(context.account.road_id) in affected_roads
            ):
                return {"refresh_scope": True}
            return {}
        if (
            scope == "all"
            or row.kind != "account"
            or row.operation != "delete"
            or row.account_id is not None
        ):
            return payload
        if (
            scope == "road"
            and context.account.road_id is not None
            and str(payload.get("road_id") or "") == str(context.account.road_id)
        ):
            return payload
        # Permanent deletion tombstones have no account FK.  Keep the entity
        # id so every client can purge stale cache, but do not disclose the
        # deleted username or hierarchy outside the viewer's data scope.
        return {}

    return SyncPullResponse(
        changes=[
            ChangeView(
                revision=row.revision,
                account_id=row.account_id,
                kind=row.kind,
                entity_id=row.entity_id,
                entity_revision=row.entity_revision,
                operation=row.operation,
                payload=visible_payload(row),
                occurred_at=row.occurred_at,
            )
            for row in rows
        ],
        latest_revision=latest_revision(db),
        has_more=has_more,
        entitlement_revision=context.account.entitlement_revision,
        stats_scope=legacy_stats_scope(scope),
        data_scope=scope,
    )


@router.get("/snapshot", response_model=SnapshotResponse)
def snapshot(context: BusinessContext, db: Db) -> SnapshotResponse:
    scope = effective_data_scope(context.account, context.device.client_version)
    account_ids = scoped_account_ids(
        db,
        context.account,
        context.device.client_version,
    )
    accounts = db.scalars(select(Account).where(Account.id.in_(account_ids))).all()
    statistic_account_ids = [
        row.id for row in accounts if participates_in_statistics(row)
    ]
    metrics = db.scalars(
        select(MetricDefinition)
        .where(MetricDefinition.is_active.is_(True))
        .order_by(MetricDefinition.sort_order, MetricDefinition.metric_key)
    ).all()
    events = db.scalars(
        select(ActivityEvent).where(
            ActivityEvent.account_id.in_(statistic_account_ids),
            ActivityEvent.deleted_at.is_(None),
        )
    ).all()
    batches = db.scalars(
        select(WorkflowBatch).where(
            WorkflowBatch.account_id.in_(statistic_account_ids),
            WorkflowBatch.deleted_at.is_(None),
        )
    ).all()
    runs = db.scalars(
        select(WorkflowRun).where(
            WorkflowRun.account_id.in_(statistic_account_ids),
            WorkflowRun.deleted_at.is_(None),
        )
    ).all()
    return SnapshotResponse(
        revision=latest_revision(db),
        generated_at=utcnow(),
        accounts=[
            {
                "id": str(row.id),
                "username": row.username,
                "display_name": row.display_name,
                "role": row.role,
                "account_type": account_type(row),
                "is_test": row.is_test,
                "stats_scope": legacy_stats_scope(row.data_scope),
                "data_scope": row.data_scope,
                "road_id": str(row.road_id) if row.road_id else None,
                "road_name": (
                    db.get(Road, row.road_id).name if row.road_id else None
                ),
                "is_active": row.is_active,
                "is_archived": row.is_archived,
                "entitlement_revision": row.entitlement_revision,
            }
            for row in accounts
        ],
        metrics=[
            {
                "metric_key": row.metric_key,
                "label": row.label,
                "unit": row.unit,
                "sort_order": row.sort_order,
            }
            for row in metrics
        ],
        activity_events=[_change_payload_for_activity(row) for row in events],
        workflow_batches=[
            {
                "account_id": str(row.account_id),
                "entity_id": row.entity_id,
                "event_uid": str(row.event_uid),
                "revision": row.revision,
                "status": row.status,
                "input_fingerprint": row.input_fingerprint,
                "business_date": row.business_date.isoformat(),
                "started_at": row.started_at.isoformat() if row.started_at else None,
                "completed_at": row.completed_at.isoformat() if row.completed_at else None,
                "elapsed_ms": row.elapsed_ms,
                "active_ms": row.active_ms,
                "paused_ms": row.paused_ms,
                "retry_count": row.retry_count,
                "counters": row.counters,
            }
            for row in batches
        ],
        workflow_runs=[
            {
                "account_id": str(row.account_id),
                "entity_id": row.entity_id,
                "event_uid": str(row.event_uid),
                "revision": row.revision,
                "batch_id": row.batch_entity_id,
                "status": row.status,
                "step_index": row.step_index,
                "started_at": row.started_at.isoformat() if row.started_at else None,
                "ended_at": row.ended_at.isoformat() if row.ended_at else None,
                "elapsed_ms": row.elapsed_ms,
                "active_ms": row.active_ms,
                "paused_ms": row.paused_ms,
                "retry_count": row.retry_count,
            }
            for row in runs
        ],
        entitlement_revision=context.account.entitlement_revision,
        stats_scope=legacy_stats_scope(scope),
        data_scope=scope,
    )

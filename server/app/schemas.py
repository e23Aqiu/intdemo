from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AccountView(StrictModel):
    id: uuid.UUID
    username: str
    display_name: str
    role: Literal["admin", "user"]
    stats_scope: Literal["own", "all"]
    device_limit: int
    is_active: bool
    is_archived: bool
    must_change_password: bool
    entitlement_revision: int
    active_device_count: int = 0
    online_device_count: int = 0
    created_at: datetime
    updated_at: datetime


class DeviceView(StrictModel):
    id: uuid.UUID
    device_uid: uuid.UUID
    name: str
    client_version: str
    created_at: datetime
    last_seen_at: datetime
    revoked_at: datetime | None
    revoked_reason: str | None


class LoginRequest(StrictModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=256)
    device_uid: uuid.UUID
    device_name: str = Field(default="Windows device", min_length=1, max_length=160)
    client_version: str = Field(default="0.2.5", min_length=1, max_length=40)
    control_client: bool = False

    @field_validator("username")
    @classmethod
    def normalize_username(cls, value: str) -> str:
        return value.strip().lower()


class RefreshRequest(StrictModel):
    refresh_token: str = Field(min_length=20, max_length=1024)
    device_uid: uuid.UUID


class ChangePasswordRequest(StrictModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=6, max_length=256)


class LogoutRequest(StrictModel):
    refresh_token: str | None = Field(default=None, max_length=1024)


class TokenBundle(StrictModel):
    token_type: Literal["bearer"] = "bearer"
    access_token: str
    access_expires_at: datetime
    refresh_token: str
    refresh_expires_at: datetime
    offline_entitlement: str
    offline_expires_at: datetime
    offline_public_key: str
    account: AccountView
    device: DeviceView
    server_revision: int


class AccountCreate(StrictModel):
    username: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_.-]+$")
    display_name: str = Field(min_length=1, max_length=120)
    role: Literal["admin", "user"] = "user"
    stats_scope: Literal["own", "all"] = "own"
    device_limit: int = Field(default=10000, ge=1, le=10000)
    is_active: bool = True

    @field_validator("username")
    @classmethod
    def normalize_username(cls, value: str) -> str:
        return value.strip().lower()

    @field_validator("display_name")
    @classmethod
    def normalize_display_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("站点显示名不能为空")
        return value


class AccountUpdate(StrictModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    role: Literal["admin", "user"] | None = None
    stats_scope: Literal["own", "all"] | None = None
    device_limit: int | None = Field(default=None, ge=1, le=10000)
    is_active: bool | None = None

    @field_validator("display_name")
    @classmethod
    def normalize_display_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("站点显示名不能为空")
        return value

    @model_validator(mode="after")
    def reject_explicit_nulls(self) -> AccountUpdate:
        if any(getattr(self, field_name) is None for field_name in self.model_fields_set):
            raise ValueError("更新字段不能为 null")
        return self


class DataResetRequest(StrictModel):
    account_id: uuid.UUID
    confirmation: Literal["RESET"]


class ConnectionTestClientView(StrictModel):
    id: uuid.UUID
    account_id: uuid.UUID
    username: str
    display_name: str
    device_uid: uuid.UUID
    name: str
    client_version: str
    last_seen_at: datetime
    connected: bool
    blocked: bool


class ConnectionTestOverview(StrictModel):
    global_blocked: bool
    clients: list[ConnectionTestClientView]


class ConnectionTestActionResult(StrictModel):
    global_blocked: bool
    device_id: uuid.UUID | None = None
    blocked: bool
    closed_websockets: int = 0


class SyncItem(StrictModel):
    schema_version: Literal[1]
    event_uid: uuid.UUID
    kind: Literal[
        "activity_event",
        "workflow_batch_snapshot",
        "workflow_run_snapshot",
    ]
    entity_id: str = Field(min_length=1, max_length=128)
    revision: int = Field(ge=1)
    occurred_at: datetime
    payload: dict[str, Any]


class SyncPushRequest(StrictModel):
    items: list[SyncItem]


class SyncItemResult(StrictModel):
    event_uid: uuid.UUID
    status: Literal["accepted", "duplicate", "stale", "rejected"]
    server_revision: int | None = None
    code: str | None = None
    message: str | None = None
    retryable: bool = False


class SyncPushResponse(StrictModel):
    items: list[SyncItemResult]
    latest_revision: int


class ChangeView(StrictModel):
    revision: int
    account_id: uuid.UUID | None
    kind: str
    entity_id: str
    entity_revision: int
    operation: Literal["upsert", "delete"]
    payload: dict[str, Any]
    occurred_at: datetime


class SyncPullResponse(StrictModel):
    changes: list[ChangeView]
    latest_revision: int
    has_more: bool
    entitlement_revision: int
    stats_scope: Literal["own", "all"]


class SnapshotResponse(StrictModel):
    revision: int
    generated_at: datetime
    accounts: list[dict[str, Any]]
    metrics: list[dict[str, Any]]
    activity_events: list[dict[str, Any]]
    workflow_batches: list[dict[str, Any]]
    workflow_runs: list[dict[str, Any]]
    entitlement_revision: int
    stats_scope: Literal["own", "all"]


class AnnouncementAttachmentInput(StrictModel):
    file_name: str = Field(min_length=1, max_length=255)
    content_type: str = Field(default="application/octet-stream", max_length=160)
    kind: Literal["image", "file"] = "file"
    content_base64: str = Field(min_length=1, max_length=14_100_000)

    @field_validator("file_name", "content_type")
    @classmethod
    def strip_attachment_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("附件名称和类型不能为空")
        return value


class AnnouncementCreate(StrictModel):
    title: str = Field(min_length=1, max_length=200)
    ticker_text: str = Field(min_length=1, max_length=500)
    body_html: str = Field(min_length=1, max_length=200_000)
    show_on_startup: bool = False
    target_account_ids: list[uuid.UUID] = Field(default_factory=list, max_length=500)
    attachments: list[AnnouncementAttachmentInput] = Field(
        default_factory=list,
        max_length=8,
    )

    @field_validator("title", "ticker_text", "body_html")
    @classmethod
    def strip_announcement_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("公告标题、轮播文字和正文不能为空")
        return value


class AnnouncementUpdate(StrictModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    ticker_text: str | None = Field(default=None, min_length=1, max_length=500)
    body_html: str | None = Field(default=None, min_length=1, max_length=200_000)
    show_on_startup: bool | None = None
    target_account_ids: list[uuid.UUID] | None = Field(
        default=None,
        max_length=500,
    )
    is_active: bool | None = None

    @field_validator("title", "ticker_text", "body_html")
    @classmethod
    def strip_optional_announcement_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("公告文字字段不能为空")
        return value

    @model_validator(mode="after")
    def reject_null_updates(self) -> AnnouncementUpdate:
        if any(getattr(self, field_name) is None for field_name in self.model_fields_set):
            raise ValueError("更新字段不能为 null")
        return self


class AnnouncementReadRequest(StrictModel):
    startup_shown: bool = False


class AdminContactMessageCreate(StrictModel):
    message: str = Field(min_length=1, max_length=2_000)

    @field_validator("message")
    @classmethod
    def strip_message(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("消息内容不能为空")
        return value

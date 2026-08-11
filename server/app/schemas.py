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
    client_version: str = Field(default="1.0.6", min_length=1, max_length=40)
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


class CaptchaModelView(StrictModel):
    id: uuid.UUID
    captcha_type: Literal["numeric", "click"]
    version: str
    algorithm: str
    status: Literal["candidate", "current", "archived"]
    artifact_sha256: str
    artifact_size: int
    sample_count: int
    test_count: int
    correct_count: int
    accuracy: float
    metrics: dict[str, Any]
    created_at: datetime
    activated_at: datetime | None


CaptchaUploadMode = Literal["off", "metrics_only", "samples_and_metrics"]


class CaptchaLearningPolicyView(StrictModel):
    upload_mode: CaptchaUploadMode
    # Compatibility for pre-three-mode clients. Metrics-only is exposed as
    # false so an older client never uploads images under that policy.
    upload_enabled: bool
    revision: int
    updated_at: datetime
    active_models: dict[str, CaptchaModelView]


class CaptchaLearningPolicyUpdate(StrictModel):
    upload_mode: CaptchaUploadMode | None = None
    upload_enabled: bool | None = None

    @model_validator(mode="after")
    def validate_policy_update(self) -> CaptchaLearningPolicyUpdate:
        if self.upload_mode is None and self.upload_enabled is None:
            raise ValueError("必须提供上传策略")
        if self.upload_mode is not None and self.upload_enabled is not None:
            raise ValueError("上传策略不能同时使用新旧字段")
        return self

    def resolved_upload_mode(self) -> CaptchaUploadMode:
        if self.upload_mode is not None:
            return self.upload_mode
        return "samples_and_metrics" if self.upload_enabled else "off"


class CaptchaAttemptCreate(StrictModel):
    captcha_type: Literal["numeric", "click"]
    source: Literal["transport_numeric", "business_click"] | None = None
    model_version: str = Field(min_length=1, max_length=80)
    success: bool
    assisted: bool = False
    occurred_at: datetime
    image_mime: Literal["image/png", "image/jpeg"] | None = None
    image_base64: str | None = Field(default=None, max_length=1_500_000)
    answer: dict[str, Any] | None = None

    @field_validator("model_version")
    @classmethod
    def validate_model_version(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("模型版本不能为空")
        return value

    @model_validator(mode="after")
    def validate_attempt_sample(self) -> CaptchaAttemptCreate:
        if self.occurred_at.tzinfo is None:
            raise ValueError("验证码尝试时间必须包含时区")
        expected_source = {
            "numeric": "transport_numeric",
            "click": "business_click",
        }[self.captcha_type]
        if self.source is not None and self.source != expected_source:
            raise ValueError("验证码类型与来源不匹配")
        sample_fields = (self.image_mime, self.image_base64, self.answer)
        supplied_count = sum(value is not None for value in sample_fields)
        if supplied_count not in {0, len(sample_fields)}:
            raise ValueError("验证码图片、图片类型和答案必须同时提供")
        if not self.success and any(value is not None for value in sample_fields):
            raise ValueError("失败尝试不能包含验证码图片或答案")
        return self


class CaptchaAttemptResult(StrictModel):
    stored: bool
    sample_stored: bool
    sample_id: uuid.UUID | None = None
    policy_revision: int


class CaptchaAttemptMetric(StrictModel):
    captcha_type: Literal["numeric", "click"]
    model_version: str
    attempt_count: int
    success_count: int
    success_rate: float


class CaptchaDatasetStats(StrictModel):
    total_count: int
    total_bytes: int
    numeric_count: int
    numeric_bytes: int
    click_count: int
    click_bytes: int


class CaptchaSampleSummary(StrictModel):
    id: uuid.UUID
    captcha_type: Literal["numeric", "click"]
    # Older deployments may have persisted a source alias.  Keep the sample
    # listing readable even when such a row is encountered; ingestion still
    # validates the canonical source for new records.
    source: str
    answer: dict[str, Any]
    model_version: str
    origin: str
    image_size: int
    captured_at: datetime
    created_at: datetime
    image_mime: Literal["image/png", "image/jpeg"] | None = None
    image_available: bool = True


class CaptchaSamplePage(StrictModel):
    items: list[CaptchaSampleSummary]
    total: int
    limit: int
    offset: int


class CaptchaSampleDeleteRequest(StrictModel):
    sample_ids: list[uuid.UUID] = Field(min_length=1, max_length=200)

    @field_validator("sample_ids")
    @classmethod
    def validate_unique_sample_ids(
        cls,
        value: list[uuid.UUID],
    ) -> list[uuid.UUID]:
        if len(set(value)) != len(value):
            raise ValueError("待删除样本不能重复")
        return value


class CaptchaSampleDeleteResult(StrictModel):
    deleted_count: int
    missing_count: int


class CaptchaLearningOverview(StrictModel):
    policy: CaptchaLearningPolicyView
    dataset: CaptchaDatasetStats
    attempts: list[CaptchaAttemptMetric]
    models: list[CaptchaModelView]


class CaptchaModelCreate(StrictModel):
    captcha_type: Literal["numeric", "click"]
    version: str = Field(
        min_length=1,
        max_length=80,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$",
    )
    algorithm: Literal["knn-pixels-v1"]
    artifact_base64: str = Field(min_length=1, max_length=28_000_000)
    sample_count: int = Field(ge=1, le=10_000_000)
    test_count: int = Field(ge=1, le=10_000_000)
    correct_count: int = Field(ge=0, le=10_000_000)
    metrics: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_counts(self) -> CaptchaModelCreate:
        if self.correct_count > self.test_count:
            raise ValueError("正确数量不能大于测试数量")
        return self


class CaptchaDatasetImportResult(StrictModel):
    imported_count: int
    duplicate_count: int
    skipped_count: int

from __future__ import annotations

import base64
import binascii
import uuid
from pathlib import PurePath
from urllib.parse import quote

from fastapi import APIRouter, Query, Request, Response
from sqlalchemy import delete, exists, func, or_, select, update

from ..database import utcnow
from ..dependencies import AdminContext, BusinessContext, Db
from ..errors import ApiError
from ..models import (
    Account,
    AdminContactMessage,
    Announcement,
    AnnouncementAttachment,
    AnnouncementReceipt,
    AnnouncementTarget,
    ContactMessageAttachment,
)
from ..realtime import update_hub
from ..schemas import (
    AdminContactMessageCreate,
    AnnouncementAttachmentInput,
    AnnouncementCreate,
    AnnouncementReadRequest,
    AnnouncementUpdate,
    ContactConversationStatusUpdate,
)
from ..services import audit

router = APIRouter(tags=["announcements"])

MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
MAX_ANNOUNCEMENT_ATTACHMENT_BYTES = 25 * 1024 * 1024
MAX_CONTACT_ATTACHMENT_BYTES = 25 * 1024 * 1024


def _target_visibility_clause(account_id: uuid.UUID):
    has_targets = (
        exists()
        .where(AnnouncementTarget.announcement_id == Announcement.id)
        .correlate(Announcement)
    )
    targets_account = (
        exists()
        .where(
            AnnouncementTarget.announcement_id == Announcement.id,
            AnnouncementTarget.account_id == account_id,
        )
        .correlate(Announcement)
    )
    return or_(~has_targets, targets_account)


def _visible_announcement(
    db: Db,
    announcement_id: uuid.UUID,
    account: Account,
) -> Announcement:
    announcement = db.get(Announcement, announcement_id)
    if not announcement:
        raise ApiError("announcement_not_found", "公告不存在", status_code=404)
    if account.role == "admin":
        return announcement
    if not announcement.is_active:
        raise ApiError("announcement_not_found", "公告不存在", status_code=404)
    target_ids = set(
        db.scalars(
            select(AnnouncementTarget.account_id).where(
                AnnouncementTarget.announcement_id == announcement.id
            )
        ).all()
    )
    if target_ids and account.id not in target_ids:
        raise ApiError("announcement_not_found", "公告不存在", status_code=404)
    return announcement


def _attachment_view(attachment: AnnouncementAttachment) -> dict:
    return {
        "id": attachment.id,
        "file_name": attachment.file_name,
        "content_type": attachment.content_type,
        "kind": attachment.kind,
        "size": attachment.size,
        "created_at": attachment.created_at,
    }


def _contact_attachment_view(attachment: ContactMessageAttachment) -> dict:
    return {
        "id": attachment.id,
        "file_name": attachment.file_name,
        "content_type": attachment.content_type,
        "kind": attachment.kind,
        "size": attachment.size,
        "created_at": attachment.created_at,
    }


def _announcement_view(
    db: Db,
    announcement: Announcement,
    *,
    account_id: uuid.UUID | None = None,
    startup_candidate: bool = False,
) -> dict:
    targets = db.execute(
        select(
            AnnouncementTarget.account_id,
            Account.username,
            Account.display_name,
        )
        .join(Account, Account.id == AnnouncementTarget.account_id)
        .where(AnnouncementTarget.announcement_id == announcement.id)
        .order_by(Account.username)
    ).all()
    attachments = db.scalars(
        select(AnnouncementAttachment)
        .where(AnnouncementAttachment.announcement_id == announcement.id)
        .order_by(AnnouncementAttachment.created_at, AnnouncementAttachment.file_name)
    ).all()
    creator = db.get(Account, announcement.created_by_id) if announcement.created_by_id else None
    receipt = (
        db.get(AnnouncementReceipt, (announcement.id, account_id))
        if account_id is not None
        else None
    )
    read_users = []
    unread_users = []
    viewer = db.get(Account, account_id) if account_id is not None else None
    if viewer is not None and viewer.role == "admin":
        target_ids = [row.account_id for row in targets]
        recipient_statement = (
            select(Account)
            .where(Account.role == "user", Account.is_archived.is_(False))
            .order_by(Account.username)
        )
        if target_ids:
            recipient_statement = recipient_statement.where(Account.id.in_(target_ids))
        recipients = db.scalars(recipient_statement).all()
        receipt_rows = db.scalars(
            select(AnnouncementReceipt).where(
                AnnouncementReceipt.announcement_id == announcement.id
            )
        ).all()
        receipts_by_account = {item.account_id: item for item in receipt_rows}
        for recipient in recipients:
            recipient_receipt = receipts_by_account.get(recipient.id)
            item = {
                "id": recipient.id,
                "username": recipient.username,
                "display_name": recipient.display_name,
                "read_at": recipient_receipt.read_at if recipient_receipt else None,
            }
            (read_users if item["read_at"] else unread_users).append(item)
    return {
        "id": announcement.id,
        "title": announcement.title,
        "ticker_text": announcement.ticker_text,
        "body_html": announcement.body_html,
        "show_on_startup": announcement.show_on_startup,
        "startup_pending": bool(
            startup_candidate
            and announcement.show_on_startup
            and (receipt is None or receipt.startup_shown_at is None)
        ),
        "is_active": announcement.is_active,
        "revision": announcement.revision,
        "created_by_id": announcement.created_by_id,
        "created_by_name": creator.display_name if creator else "管理员",
        "targets": [
            {
                "id": row.account_id,
                "username": row.username,
                "display_name": row.display_name,
            }
            for row in targets
        ],
        "target_account_ids": [row.account_id for row in targets],
        "attachments": [_attachment_view(item) for item in attachments],
        "read_count": len(read_users),
        "unread_count": len(unread_users),
        "read_users": read_users,
        "unread_users": unread_users,
        "read_at": receipt.read_at if receipt else None,
        "created_at": announcement.created_at,
        "updated_at": announcement.updated_at,
    }


def _validated_target_ids(db: Db, account_ids: list[uuid.UUID]) -> list[uuid.UUID]:
    unique_ids = list(dict.fromkeys(account_ids))
    if not unique_ids:
        return []
    accounts = db.scalars(select(Account).where(Account.id.in_(unique_ids))).all()
    by_id = {account.id: account for account in accounts}
    missing = [str(account_id) for account_id in unique_ids if account_id not in by_id]
    if missing:
        raise ApiError(
            "announcement_target_not_found",
            "部分公告接收账号不存在",
            status_code=422,
            details={"account_ids": missing},
        )
    unavailable = [
        account.username for account in accounts if account.is_archived or not account.is_active
    ]
    if unavailable:
        raise ApiError(
            "announcement_target_unavailable",
            "不能向停用或归档账号发布公告",
            status_code=422,
            details={"usernames": sorted(unavailable)},
        )
    return unique_ids


def _safe_file_name(value: str) -> str:
    normalized = str(value or "").replace("\\", "/").strip()
    name = PurePath(normalized).name.strip()
    if not name or name in {".", ".."}:
        raise ApiError("invalid_attachment_name", "附件名称无效", status_code=422)
    return name[:255]


def _decode_attachment(payload: AnnouncementAttachmentInput) -> tuple[str, bytes]:
    try:
        content = base64.b64decode(payload.content_base64, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ApiError(
            "invalid_attachment_data",
            "附件内容不是有效的 Base64 数据",
            status_code=422,
        ) from exc
    if not content:
        raise ApiError("empty_attachment", "附件内容不能为空", status_code=422)
    if len(content) > MAX_ATTACHMENT_BYTES:
        raise ApiError(
            "attachment_too_large",
            "单个附件不能超过 10 MB",
            status_code=413,
        )
    return _safe_file_name(payload.file_name), content


def _new_attachment(
    announcement_id: uuid.UUID,
    payload: AnnouncementAttachmentInput,
) -> AnnouncementAttachment:
    file_name, content = _decode_attachment(payload)
    content_type = payload.content_type.strip().lower()
    kind = "image" if payload.kind == "image" and content_type.startswith("image/") else "file"
    return AnnouncementAttachment(
        announcement_id=announcement_id,
        file_name=file_name,
        content_type=content_type,
        kind=kind,
        size=len(content),
        content=content,
    )


def _new_contact_attachment(
    message_id: uuid.UUID,
    payload: AnnouncementAttachmentInput,
) -> ContactMessageAttachment:
    file_name, content = _decode_attachment(payload)
    content_type = payload.content_type.strip().lower()
    kind = "image" if payload.kind == "image" and content_type.startswith("image/") else "file"
    return ContactMessageAttachment(
        message_id=message_id,
        file_name=file_name,
        content_type=content_type,
        kind=kind,
        size=len(content),
        content=content,
    )


def _add_contact_attachments(
    db: Db,
    message_id: uuid.UUID,
    payloads: list[AnnouncementAttachmentInput],
) -> None:
    attachments = [_new_contact_attachment(message_id, item) for item in payloads]
    if sum(item.size for item in attachments) > MAX_CONTACT_ATTACHMENT_BYTES:
        raise ApiError(
            "contact_attachments_too_large",
            "单条消息的附件总大小不能超过 25 MB",
            status_code=413,
        )
    db.add_all(attachments)


def _clear_receipts(db: Db, announcement_id: uuid.UUID) -> None:
    db.execute(
        delete(AnnouncementReceipt).where(AnnouncementReceipt.announcement_id == announcement_id)
    )


def _contact_thread_root(
    db: Db,
    message_id: uuid.UUID,
) -> AdminContactMessage:
    row = db.get(AdminContactMessage, message_id)
    if row is None:
        raise ApiError("message_not_found", "消息会话不存在", status_code=404)
    root_id = row.thread_id or row.id
    root = db.get(AdminContactMessage, root_id)
    if root is None:
        raise ApiError("message_not_found", "消息会话不存在", status_code=404)
    return root


def _contact_thread_rows(
    db: Db,
    root: AdminContactMessage,
) -> list[AdminContactMessage]:
    return list(
        db.scalars(
            select(AdminContactMessage)
            .where(
                or_(
                    AdminContactMessage.id == root.id,
                    AdminContactMessage.thread_id == root.id,
                )
            )
            .order_by(AdminContactMessage.created_at, AdminContactMessage.id)
        ).all()
    )


def _contact_conversation_view(
    db: Db,
    root: AdminContactMessage,
) -> dict:
    rows = _contact_thread_rows(db, root)
    account_ids = {row.sender_account_id for row in rows}
    accounts = db.scalars(select(Account).where(Account.id.in_(account_ids))).all()
    accounts_by_id = {account.id: account for account in accounts}
    owner = accounts_by_id.get(root.sender_account_id)
    announcement = db.get(Announcement, root.announcement_id) if root.announcement_id else None
    messages = []
    attachment_rows = db.scalars(
        select(ContactMessageAttachment)
        .where(ContactMessageAttachment.message_id.in_([row.id for row in rows]))
        .order_by(ContactMessageAttachment.created_at, ContactMessageAttachment.file_name)
    ).all() if rows else []
    attachments_by_message = {}
    for attachment in attachment_rows:
        attachments_by_message.setdefault(attachment.message_id, []).append(
            _contact_attachment_view(attachment)
        )
    for row in rows:
        sender = accounts_by_id.get(row.sender_account_id)
        messages.append(
            {
                "id": row.id,
                "sender_account_id": row.sender_account_id,
                "sender_role": sender.role if sender else "unknown",
                "sender_username": sender.username if sender else "已删除账号",
                "sender_display_name": sender.display_name if sender else "已删除账号",
                "message": row.message,
                "attachments": attachments_by_message.get(row.id, []),
                "created_at": row.created_at,
                "admin_read_at": row.read_at,
                "user_read_at": row.read_by_user_at,
            }
        )
    last_message = messages[-1] if messages else None
    unread_for_admin = sum(
        1
        for row in rows
        if accounts_by_id.get(row.sender_account_id)
        and accounts_by_id[row.sender_account_id].role == "user"
        and row.read_at is None
    )
    unread_for_user = sum(
        1
        for row in rows
        if accounts_by_id.get(row.sender_account_id)
        and accounts_by_id[row.sender_account_id].role == "admin"
        and row.read_by_user_at is None
    )
    return {
        "id": root.id,
        "thread_id": root.id,
        "sender_account_id": root.sender_account_id,
        "sender_username": owner.username if owner else "已删除账号",
        "sender_display_name": owner.display_name if owner else "已删除账号",
        "announcement_id": root.announcement_id,
        "announcement_title": announcement.title if announcement else None,
        "message": root.message,
        "created_at": root.created_at,
        "updated_at": rows[-1].created_at if rows else root.created_at,
        "read_at": None if unread_for_admin else root.read_at or root.created_at,
        "unread_count": unread_for_admin,
        "user_unread_count": unread_for_user,
        "status": root.status or "open",
        "last_message": last_message,
        "messages": messages,
    }


def _user_unread_contact_count(db: Db, account_id: uuid.UUID) -> int:
    owned_root_ids = select(AdminContactMessage.id).where(
        AdminContactMessage.sender_account_id == account_id,
        or_(
            AdminContactMessage.thread_id.is_(None),
            AdminContactMessage.thread_id == AdminContactMessage.id,
        ),
    )
    admin_account_ids = select(Account.id).where(Account.role == "admin")
    return int(
        db.scalar(
            select(func.count(AdminContactMessage.id)).where(
                AdminContactMessage.thread_id.in_(owned_root_ids),
                AdminContactMessage.sender_account_id.in_(admin_account_ids),
                AdminContactMessage.read_by_user_at.is_(None),
            )
        )
        or 0
    )


@router.get("/announcements")
def list_visible_announcements(
    context: BusinessContext,
    db: Db,
    limit: int = Query(default=50, ge=1, le=100),
) -> list[dict]:
    statement = (
        select(Announcement)
        .where(Announcement.is_active.is_(True))
        .where(_target_visibility_clause(context.account.id))
        .order_by(Announcement.created_at.desc())
        .limit(limit)
    )
    rows = db.scalars(statement).all()
    startup_id = next(
        (row.id for row in rows if row.show_on_startup),
        None,
    )
    return [
        _announcement_view(
            db,
            row,
            account_id=context.account.id,
            startup_candidate=row.id == startup_id,
        )
        for row in rows
    ]


@router.post("/announcements/{announcement_id}/read")
def mark_announcement_read(
    announcement_id: uuid.UUID,
    payload: AnnouncementReadRequest,
    context: BusinessContext,
    db: Db,
) -> dict:
    _visible_announcement(db, announcement_id, context.account)
    receipt = db.get(
        AnnouncementReceipt,
        (announcement_id, context.account.id),
    )
    now = utcnow()
    if receipt is None:
        receipt = AnnouncementReceipt(
            announcement_id=announcement_id,
            account_id=context.account.id,
        )
        db.add(receipt)
    if payload.confirmed and receipt.read_at is None:
        receipt.read_at = now
    if payload.startup_shown and receipt.startup_shown_at is None:
        receipt.startup_shown_at = now
    db.commit()
    return {
        "announcement_id": announcement_id,
        "read_at": receipt.read_at,
        "startup_shown_at": receipt.startup_shown_at,
    }


@router.get("/announcements/attachments/{attachment_id}")
def download_announcement_attachment(
    attachment_id: uuid.UUID,
    context: BusinessContext,
    db: Db,
) -> Response:
    attachment = db.get(AnnouncementAttachment, attachment_id)
    if not attachment:
        raise ApiError("attachment_not_found", "附件不存在", status_code=404)
    _visible_announcement(db, attachment.announcement_id, context.account)
    encoded_name = quote(attachment.file_name, safe="")
    return Response(
        content=attachment.content,
        media_type=attachment.content_type,
        headers={
            "Content-Disposition": (
                f"attachment; filename=\"download\"; filename*=UTF-8''{encoded_name}"
            ),
            "Content-Length": str(attachment.size),
        },
    )


@router.get("/messages/attachments/{attachment_id}")
def download_contact_message_attachment(
    attachment_id: uuid.UUID,
    context: BusinessContext,
    db: Db,
) -> Response:
    attachment = db.get(ContactMessageAttachment, attachment_id)
    if attachment is None:
        raise ApiError("attachment_not_found", "附件不存在", status_code=404)
    message = db.get(AdminContactMessage, attachment.message_id)
    if message is None:
        raise ApiError("attachment_not_found", "附件不存在", status_code=404)
    root = _contact_thread_root(db, message.id)
    if context.account.role != "admin" and root.sender_account_id != context.account.id:
        raise ApiError("attachment_not_found", "附件不存在", status_code=404)
    encoded_name = quote(attachment.file_name, safe="")
    return Response(
        content=attachment.content,
        media_type=attachment.content_type,
        headers={
            "Content-Disposition": (
                f"attachment; filename=\"download\"; filename*=UTF-8''{encoded_name}"
            ),
            "Content-Length": str(attachment.size),
        },
    )


@router.post("/announcements/{announcement_id}/messages", status_code=201)
def contact_administrator(
    announcement_id: uuid.UUID,
    payload: AdminContactMessageCreate,
    context: BusinessContext,
    db: Db,
) -> dict:
    if context.account.role != "user":
        raise ApiError(
            "user_message_only",
            "仅普通用户可以通过公告联系管理员",
            status_code=403,
        )
    announcement = _visible_announcement(db, announcement_id, context.account)
    row = AdminContactMessage(
        sender_account_id=context.account.id,
        announcement_id=announcement.id,
        message=payload.message,
        status="open",
        read_by_user_at=utcnow(),
    )
    db.add(row)
    db.flush()
    row.thread_id = row.id
    _add_contact_attachments(db, row.id, payload.attachments)
    db.commit()
    return _contact_conversation_view(db, row)


@router.get("/messages")
def list_contact_conversations(
    context: BusinessContext,
    db: Db,
    announcement_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    mark_read: bool = Query(default=True),
) -> dict:
    if context.account.role != "user":
        raise ApiError("user_message_only", "仅普通用户可以查看消息会话", status_code=403)
    statement = (
        select(AdminContactMessage)
        .where(
            AdminContactMessage.sender_account_id == context.account.id,
            or_(
                AdminContactMessage.thread_id.is_(None),
                AdminContactMessage.thread_id == AdminContactMessage.id,
            ),
        )
        .order_by(AdminContactMessage.created_at.desc())
        .limit(limit)
    )
    if announcement_id is not None:
        statement = statement.where(AdminContactMessage.announcement_id == announcement_id)
    roots = list(db.scalars(statement).all())
    now = utcnow()
    changed = False
    conversations = []
    for root in roots:
        rows = _contact_thread_rows(db, root)
        accounts = db.scalars(
            select(Account).where(
                Account.id.in_({row.sender_account_id for row in rows})
            )
        ).all()
        roles = {account.id: account.role for account in accounts}
        for row in rows:
            if (
                mark_read
                and roles.get(row.sender_account_id) == "admin"
                and row.read_by_user_at is None
            ):
                row.read_by_user_at = now
                changed = True
        conversations.append(_contact_conversation_view(db, root))
    if changed:
        db.commit()
    unread_count = _user_unread_contact_count(db, context.account.id)
    return {"items": conversations, "unread_count": unread_count}


@router.get("/messages/unread-count")
def contact_conversation_unread_count(
    context: BusinessContext,
    db: Db,
) -> dict:
    if context.account.role != "user":
        raise ApiError("user_message_only", "仅普通用户可以查看消息状态", status_code=403)
    return {"unread_count": _user_unread_contact_count(db, context.account.id)}


@router.post("/messages/{message_id}/read")
def mark_contact_conversation_read(
    message_id: uuid.UUID,
    context: BusinessContext,
    db: Db,
) -> dict:
    if context.account.role != "user":
        raise ApiError("user_message_only", "仅普通用户可以更新消息状态", status_code=403)
    root = _contact_thread_root(db, message_id)
    if root.sender_account_id != context.account.id:
        raise ApiError("message_not_found", "消息会话不存在", status_code=404)
    rows = _contact_thread_rows(db, root)
    accounts = db.scalars(
        select(Account).where(Account.id.in_({row.sender_account_id for row in rows}))
    ).all()
    roles = {account.id: account.role for account in accounts}
    now = utcnow()
    changed = False
    for row in rows:
        if roles.get(row.sender_account_id) == "admin" and row.read_by_user_at is None:
            row.read_by_user_at = now
            changed = True
    if changed:
        db.commit()
    return {"id": root.id, "read_at": now}


@router.post("/messages/{message_id}/replies", status_code=201)
def reply_to_administrator(
    message_id: uuid.UUID,
    payload: AdminContactMessageCreate,
    context: BusinessContext,
    db: Db,
) -> dict:
    if context.account.role != "user":
        raise ApiError("user_message_only", "仅普通用户可以回复消息", status_code=403)
    root = _contact_thread_root(db, message_id)
    if root.sender_account_id != context.account.id:
        raise ApiError("message_not_found", "消息会话不存在", status_code=404)
    row = AdminContactMessage(
        sender_account_id=context.account.id,
        announcement_id=root.announcement_id,
        thread_id=root.id,
        status="open",
        message=payload.message,
        read_by_user_at=utcnow(),
    )
    root.status = "open"
    db.add(row)
    db.flush()
    _add_contact_attachments(db, row.id, payload.attachments)
    db.commit()
    return _contact_conversation_view(db, root)


@router.post("/messages/{message_id}/status")
def update_contact_conversation_status(
    message_id: uuid.UUID,
    payload: ContactConversationStatusUpdate,
    context: BusinessContext,
    db: Db,
) -> dict:
    if context.account.role != "user":
        raise ApiError("user_message_only", "仅普通用户可以更新会话状态", status_code=403)
    root = _contact_thread_root(db, message_id)
    if root.sender_account_id != context.account.id:
        raise ApiError("message_not_found", "消息会话不存在", status_code=404)
    root.status = payload.status
    db.commit()
    return _contact_conversation_view(db, root)


@router.get("/admin/announcements")
def admin_list_announcements(
    context: AdminContext,
    db: Db,
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict]:
    rows = db.scalars(
        select(Announcement).order_by(Announcement.created_at.desc()).limit(limit)
    ).all()
    return [_announcement_view(db, row, account_id=context.account.id) for row in rows]


@router.post("/admin/announcements", status_code=201)
def admin_create_announcement(
    payload: AnnouncementCreate,
    request: Request,
    context: AdminContext,
    db: Db,
) -> dict:
    target_ids = _validated_target_ids(db, payload.target_account_ids)
    decoded_attachments = [_new_attachment(uuid.UUID(int=0), item) for item in payload.attachments]
    if sum(item.size for item in decoded_attachments) > MAX_ANNOUNCEMENT_ATTACHMENT_BYTES:
        raise ApiError(
            "announcement_attachments_too_large",
            "单个公告的附件总大小不能超过 25 MB",
            status_code=413,
        )
    announcement = Announcement(
        created_by_id=context.account.id,
        title=payload.title,
        ticker_text=payload.ticker_text,
        body_html=payload.body_html,
        show_on_startup=payload.show_on_startup,
        is_active=True,
    )
    db.add(announcement)
    db.flush()
    for account_id in target_ids:
        db.add(
            AnnouncementTarget(
                announcement_id=announcement.id,
                account_id=account_id,
            )
        )
    for attachment in decoded_attachments:
        attachment.announcement_id = announcement.id
        db.add(attachment)
    audit(
        db,
        request,
        actor_id=context.account.id,
        action="announcement.create",
        target_type="announcement",
        target_id=str(announcement.id),
        details={
            "title": announcement.title,
            "target_count": len(target_ids),
            "attachment_count": len(decoded_attachments),
            "show_on_startup": announcement.show_on_startup,
        },
    )
    db.commit()
    return _announcement_view(db, announcement, account_id=context.account.id)


@router.patch("/admin/announcements/{announcement_id}")
def admin_update_announcement(
    announcement_id: uuid.UUID,
    payload: AnnouncementUpdate,
    request: Request,
    context: AdminContext,
    db: Db,
) -> dict:
    announcement = db.get(Announcement, announcement_id)
    if not announcement:
        raise ApiError("announcement_not_found", "公告不存在", status_code=404)
    changes = payload.model_dump(exclude_unset=True)
    target_ids = None
    if "target_account_ids" in changes:
        target_ids = _validated_target_ids(db, changes.pop("target_account_ids"))
    for name, value in changes.items():
        setattr(announcement, name, value)
    if target_ids is not None:
        db.execute(
            delete(AnnouncementTarget).where(AnnouncementTarget.announcement_id == announcement.id)
        )
        for account_id in target_ids:
            db.add(
                AnnouncementTarget(
                    announcement_id=announcement.id,
                    account_id=account_id,
                )
            )
    announcement.revision += 1
    announcement.updated_at = utcnow()
    _clear_receipts(db, announcement.id)
    audit(
        db,
        request,
        actor_id=context.account.id,
        action="announcement.update",
        target_type="announcement",
        target_id=str(announcement.id),
        details={"fields": sorted(payload.model_fields_set)},
    )
    db.commit()
    return _announcement_view(db, announcement, account_id=context.account.id)


@router.delete("/admin/announcements/{announcement_id}", status_code=204)
def admin_delete_announcement(
    announcement_id: uuid.UUID,
    request: Request,
    context: AdminContext,
    db: Db,
) -> Response:
    announcement = db.get(Announcement, announcement_id)
    if not announcement:
        raise ApiError("announcement_not_found", "公告不存在", status_code=404)
    db.execute(
        update(AdminContactMessage)
        .where(AdminContactMessage.announcement_id == announcement.id)
        .values(announcement_id=None)
    )
    for model in (
        AnnouncementReceipt,
        AnnouncementTarget,
        AnnouncementAttachment,
    ):
        db.execute(delete(model).where(model.announcement_id == announcement.id))
    db.delete(announcement)
    audit(
        db,
        request,
        actor_id=context.account.id,
        action="announcement.delete",
        target_type="announcement",
        target_id=str(announcement.id),
        details={"title": announcement.title},
    )
    db.commit()
    return Response(status_code=204)


@router.post(
    "/admin/announcements/{announcement_id}/attachments",
    status_code=201,
)
def admin_add_announcement_attachment(
    announcement_id: uuid.UUID,
    payload: AnnouncementAttachmentInput,
    request: Request,
    context: AdminContext,
    db: Db,
) -> dict:
    announcement = db.get(Announcement, announcement_id)
    if not announcement:
        raise ApiError("announcement_not_found", "公告不存在", status_code=404)
    attachment = _new_attachment(announcement.id, payload)
    attachment_count = int(
        db.scalar(
            select(func.count(AnnouncementAttachment.id)).where(
                AnnouncementAttachment.announcement_id == announcement.id
            )
        )
        or 0
    )
    if attachment_count >= 8:
        raise ApiError(
            "too_many_attachments",
            "单个公告最多上传 8 个附件",
            status_code=422,
        )
    current_total = int(
        db.scalar(
            select(func.sum(AnnouncementAttachment.size)).where(
                AnnouncementAttachment.announcement_id == announcement.id
            )
        )
        or 0
    )
    if current_total + attachment.size > MAX_ANNOUNCEMENT_ATTACHMENT_BYTES:
        raise ApiError(
            "announcement_attachments_too_large",
            "单个公告的附件总大小不能超过 25 MB",
            status_code=413,
        )
    db.add(attachment)
    announcement.revision += 1
    announcement.updated_at = utcnow()
    _clear_receipts(db, announcement.id)
    audit(
        db,
        request,
        actor_id=context.account.id,
        action="announcement.attachment.add",
        target_type="announcement",
        target_id=str(announcement.id),
        details={"file_name": attachment.file_name, "size": attachment.size},
    )
    db.commit()
    return _attachment_view(attachment)


@router.delete(
    "/admin/announcements/{announcement_id}/attachments/{attachment_id}",
    status_code=204,
)
def admin_delete_announcement_attachment(
    announcement_id: uuid.UUID,
    attachment_id: uuid.UUID,
    request: Request,
    context: AdminContext,
    db: Db,
) -> Response:
    announcement = db.get(Announcement, announcement_id)
    attachment = db.get(AnnouncementAttachment, attachment_id)
    if announcement is None or attachment is None or attachment.announcement_id != announcement.id:
        raise ApiError("attachment_not_found", "附件不存在", status_code=404)
    db.delete(attachment)
    announcement.revision += 1
    announcement.updated_at = utcnow()
    _clear_receipts(db, announcement.id)
    audit(
        db,
        request,
        actor_id=context.account.id,
        action="announcement.attachment.delete",
        target_type="announcement",
        target_id=str(announcement.id),
        details={"file_name": attachment.file_name},
    )
    db.commit()
    return Response(status_code=204)


@router.get("/admin/messages")
def admin_list_contact_messages(
    context: AdminContext,
    db: Db,
    unread_only: bool = False,
    limit: int = Query(default=200, ge=1, le=500),
) -> dict:
    statement = (
        select(AdminContactMessage)
        .where(
            or_(
                AdminContactMessage.thread_id.is_(None),
                AdminContactMessage.thread_id == AdminContactMessage.id,
            )
        )
        .order_by(AdminContactMessage.created_at.desc())
        .limit(limit)
    )
    if unread_only:
        unread_threads = select(AdminContactMessage.thread_id).where(
            AdminContactMessage.read_at.is_(None)
        )
        statement = statement.where(
            or_(
                AdminContactMessage.read_at.is_(None),
                AdminContactMessage.id.in_(unread_threads),
            )
        )
    roots = db.scalars(statement).all()
    unread_count = int(
        db.scalar(
            select(func.count(AdminContactMessage.id)).where(AdminContactMessage.read_at.is_(None))
        )
        or 0
    )
    return {
        "items": [_contact_conversation_view(db, root) for root in roots],
        "unread_count": unread_count,
    }


@router.post("/admin/messages/{message_id}/read")
def admin_mark_contact_message_read(
    message_id: uuid.UUID,
    context: AdminContext,
    db: Db,
) -> dict:
    root = _contact_thread_root(db, message_id)
    rows = _contact_thread_rows(db, root)
    accounts = db.scalars(
        select(Account).where(Account.id.in_({row.sender_account_id for row in rows}))
    ).all()
    roles = {account.id: account.role for account in accounts}
    now = utcnow()
    changed = False
    for row in rows:
        if roles.get(row.sender_account_id) == "user" and row.read_at is None:
            row.read_at = now
            changed = True
    if changed:
        db.commit()
    return {"id": root.id, "read_at": now}


@router.post("/admin/messages/{message_id}/reply", status_code=201)
async def admin_reply_contact_message(
    message_id: uuid.UUID,
    payload: AdminContactMessageCreate,
    context: AdminContext,
    db: Db,
) -> dict:
    root = _contact_thread_root(db, message_id)
    row = AdminContactMessage(
        sender_account_id=context.account.id,
        announcement_id=root.announcement_id,
        thread_id=root.id,
        status="open",
        message=payload.message,
        read_at=utcnow(),
    )
    root.status = "open"
    db.add(row)
    db.flush()
    _add_contact_attachments(db, row.id, payload.attachments)
    db.commit()
    await update_hub.broadcast_event("contact_messages_changed")
    return _contact_conversation_view(db, root)


@router.delete("/admin/messages/{message_id}", status_code=204)
def admin_delete_contact_message(
    message_id: uuid.UUID,
    request: Request,
    context: AdminContext,
    db: Db,
) -> Response:
    root = _contact_thread_root(db, message_id)
    audit(
        db,
        request,
        actor_id=context.account.id,
        action="message.delete",
        target_type="admin_contact_message",
        target_id=str(root.id),
        details={
            "sender_account_id": str(root.sender_account_id),
            "announcement_id": (
                str(root.announcement_id) if root.announcement_id else None
            ),
        },
    )
    for row in _contact_thread_rows(db, root):
        db.delete(row)
    db.commit()
    return Response(status_code=204)

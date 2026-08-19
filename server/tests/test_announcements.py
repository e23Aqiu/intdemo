from __future__ import annotations

import base64

from .conftest import auth_header, changed_admin, login


def _create_user(client, admin_bundle, username):
    response = client.post(
        "/api/v1/admin/accounts",
        headers=auth_header(admin_bundle),
        json={
            "username": username,
            "display_name": f"{username}站点",
            "role": "user",
            "stats_scope": "own",
            "device_limit": 10000,
            "is_active": True,
        },
    )
    assert response.status_code == 201, response.text
    account = response.json()
    initial = login(client, username, "123456", device=100 + len(username))
    changed = client.post(
        "/api/v1/auth/change-password",
        headers=auth_header(initial),
        json={
            "current_password": "123456",
            "new_password": f"{username.title()}!23456",
        },
    )
    assert changed.status_code == 200, changed.text
    return account, changed.json()


def _announcement_payload(**overrides):
    payload = {
        "title": "系统维护通知",
        "ticker_text": "今晚 22:00 进行系统维护",
        "body_html": ('<p><b>维护时间：</b><span style="color:#d33f49">今晚 22:00</span></p>'),
        "show_on_startup": True,
        "target_account_ids": [],
        "attachments": [
            {
                "file_name": "维护说明.png",
                "content_type": "image/png",
                "kind": "image",
                "content_base64": base64.b64encode(b"fake-png-content").decode(),
            }
        ],
    }
    payload.update(overrides)
    return payload


def test_global_announcement_attachment_and_startup_receipt(client):
    admin = changed_admin(client)
    _, station = _create_user(client, admin, "stationa")

    created = client.post(
        "/api/v1/admin/announcements",
        headers=auth_header(admin),
        json=_announcement_payload(),
    )
    assert created.status_code == 201, created.text
    announcement = created.json()
    assert announcement["show_on_startup"] is True
    assert announcement["target_account_ids"] == []
    assert announcement["attachments"][0]["kind"] == "image"

    visible = client.get(
        "/api/v1/announcements",
        headers=auth_header(station),
    )
    assert visible.status_code == 200, visible.text
    row = visible.json()[0]
    assert row["id"] == announcement["id"]
    assert row["startup_pending"] is True
    assert row["read_at"] is None

    displayed = client.post(
        f"/api/v1/announcements/{announcement['id']}/read",
        headers=auth_header(station),
        json={"startup_shown": True, "confirmed": False},
    )
    assert displayed.status_code == 200, displayed.text
    assert displayed.json()["startup_shown_at"] is not None
    assert displayed.json()["read_at"] is None

    visible_again = client.get(
        "/api/v1/announcements",
        headers=auth_header(station),
    ).json()[0]
    assert visible_again["startup_pending"] is False
    assert visible_again["read_at"] is None

    read = client.post(
        f"/api/v1/announcements/{announcement['id']}/read",
        headers=auth_header(station),
        json={"startup_shown": False, "confirmed": True},
    )
    assert read.status_code == 200, read.text
    assert read.json()["read_at"] is not None
    managed_receipts = client.get(
        "/api/v1/admin/announcements",
        headers=auth_header(admin),
    ).json()[0]
    assert managed_receipts["read_count"] == 1
    assert managed_receipts["unread_count"] >= 1
    assert managed_receipts["read_users"][0]["username"] == "stationa"
    assert "stationa" not in {
        item["username"] for item in managed_receipts["unread_users"]
    }

    updated = client.patch(
        f"/api/v1/admin/announcements/{announcement['id']}",
        headers=auth_header(admin),
        json={"ticker_text": "维护时间调整为今晚 22:30"},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["revision"] == 2
    visible_after_update = client.get(
        "/api/v1/announcements",
        headers=auth_header(station),
    ).json()[0]
    assert visible_after_update["startup_pending"] is True
    assert visible_after_update["read_at"] is None

    attachment_id = announcement["attachments"][0]["id"]
    downloaded = client.get(
        f"/api/v1/announcements/attachments/{attachment_id}",
        headers=auth_header(station),
    )
    assert downloaded.status_code == 200
    assert downloaded.content == b"fake-png-content"
    assert downloaded.headers["content-type"] == "image/png"
    assert "filename*=UTF-8''" in downloaded.headers["content-disposition"]

    added = client.post(
        f"/api/v1/admin/announcements/{announcement['id']}/attachments",
        headers=auth_header(admin),
        json={
            "file_name": "维护安排.txt",
            "content_type": "text/plain",
            "kind": "file",
            "content_base64": base64.b64encode(b"maintenance").decode(),
        },
    )
    assert added.status_code == 201, added.text
    added_id = added.json()["id"]
    assert (
        client.delete(
            (f"/api/v1/admin/announcements/{announcement['id']}/attachments/{added_id}"),
            headers=auth_header(admin),
        ).status_code
        == 204
    )


def test_targeted_visibility_updates_and_admin_management(client):
    admin = changed_admin(client)
    first_account, first = _create_user(client, admin, "first")
    _, second = _create_user(client, admin, "second")

    created = client.post(
        "/api/v1/admin/announcements",
        headers=auth_header(admin),
        json=_announcement_payload(
            title="定向公告",
            ticker_text="仅 first 可见",
            target_account_ids=[first_account["id"]],
            attachments=[],
        ),
    )
    assert created.status_code == 201, created.text
    announcement = created.json()

    first_rows = client.get(
        "/api/v1/announcements",
        headers=auth_header(first),
    ).json()
    second_rows = client.get(
        "/api/v1/announcements",
        headers=auth_header(second),
    ).json()
    admin_ticker_rows = client.get(
        "/api/v1/announcements",
        headers=auth_header(admin),
    ).json()
    assert [row["id"] for row in first_rows] == [announcement["id"]]
    assert second_rows == []
    assert admin_ticker_rows == []

    managed = client.get(
        "/api/v1/admin/announcements",
        headers=auth_header(admin),
    )
    assert managed.status_code == 200
    assert managed.json()[0]["targets"][0]["username"] == "first"
    assert managed.json()[0]["read_count"] == 0
    assert managed.json()[0]["unread_count"] == 1

    assert client.post(
        f"/api/v1/announcements/{announcement['id']}/read",
        headers=auth_header(first),
        json={"startup_shown": False, "confirmed": True},
    ).status_code == 200
    managed_after_read = client.get(
        "/api/v1/admin/announcements",
        headers=auth_header(admin),
    ).json()[0]
    assert managed_after_read["read_count"] == 1
    assert managed_after_read["unread_count"] == 0

    denied = client.get(
        "/api/v1/admin/announcements",
        headers=auth_header(first),
    )
    assert denied.status_code == 403
    assert denied.json()["code"] == "admin_required"

    updated = client.patch(
        f"/api/v1/admin/announcements/{announcement['id']}",
        headers=auth_header(admin),
        json={
            "ticker_text": "公告已更新",
            "is_active": False,
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["revision"] == 2
    assert updated.json()["is_active"] is False
    assert (
        client.get(
            "/api/v1/announcements",
            headers=auth_header(first),
        ).json()
        == []
    )

    deleted = client.delete(
        f"/api/v1/admin/announcements/{announcement['id']}",
        headers=auth_header(admin),
    )
    assert deleted.status_code == 204
    assert (
        client.get(
            "/api/v1/admin/announcements",
            headers=auth_header(admin),
        ).json()
        == []
    )


def test_v110_announcement_and_text_message_payloads_remain_supported(client):
    admin = changed_admin(client)
    account, station = _create_user(client, admin, "legacyclient")
    announcement = client.post(
        "/api/v1/admin/announcements",
        headers=auth_header(admin),
        json=_announcement_payload(
            title="兼容公告",
            ticker_text="旧客户端兼容验证",
            target_account_ids=[account["id"]],
            attachments=[],
        ),
    ).json()

    read = client.post(
        f"/api/v1/announcements/{announcement['id']}/read",
        headers=auth_header(station),
        json={"startup_shown": True},
    )
    assert read.status_code == 200, read.text
    assert read.json()["read_at"] is not None

    sent = client.post(
        f"/api/v1/announcements/{announcement['id']}/messages",
        headers=auth_header(station),
        json={"message": "旧版客户端纯文字反馈"},
    )
    assert sent.status_code == 201, sent.text
    assert sent.json()["message"] == "旧版客户端纯文字反馈"
    assert sent.json()["messages"][0]["attachments"] == []


def test_user_text_messages_are_visible_and_readable_by_admin(client):
    admin = changed_admin(client)
    first_account, first = _create_user(client, admin, "sender")
    _, second = _create_user(client, admin, "outsider")
    announcement = client.post(
        "/api/v1/admin/announcements",
        headers=auth_header(admin),
        json=_announcement_payload(
            title="需要反馈",
            ticker_text="请 sender 查看",
            target_account_ids=[first_account["id"]],
            attachments=[],
        ),
    ).json()

    sent = client.post(
        f"/api/v1/announcements/{announcement['id']}/messages",
        headers=auth_header(first),
        json={
            "message": "  我已收到，请问维护期间能否继续录入？  ",
            "attachments": [
                {
                    "file_name": "现场.png",
                    "content_type": "image/png",
                    "kind": "image",
                    "content_base64": base64.b64encode(b"message-image").decode(),
                }
            ],
        },
    )
    assert sent.status_code == 201, sent.text
    assert sent.json()["message"] == "我已收到，请问维护期间能否继续录入？"
    sent_attachment = sent.json()["messages"][0]["attachments"][0]
    assert sent_attachment["file_name"] == "现场.png"
    downloaded = client.get(
        f"/api/v1/messages/attachments/{sent_attachment['id']}",
        headers=auth_header(first),
    )
    assert downloaded.status_code == 200
    assert downloaded.content == b"message-image"
    assert client.get(
        f"/api/v1/messages/attachments/{sent_attachment['id']}",
        headers=auth_header(second),
    ).status_code == 404

    outsider = client.post(
        f"/api/v1/announcements/{announcement['id']}/messages",
        headers=auth_header(second),
        json={"message": "我不应该能联系"},
    )
    assert outsider.status_code == 404

    admin_send = client.post(
        f"/api/v1/announcements/{announcement['id']}/messages",
        headers=auth_header(admin),
        json={"message": "管理员不能从这里发送"},
    )
    assert admin_send.status_code == 403
    assert admin_send.json()["code"] == "user_message_only"

    inbox = client.get(
        "/api/v1/admin/messages",
        headers=auth_header(admin),
    )
    assert inbox.status_code == 200
    body = inbox.json()
    assert body["unread_count"] == 1
    assert body["items"][0]["sender_username"] == "sender"
    assert body["items"][0]["announcement_title"] == "需要反馈"
    assert body["items"][0]["message"] == "我已收到，请问维护期间能否继续录入？"

    marked = client.post(
        f"/api/v1/admin/messages/{body['items'][0]['id']}/read",
        headers=auth_header(admin),
    )
    assert marked.status_code == 200
    assert marked.json()["read_at"] is not None
    assert client.get(
        "/api/v1/admin/messages?unread_only=true",
        headers=auth_header(admin),
    ).json() == {"items": [], "unread_count": 0}

    replied = client.post(
        f"/api/v1/admin/messages/{body['items'][0]['id']}/reply",
        headers=auth_header(admin),
        json={
            "message": "可以继续录入，维护期间同步会自动重试。",
            "attachments": [
                {
                    "file_name": "说明.txt",
                    "content_type": "text/plain",
                    "kind": "file",
                    "content_base64": base64.b64encode(b"retry help").decode(),
                }
            ],
        },
    )
    assert replied.status_code == 201, replied.text
    assert replied.json()["messages"][-1]["sender_role"] == "admin"
    admin_attachment = replied.json()["messages"][-1]["attachments"][0]
    assert client.get(
        f"/api/v1/messages/attachments/{admin_attachment['id']}",
        headers=auth_header(admin),
    ).content == b"retry help"

    polled = client.get(
        f"/api/v1/messages?announcement_id={announcement['id']}&mark_read=false",
        headers=auth_header(first),
    )
    assert polled.status_code == 200, polled.text
    assert polled.json()["unread_count"] == 1
    assert polled.json()["items"][0]["user_unread_count"] == 1
    assert client.get(
        "/api/v1/messages/unread-count",
        headers=auth_header(first),
    ).json() == {"unread_count": 1}

    marked_for_user = client.post(
        f"/api/v1/messages/{body['items'][0]['id']}/read",
        headers=auth_header(first),
    )
    assert marked_for_user.status_code == 200, marked_for_user.text
    after_explicit_read = client.get(
        f"/api/v1/messages?announcement_id={announcement['id']}&mark_read=false",
        headers=auth_header(first),
    ).json()
    assert after_explicit_read["unread_count"] == 0
    assert after_explicit_read["items"][0]["user_unread_count"] == 0
    assert client.get(
        "/api/v1/messages/unread-count",
        headers=auth_header(first),
    ).json() == {"unread_count": 0}

    second_reply = client.post(
        f"/api/v1/admin/messages/{body['items'][0]['id']}/reply",
        headers=auth_header(admin),
        json={"message": "再补充一条提醒。"},
    )
    assert second_reply.status_code == 201, second_reply.text

    user_conversations = client.get(
        f"/api/v1/messages?announcement_id={announcement['id']}",
        headers=auth_header(first),
    )
    assert user_conversations.status_code == 200, user_conversations.text
    conversation = user_conversations.json()["items"][0]
    assert conversation["messages"][-1]["message"] == "再补充一条提醒。"
    assert client.get(
        f"/api/v1/messages?announcement_id={announcement['id']}&mark_read=false",
        headers=auth_header(first),
    ).json()["unread_count"] == 0

    continued = client.post(
        f"/api/v1/messages/{conversation['id']}/replies",
        headers=auth_header(first),
        json={"message": "问题仍未解决，请继续协助。"},
    )
    assert continued.status_code == 201, continued.text
    assert continued.json()["status"] == "open"
    assert client.get(
        "/api/v1/admin/messages",
        headers=auth_header(admin),
    ).json()["unread_count"] == 1

    attachment_only = client.post(
        f"/api/v1/messages/{conversation['id']}/replies",
        headers=auth_header(first),
        json={
            "message": "",
            "attachments": [
                {
                    "file_name": "补充材料.pdf",
                    "content_type": "application/pdf",
                    "kind": "file",
                    "content_base64": base64.b64encode(b"pdf-content").decode(),
                }
            ],
        },
    )
    assert attachment_only.status_code == 201, attachment_only.text
    assert attachment_only.json()["messages"][-1]["message"] == ""
    assert (
        attachment_only.json()["messages"][-1]["attachments"][0]["file_name"]
        == "补充材料.pdf"
    )

    resolved = client.post(
        f"/api/v1/messages/{conversation['id']}/status",
        headers=auth_header(first),
        json={"status": "resolved"},
    )
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["status"] == "resolved"

    deleted = client.delete(
        f"/api/v1/admin/messages/{body['items'][0]['id']}",
        headers=auth_header(admin),
    )
    assert deleted.status_code == 204
    assert client.get(
        "/api/v1/admin/messages",
        headers=auth_header(admin),
    ).json() == {"items": [], "unread_count": 0}

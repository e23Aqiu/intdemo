import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import (
    QBuffer,
    QByteArray,
    QCoreApplication,
    QEvent,
    QIODevice,
    QPoint,
    Qt,
)
from PyQt5.QtGui import QPixmap
from PyQt5.QtWidgets import (
    QApplication,
    QFrame,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QPushButton,
)

from integrated_client.config import DEFAULT_ADMIN_PASSWORD, DEFAULT_ADMIN_USERNAME
from integrated_client.database import Database
from integrated_client.online.api import ApiResponseError, NetworkUnavailable
from integrated_client.ui.announcement_page import (
    AdminConversationDialog,
    AnnouncementAdminPage,
    AnnouncementDetailDialog,
    AnnouncementEditorDialog,
    AnnouncementListDialog,
    AnnouncementTickerButton,
    ContactAdminDialog,
    ContactAttachmentPicker,
    ContactConversationDialog,
    ContactHistoryDialog,
    ConversationTimeline,
    ImagePreviewDialog,
)
from integrated_client.ui.main_window import MainWindow


class FakeAnnouncementApi:
    def __init__(self):
        self.read_calls = []
        self.sent_messages = []
        self.admin_replies = []
        self.visible_announcements = []
        self.managed_announcements = []
        self.inbox = {"items": [], "unread_count": 0}
        self.accounts = []
        self.conversations = []
        self.user_read_calls = []

    def announcements(self, _token, limit=50):
        return list(self.visible_announcements[:limit])

    def mark_announcement_read(
        self,
        _token,
        announcement_id,
        *,
        startup_shown=False,
    ):
        self.read_calls.append((announcement_id, startup_shown))
        return {"announcement_id": announcement_id}

    @staticmethod
    def download_announcement_attachment(_token, _attachment_id):
        return b"attachment"

    def send_admin_message(
        self,
        _token,
        announcement_id,
        message,
        attachments=None,
    ):
        self.sent_messages.append((announcement_id, message))
        conversation = {
            "id": "message-1",
            "announcement_id": announcement_id,
            "announcement_title": "公告",
            "status": "open",
            "messages": [
                {
                    "sender_role": "user",
                    "message": message,
                    "attachments": list(attachments or []),
                    "created_at": "2026-08-19T10:00:00",
                }
            ],
        }
        return conversation

    def contact_conversations(
        self,
        _token,
        *,
        announcement_id=None,
        limit=50,
        mark_read=True,
    ):
        items = list(self.conversations)
        if announcement_id:
            items = [
                item
                for item in items
                if str(item.get("announcement_id")) == str(announcement_id)
            ]
        return {
            "items": items[:limit],
            "unread_count": sum(
                int(item.get("user_unread_count") or 0) for item in items
            ),
        }

    def mark_contact_conversation_read(self, _token, message_id):
        self.user_read_calls.append(message_id)
        for conversation in self.conversations:
            if str(conversation.get("id")) == str(message_id):
                conversation["user_unread_count"] = 0
        return {"id": message_id, "read_at": "2026-08-19T10:10:00"}

    def contact_conversation_unread_count(self, _token):
        return sum(
            int(item.get("user_unread_count") or 0)
            for item in self.conversations
        )

    @staticmethod
    def reply_admin_message(
        _token,
        message_id,
        message,
        attachments=None,
    ):
        return {
            "id": message_id,
            "announcement_id": "announcement-1",
            "announcement_title": "系统维护公告",
            "status": "open",
            "messages": [
                {
                    "sender_role": "user",
                    "message": message,
                    "attachments": list(attachments or []),
                    "created_at": "2026-08-19T10:05:00",
                }
            ],
        }

    @staticmethod
    def update_contact_status(_token, message_id, status):
        return {
            "id": message_id,
            "announcement_id": "announcement-1",
            "announcement_title": "系统维护公告",
            "status": status,
            "messages": [],
        }

    def admin_announcements(self, _token, limit=100):
        return list(self.managed_announcements[:limit])

    @staticmethod
    def admin_create_announcement(_token, payload):
        return dict(payload, id="created")

    @staticmethod
    def admin_update_announcement(_token, announcement_id, payload):
        return dict(payload, id=announcement_id)

    @staticmethod
    def admin_delete_announcement(_token, _announcement_id):
        return None

    @staticmethod
    def admin_add_announcement_attachment(
        _token,
        _announcement_id,
        payload,
    ):
        return dict(payload, id="attachment")

    @staticmethod
    def admin_delete_announcement_attachment(
        _token,
        _announcement_id,
        _attachment_id,
    ):
        return None

    def admin_messages(self, _token, *, unread_only=False, limit=200):
        if not unread_only:
            return {
                "items": list(self.inbox["items"][:limit]),
                "unread_count": self.inbox["unread_count"],
            }
        return {
            "items": [item for item in self.inbox["items"] if not item.get("read_at")][
                :limit
            ],
            "unread_count": self.inbox["unread_count"],
        }

    @staticmethod
    def admin_mark_message_read(_token, message_id):
        return {"id": message_id, "read_at": "2026-07-28T12:00:00"}

    def admin_delete_message(self, _token, message_id):
        self.inbox["items"] = [
            item
            for item in self.inbox["items"]
            if str(item.get("id")) != str(message_id)
        ]
        self.inbox["unread_count"] = sum(
            not item.get("read_at") for item in self.inbox["items"]
        )

    def admin_reply_message(self, _token, message_id, message, attachments=None):
        self.admin_replies.append((message_id, message, list(attachments or [])))
        return {"id": message_id}

    def admin_accounts(self, _token):
        return list(self.accounts)


class FakeSession:
    def __init__(self, api):
        self.api = api

    @staticmethod
    def access_token():
        return "test-token"


class AnnouncementUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "test.db")
        self.database.ensure_default_admin()
        self.admin = self.database.authenticate(
            DEFAULT_ADMIN_USERNAME,
            DEFAULT_ADMIN_PASSWORD,
        )

    def tearDown(self):
        for widget in list(self.app.topLevelWidgets()):
            workflow = getattr(widget, "workflow_page", None)
            if workflow is not None:
                workflow.shutdown()
            if hasattr(widget, "_prepared_to_close"):
                widget._prepared_to_close = True
            widget.close()
            widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.app.processEvents()
        self.temp_dir.cleanup()

    def _wait_until(self, predicate, timeout=3):
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.app.processEvents()
            if predicate():
                return True
            time.sleep(0.01)
        self.app.processEvents()
        return bool(predicate())

    @staticmethod
    def _announcement(**overrides):
        result = {
            "id": "announcement-1",
            "title": "系统维护公告",
            "ticker_text": "今晚 22:00 维护，请提前保存数据",
            "body_html": "<p><b>维护时间：</b>22:00—23:00</p>",
            "show_on_startup": True,
            "startup_pending": False,
            "is_active": True,
            "revision": 1,
            "created_by_name": "系统管理员",
            "attachments": [],
            "target_account_ids": [],
            "targets": [],
            "read_count": 2,
            "unread_count": 3,
            "read_users": [
                {"username": "reader", "display_name": "已读用户"},
            ],
            "unread_users": [
                {"username": "pending", "display_name": "未读用户"},
            ],
            "read_at": None,
            "created_at": "2026-07-28T10:00:00",
            "updated_at": "2026-07-28T10:00:00",
        }
        result.update(overrides)
        return result

    def test_admin_ticker_hides_announcement_unread_but_keeps_message_indicator(self):
        api = FakeAnnouncementApi()
        api.visible_announcements = [self._announcement()]
        api.inbox = {"items": [], "unread_count": 3}
        window = MainWindow(
            self.database,
            self.admin,
            session_manager=FakeSession(api),
        )
        window.show()
        self.assertTrue(
            self._wait_until(lambda: bool(window.announcements)),
            "announcement polling did not complete",
        )

        self.assertEqual(
            window.announcement_ticker_button.text(),
            "今晚 22:00 维护，请提前保存数据",
        )
        self.assertIn("announcements_admin", window._pages)
        self.assertEqual(
            window._nav_buttons["announcements_admin"].text(),
            "公告发布",
        )
        self.assertEqual(window._announcement_nav_badge.unread_count, 3)
        self.assertTrue(window._announcement_nav_badge_spacer.isVisible())
        self.assertEqual(
            window._announcement_nav_badge.text(),
            "有 3 条新消息",
        )
        self.assertTrue(window._announcement_nav_badge.isVisible())
        self.assertNotIn(
            "●",
            window._nav_buttons["announcements_admin"].text(),
        )
        self.assertEqual(
            window._nav_buttons["announcements_admin"].toolTip(),
            "收到 3 条未读用户消息",
        )
        self.assertFalse(window.announcement_horn_button.property("hasUnread"))
        self.assertEqual(
            window.announcement_horn_button.toolTip(),
            "点击查看全部公告",
        )
        window.announcement_ticker_button.enterEvent(QEvent(QEvent.Enter))
        self.app.processEvents()
        self.assertFalse(
            window.announcement_ticker_button._hover_card.state_label.isVisible()
        )
        window.announcement_horn_button.click()
        self.app.processEvents()
        announcement_list = window.announcement_list_dialog
        self.assertEqual(announcement_list.count_label.text(), "共 1 条")
        self.assertTrue(announcement_list.mark_read_button.isHidden())
        self.assertTrue(announcement_list.history_button.isHidden())
        self.assertNotIn("未读", announcement_list.announcement_list.item(0).text())
        self.assertEqual(announcement_list.preview_state.text(), "公告")

        window._show_current_announcement()
        self.assertIsNotNone(window.announcement_dialog)
        self.assertIsInstance(
            window.announcement_dialog,
            AnnouncementDetailDialog,
        )
        self.assertTrue(
            self._wait_until(lambda: bool(api.read_calls)),
            "read receipt was not sent",
        )
        self.assertEqual(api.read_calls[-1], ("announcement-1", False))

    def test_horn_opens_complete_announcement_list_and_selected_detail(self):
        api = FakeAnnouncementApi()
        api.visible_announcements = [
            self._announcement(),
            self._announcement(
                id="announcement-2",
                title="业务提醒",
                ticker_text="请及时完成今日数据处理",
                read_at="2026-07-28T12:00:00",
                created_at="2026-07-28T11:00:00",
            ),
        ]
        user = self.database.create_account(
            "horn_user",
            "HornUser@123",
            "user",
            self.admin.id,
            display_name="公告用户",
        )
        window = MainWindow(
            self.database,
            user,
            session_manager=FakeSession(api),
        )
        window.show()
        self.assertTrue(self._wait_until(lambda: len(window.announcements) == 2))

        window.announcement_horn_button.click()
        self.app.processEvents()
        dialog = window.announcement_list_dialog
        self.assertIsInstance(dialog, AnnouncementListDialog)
        self.assertTrue(dialog.isVisible())
        self.assertEqual(dialog.announcement_list.count(), 2)
        self.assertEqual(dialog.count_label.text(), "共 2 条 · 未读 1 条")
        self.assertEqual(dialog.preview_title.text(), "系统维护公告")
        self.assertTrue(dialog.mark_read_button.isEnabled())

        dialog.mark_read_button.click()
        self.app.processEvents()
        self.assertTrue(api.visible_announcements[0]["read_at"])
        self.assertEqual(dialog.count_label.text(), "共 2 条 · 未读 0 条")
        self.assertFalse(window.announcement_horn_button.property("hasUnread"))
        self.assertTrue(
            self._wait_until(
                lambda: ("announcement-1", False) in api.read_calls
            )
        )

        dialog.announcement_list.setCurrentRow(1)
        self.app.processEvents()
        self.assertEqual(dialog.preview_title.text(), "业务提醒")
        self.assertEqual(dialog.preview_state.text(), "已读公告")
        dialog._open_selected()
        self.assertTrue(
            self._wait_until(
                lambda: (
                    window.announcement_dialog is not None
                    and window.announcement_dialog.isVisible()
                )
            )
        )
        self.assertEqual(
            window.announcement_dialog.announcement["id"],
            "announcement-2",
        )

    def test_normal_user_horn_opens_all_conversation_history(self):
        api = FakeAnnouncementApi()
        api.visible_announcements = [self._announcement(read_at="2026-08-19T09:00:00")]
        api.conversations = [
            {
                "id": "conversation-1",
                "announcement_id": "announcement-1",
                "announcement_title": "系统维护公告",
                "status": "open",
                "updated_at": "2026-08-19T10:05:00",
                "last_message": {"message": "请继续协助处理"},
                "messages": [
                    {
                        "sender_role": "user",
                        "message": "请继续协助处理",
                        "attachments": [],
                        "created_at": "2026-08-19T10:05:00",
                    }
                ],
            },
            {
                "id": "conversation-2",
                "announcement_id": "announcement-2",
                "announcement_title": "业务提醒",
                "status": "resolved",
                "updated_at": "2026-08-18T15:30:00",
                "last_message": {"message": "已经解决"},
                "messages": [
                    {
                        "sender_role": "admin",
                        "message": "已经解决",
                        "attachments": [],
                        "created_at": "2026-08-18T15:30:00",
                    }
                ],
            },
        ]
        user = self.database.create_account(
            "history_user",
            "History@123",
            "user",
            self.admin.id,
            display_name="历史会话用户",
        )
        window = MainWindow(
            self.database,
            user,
            session_manager=FakeSession(api),
        )
        window.show()
        self.assertTrue(self._wait_until(lambda: bool(window.announcements)))

        window.announcement_horn_button.click()
        self.app.processEvents()
        announcement_list = window.announcement_list_dialog
        self.assertTrue(announcement_list.history_button.isVisible())
        with patch(
            "integrated_client.ui.announcement_page.run_with_loading",
            side_effect=lambda _parent, _message, function: function(),
        ):
            announcement_list.history_button.click()
        self.app.processEvents()

        history = window.contact_history_dialog
        self.assertIsInstance(history, ContactHistoryDialog)
        self.assertEqual(history.conversation_picker.count(), 2)
        self.assertEqual(history.conversation["id"], "conversation-1")
        self.assertIn("请继续协助处理", history.history.toPlainText())
        history.conversation_picker.setCurrentIndex(1)
        self.app.processEvents()
        self.assertEqual(history.conversation["id"], "conversation-2")
        self.assertIn("已经解决", history.history.toPlainText())

    def test_user_unread_badges_flow_from_horn_to_history_picker(self):
        api = FakeAnnouncementApi()
        api.visible_announcements = [self._announcement(read_at=True)]
        api.conversations = [
            {
                "id": "conversation-current",
                "announcement_id": "announcement-1",
                "announcement_title": "当前会话",
                "status": "open",
                "user_unread_count": 0,
                "updated_at": "2026-08-19T10:06:00",
                "last_message": {"message": "当前正在查看"},
                "messages": [],
            },
            {
                "id": "conversation-unread",
                "announcement_id": "announcement-1",
                "announcement_title": "系统维护公告",
                "status": "open",
                "user_unread_count": 2,
                "updated_at": "2026-08-19T10:05:00",
                "last_message": {"message": "请查看管理员回复"},
                "messages": [
                    {
                        "id": "admin-reply-unread",
                        "sender_role": "admin",
                        "message": "请查看管理员回复",
                        "created_at": "2026-08-19T10:05:00",
                        "attachments": [],
                    }
                ],
            }
        ]
        user = self.database.create_account(
            "history_badge_user",
            "HistoryBadge@123",
            "user",
            self.admin.id,
            display_name="历史气泡用户",
        )
        window = MainWindow(
            self.database,
            user,
            session_manager=FakeSession(api),
        )
        window.show()
        self.assertTrue(
            self._wait_until(
                lambda: window.announcement_horn_button.message_unread_count == 2
            )
        )
        self.assertEqual(window.announcement_horn_button.text(), "")
        self.assertEqual(window.announcement_horn_button.width(), 38)
        self.assertEqual(
            window.announcement_horn_button.unread_bubble.text(),
            "有 2 条新消息",
        )
        self.assertTrue(window.announcement_horn_button.unread_bubble.isVisible())

        window.announcement_horn_button.click()
        self.app.processEvents()
        announcement_list = window.announcement_list_dialog
        self.assertEqual(announcement_list.history_button.message_unread_count, 2)
        self.assertEqual(announcement_list.history_button.text(), "历史会话")
        self.assertEqual(
            announcement_list.history_button.unread_bubble.text(),
            "有 2 条新消息",
        )
        with patch(
            "integrated_client.ui.announcement_page.run_with_loading",
            side_effect=lambda _parent, _message, function: function(),
        ):
            announcement_list.history_button.click()
        self.app.processEvents()

        history = window.contact_history_dialog
        self.assertIn(
            "有 1 个未读会话 · 2 条新消息",
            history.conversation_picker.currentText(),
        )
        self.assertIn("有 2 条新消息", history.conversation_picker.itemText(1))
        self.assertNotIn("●", history.conversation_picker.itemText(1))
        history.conversation_picker.setCurrentIndex(1)
        self.assertTrue(
            self._wait_until(lambda: api.user_read_calls == ["conversation-unread"])
        )
        self.assertNotIn("新消息", history.conversation_picker.itemText(1))
        self.assertIn("发起：", history.conversation_picker.itemText(1))
        self.assertEqual(window.announcement_horn_button.message_unread_count, 0)
        self.assertEqual(window.announcement_horn_button.text(), "")
        self.assertEqual(window.announcement_horn_button.width(), 38)
        self.assertTrue(window.announcement_horn_button.unread_bubble.isHidden())

    def test_image_preview_supports_fit_zoom_and_actual_size(self):
        pixmap = QPixmap(1600, 1000)
        pixmap.fill(Qt.white)
        encoded = QByteArray()
        qt_buffer = QBuffer(encoded)
        qt_buffer.open(QIODevice.WriteOnly)
        pixmap.save(qt_buffer, "PNG")
        qt_buffer.close()
        dialog = ImagePreviewDialog(bytes(encoded), "large.png")
        dialog.show()
        self.app.processEvents()
        dialog.fit_to_window()
        fitted_zoom = dialog._zoom
        self.assertLess(fitted_zoom, 1.0)
        dialog._step_zoom(1)
        self.assertGreater(dialog._zoom, fitted_zoom)
        dialog._set_zoom(1.0)
        horizontal = dialog.image_scroll.horizontalScrollBar()
        vertical = dialog.image_scroll.verticalScrollBar()
        horizontal.setValue(horizontal.maximum() // 2)
        vertical.setValue(vertical.maximum() // 2)
        previous_position = (horizontal.value(), vertical.value())
        dialog._pan_image(QPoint(20, 15))
        self.assertEqual(
            (horizontal.value(), vertical.value()),
            (previous_position[0] - 20, previous_position[1] - 15),
        )
        dialog.zoom_actual_btn.click()
        self.assertEqual(dialog._zoom, 1.0)
        self.assertEqual(dialog.zoom_label.text(), "100%")
        dialog.close()

    def test_ticker_uses_styled_hover_preview(self):
        api = FakeAnnouncementApi()
        api.visible_announcements = [self._announcement()]
        window = MainWindow(
            self.database,
            self.admin,
            session_manager=FakeSession(api),
        )
        window.show()
        self.assertTrue(self._wait_until(lambda: bool(window.announcements)))

        ticker = window.announcement_ticker_button
        self.assertIsInstance(ticker, AnnouncementTickerButton)
        ticker.enterEvent(QEvent(QEvent.Enter))
        self.app.processEvents()
        hover = ticker._hover_card
        self.assertTrue(hover.isVisible())
        self.assertEqual(hover.title_label.text(), "系统维护公告")
        self.assertEqual(
            hover.summary_label.text(),
            "今晚 22:00 维护，请提前保存数据",
        )
        self.assertEqual(hover.badge_label.text(), "公告 1 / 1")
        self.assertEqual(ticker.toolTip(), "")
        self.assertTrue(hover.testAttribute(Qt.WA_TranslucentBackground))
        ticker.leaveEvent(QEvent(QEvent.Leave))
        self.assertFalse(hover.isVisible())

    def test_startup_announcement_opens_once_and_records_startup_display(self):
        api = FakeAnnouncementApi()
        window = MainWindow(
            self.database,
            self.admin,
            session_manager=FakeSession(api),
        )
        announcement = self._announcement(startup_pending=True)
        window._announcements_loaded(
            {"announcements": [announcement], "unread_messages": 0},
            None,
        )
        self.assertTrue(
            self._wait_until(
                lambda: (
                    window.announcement_dialog is not None
                    and window.announcement_dialog.isVisible()
                )
            )
        )
        self.assertFalse(announcement["startup_pending"])
        self.assertTrue(
            self._wait_until(lambda: ("announcement-1", True) in api.read_calls)
        )

    def test_normal_user_detail_offers_plain_text_contact_only(self):
        user = self.database.create_account(
            "notice_user",
            "Notice@123",
            "user",
            self.admin.id,
            display_name="公告用户",
        )
        dialog = AnnouncementDetailDialog(
            self._announcement(),
            FakeSession(FakeAnnouncementApi()),
            user,
        )
        button_texts = [button.text() for button in dialog.findChildren(QPushButton)]
        self.assertIn("联系管理员", button_texts)
        self.assertIn("关闭弹窗", button_texts)

        composer = ContactAdminDialog(self._announcement())
        self.assertIsInstance(composer.message_edit, QPlainTextEdit)
        composer.message_edit.setPlainText("<b>这是纯文字</b>")
        self.assertEqual(composer.message(), "<b>这是纯文字</b>")

    def test_contact_attachment_picker_encodes_files(self):
        path = Path(self.temp_dir.name) / "现场.txt"
        path.write_bytes(b"contact attachment")
        picker = ContactAttachmentPicker()
        picker._add_paths([str(path)], "file")

        self.assertTrue(picker.has_attachments())
        self.assertEqual(picker.list_widget.count(), 1)
        payload = picker.payloads()[0]
        self.assertEqual(payload["file_name"], "现场.txt")
        self.assertEqual(payload["content_base64"], "Y29udGFjdCBhdHRhY2htZW50")

    def test_contact_dialog_falls_back_to_legacy_text_message_api(self):
        class LegacyApi(FakeAnnouncementApi):
            @staticmethod
            def contact_conversations(
                _token,
                *,
                announcement_id=None,
                limit=50,
                mark_read=True,
            ):
                raise ApiResponseError(
                    "not_found",
                    "Not Found",
                    status_code=404,
                )

        api = LegacyApi()
        with patch(
            "integrated_client.ui.announcement_page.run_with_loading",
            side_effect=lambda _parent, _message, function: function(),
        ):
            dialog = ContactConversationDialog(
                self._announcement(),
                FakeSession(api),
            )
            self.assertTrue(dialog.legacy_mode)
            self.assertTrue(dialog.attachment_picker.isHidden())
            dialog.message_edit.setPlainText("旧服务反馈")
            with patch(
                "integrated_client.ui.announcement_page.QMessageBox.information"
            ):
                dialog._send()

        self.assertEqual(api.sent_messages, [("announcement-1", "旧服务反馈")])

    def test_editor_preserves_rich_text_targets_and_startup_option(self):
        accounts = [
            {
                "id": "user-id",
                "username": "station01",
                "display_name": "一号站",
                "role": "user",
                "is_active": True,
                "is_archived": False,
            }
        ]
        dialog = AnnouncementEditorDialog(accounts)
        self.assertTrue(dialog.title_edit.testAttribute(Qt.WA_InputMethodEnabled))
        self.assertTrue(dialog.ticker_edit.testAttribute(Qt.WA_InputMethodEnabled))
        self.assertTrue(dialog.body_edit.testAttribute(Qt.WA_InputMethodEnabled))
        dialog.title_edit.setText("格式化公告")
        dialog.ticker_edit.setText("请查看格式化公告")
        dialog.body_edit.setPlainText("重点内容")
        dialog.body_edit.selectAll()
        dialog._toggle_bold(True)
        dialog.show_on_startup_checkbox.setChecked(True)
        dialog.all_users_checkbox.setChecked(False)
        dialog.target_list.item(0).setCheckState(Qt.Checked)

        payload = dialog.announcement_payload()
        self.assertEqual(payload["target_account_ids"], ["user-id"])
        self.assertTrue(payload["show_on_startup"])
        self.assertIn("font-weight", payload["body_html"])
        self.assertIn("重点内容", payload["body_html"])
        self.assertEqual(dialog.toolbar_buttons["bold"].text(), "B")
        self.assertEqual(dialog.toolbar_buttons["bold"].toolTip(), "加粗")
        self.assertEqual(dialog.toolbar_buttons["italic"].text(), "I")
        self.assertEqual(dialog.toolbar_buttons["underline"].text(), "U")
        self.assertTrue(dialog.toolbar_buttons["align_left"].icon().isNull() is False)

    def test_admin_page_marks_message_read_only_after_chat_opens(self):
        api = FakeAnnouncementApi()
        api.managed_announcements = [self._announcement()]
        api.accounts = [
            {
                "id": "user-id",
                "username": "station01",
                "display_name": "一号站",
                "role": "user",
                "is_active": True,
                "is_archived": False,
            }
        ]
        api.inbox = {
            "unread_count": 1,
            "items": [
                {
                    "id": "message-id",
                    "sender_display_name": "一号站",
                    "sender_username": "station01",
                    "announcement_title": "系统维护公告",
                    "message": "<b>这里按纯文字显示</b>",
                    "messages": [
                        {
                            "sender_role": "user",
                            "message": "<b>这里按纯文字显示</b>",
                            "created_at": "2026-07-28T11:00:00",
                            "attachments": [
                                {
                                    "id": "attachment-id",
                                    "file_name": "现场.png",
                                    "kind": "image",
                                    "size": 120,
                                }
                            ],
                        }
                    ],
                    "created_at": "2026-07-28T11:00:00",
                    "read_at": None,
                    "unread_count": 1,
                }
            ],
        }
        page = AnnouncementAdminPage(FakeSession(api))
        page.show()
        self.app.processEvents()
        unread = []
        page.unread_messages_changed.connect(unread.append)
        with patch(
            "integrated_client.ui.announcement_page.run_with_loading",
            side_effect=lambda _parent, _message, function: function(),
        ):
            page.refresh()

        self.assertEqual(page.announcement_table.rowCount(), 1)
        self.assertEqual(page.announcement_table.item(0, 2).text(), "2 / 3")
        self.assertEqual(page.message_table.rowCount(), 1)
        self.assertEqual(unread, [1])
        self.assertEqual(page.message_tab_badge.text(), "有 1 条新消息")
        self.assertFalse(page.message_tab_badge.isHidden())
        self.assertTrue(page.message_badge_spacer.isVisible())
        self.assertGreater(
            page.message_tab_badge.width(),
            page.message_tab_badge.fontMetrics().horizontalAdvance(
                page.message_tab_badge.text()
            ),
        )
        self.assertEqual(page.announcement_preview.objectName(), "AnnouncementBody")
        self.assertEqual(
            page.announcement_receipt_detail.objectName(),
            "AnnouncementReceiptDetail",
        )
        page.message_table.selectRow(0)
        self.app.processEvents()
        self.assertIsNone(api.inbox["items"][0].get("read_at"))
        self.assertIsNone(page.messages[0].get("read_at"))
        self.assertEqual(page.inbox_summary.text(), "未读消息：1")
        self.assertFalse(page.message_tab_badge.isHidden())
        self.assertFalse(
            any(
                button.text() == "标记所选为已读"
                for button in page.findChildren(QPushButton)
            )
        )
        page._reply_message()
        self.app.processEvents()
        chat = page._chat_windows["message-id"]
        self.assertTrue(
            self._wait_until(
                lambda: api.inbox["items"][0].get("read_at") is not None
                or page.messages[0].get("read_at") is not None
            )
        )
        self.assertEqual(page.inbox_summary.text(), "未读消息：0")
        self.assertTrue(page.message_tab_badge.isHidden())
        self.assertIsInstance(chat, AdminConversationDialog)
        self.assertFalse(chat.isModal())
        self.assertIsNone(chat.parent())
        self.assertEqual(chat.windowType(), Qt.Window)
        self.assertTrue(chat.message_edit.isEnabled())
        self.assertTrue(chat.message_edit.hasFocus())
        self.assertIn("<b>这里按纯文字显示</b>", chat.history.toPlainText())
        chat.message_edit.setPlainText("管理员回复")
        chat._send()
        self.assertTrue(self._wait_until(lambda: bool(api.admin_replies)))
        self.assertEqual(api.admin_replies[0][:2], ("message-id", "管理员回复"))

    def test_chat_composers_put_count_and_separator_before_attachments(self):
        api = FakeAnnouncementApi()
        session = FakeSession(api)
        with patch(
            "integrated_client.ui.announcement_page.run_with_loading",
            side_effect=lambda _parent, _message, function: function(),
        ):
            user_chat = ContactConversationDialog(self._announcement(), session)
        admin_chat = AdminConversationDialog(
            {
                "id": "conversation-layout",
                "sender_display_name": "布局用户",
                "sender_username": "layout-user",
                "announcement_title": "布局公告",
                "status": "open",
                "messages": [],
            },
            session,
        )

        for chat in (user_chat, admin_chat):
            composer_layout = chat.message_edit.parentWidget().layout()
            self.assertLess(
                composer_layout.indexOf(chat.message_edit),
                composer_layout.indexOf(chat.count_label),
            )
            self.assertLess(
                composer_layout.indexOf(chat.count_label),
                composer_layout.indexOf(chat.composer_separator),
            )
            self.assertLess(
                composer_layout.indexOf(chat.composer_separator),
                composer_layout.indexOf(chat.attachment_picker),
            )
            self.assertEqual(chat.composer_separator.frameShape(), QFrame.HLine)
            chat.close()

    def test_detail_contact_closes_announcement_and_opens_independent_chat(self):
        api = FakeAnnouncementApi()
        api.visible_announcements = [self._announcement()]
        user = self.database.create_account(
            "contact_user",
            "Contact@123",
            "user",
            self.admin.id,
            display_name="联系用户",
        )
        window = MainWindow(
            self.database,
            user,
            session_manager=FakeSession(api),
        )
        window.show()
        self.assertTrue(self._wait_until(lambda: bool(window.announcements)))
        window._show_current_announcement()
        detail = window.announcement_dialog
        detail._contact_admin()
        self.app.processEvents()

        chat = window.contact_conversation_dialogs["announcement-1"]
        self.assertFalse(detail.isVisible())
        self.assertTrue(chat.isVisible())
        self.assertFalse(chat.isModal())
        self.assertIsNone(chat.parent())
        self.assertEqual(chat.windowType(), Qt.Window)
        window.showMinimized()
        self.app.processEvents()
        self.assertTrue(window.isMinimized())
        self.assertFalse(chat.isMinimized())
        window.showNormal()
        self.assertFalse(
            any(
                button.text() == "未解决，继续沟通"
                for button in chat.findChildren(QPushButton)
            )
        )

    def test_admin_tables_keep_all_columns_interactive_and_fill_viewport(self):
        page = AnnouncementAdminPage(FakeSession(FakeAnnouncementApi()))
        message_header = page.message_table.horizontalHeader()
        for column in range(page.message_table.columnCount()):
            self.assertEqual(
                message_header.sectionResizeMode(column),
                QHeaderView.Interactive,
            )
        self.assertTrue(message_header.stretchLastSection())
        announcement_header = page.announcement_table.horizontalHeader()
        for column in range(page.announcement_table.columnCount()):
            self.assertEqual(
                announcement_header.sectionResizeMode(column),
                QHeaderView.Interactive,
            )
        self.assertTrue(announcement_header.stretchLastSection())
        page.deleteLater()

    def test_history_marks_the_visible_conversation_read_when_opened(self):
        api = FakeAnnouncementApi()
        api.visible_announcements = [self._announcement(read_at=True)]
        api.conversations = [
            {
                "id": "conversation-1",
                "announcement_id": "announcement-1",
                "announcement_title": "系统维护公告",
                "status": "open",
                "user_unread_count": 1,
                "messages": [
                    {
                        "id": "admin-reply-1",
                        "sender_role": "admin",
                        "message": "管理员已回复",
                        "created_at": "2026-08-19T10:00:00",
                        "attachments": [],
                    }
                ],
            }
        ]
        user = self.database.create_account(
            "badge_user",
            "BadgeUser@123",
            "user",
            self.admin.id,
            display_name="徽标用户",
        )
        window = MainWindow(
            self.database,
            user,
            session_manager=FakeSession(api),
        )
        window.show()
        self.assertTrue(
            self._wait_until(
                lambda: window.announcement_horn_button.message_unread_count == 1
            )
        )
        window._show_contact_history()
        history = window.contact_history_dialog
        self.assertTrue(self._wait_until(lambda: api.user_read_calls == ["conversation-1"]))
        self.assertEqual(window.announcement_horn_button.message_unread_count, 0)
        self.assertNotIn("新消息", history.conversation_picker.itemText(0))

    def test_chat_failed_messages_can_be_retried_for_user_and_admin(self):
        api = FakeAnnouncementApi()
        session = FakeSession(api)
        user_attempts = []
        original_user_send = api.send_admin_message

        def flaky_user_send(*args, **kwargs):
            user_attempts.append(1)
            if len(user_attempts) == 1:
                raise NetworkUnavailable("网络暂时不可用")
            return original_user_send(*args, **kwargs)

        api.send_admin_message = flaky_user_send
        with patch(
            "integrated_client.ui.announcement_page.run_with_loading",
            side_effect=lambda _parent, _message, function: function(),
        ):
            user_chat = ContactConversationDialog(self._announcement(), session)
            user_chat.message_edit.setPlainText("稍后重发的用户消息")
            user_chat._send()
            user_chat.message_edit.setPlainText("下一条草稿可立即输入")
            self.assertTrue(user_chat.message_edit.isEnabled())
            self.assertEqual(user_chat.message_edit.toPlainText(), "下一条草稿可立即输入")
            self.assertTrue(
                self._wait_until(
                    lambda: bool(user_chat._failed_messages)
                    and user_chat._failed_messages[0].get("_send_state") == "failed"
                )
            )
            self.assertEqual(len(user_chat._failed_messages), 1)
            self.assertIn("发送失败", user_chat.status_label.text())
            retry = user_chat.findChild(QPushButton, "ChatRetryButton")
            self.assertIsNotNone(retry)
            retry.click()
            self.assertTrue(
                self._wait_until(
                    lambda: len(user_attempts) == 2
                    and not user_chat._failed_messages
                )
            )
            self.assertEqual(api.sent_messages[-1][1], "稍后重发的用户消息")
            self.assertEqual(user_chat.message_edit.toPlainText(), "下一条草稿可立即输入")

        conversation = {
            "id": "admin-retry-conversation",
            "sender_display_name": "测试站",
            "sender_username": "station01",
            "announcement_title": "公告",
            "status": "open",
            "messages": [],
        }
        admin_attempts = []
        original_admin_send = api.admin_reply_message

        def flaky_admin_send(*args, **kwargs):
            admin_attempts.append(1)
            if len(admin_attempts) == 1:
                raise NetworkUnavailable("服务器暂时不可用")
            return original_admin_send(*args, **kwargs)

        api.admin_reply_message = flaky_admin_send
        with patch(
            "integrated_client.ui.announcement_page.run_with_loading",
            side_effect=lambda _parent, _message, function: function(),
        ):
            admin_chat = AdminConversationDialog(conversation, session)
            admin_chat.message_edit.setPlainText("稍后重发的管理员回复")
            admin_chat._send()
            admin_chat.message_edit.setPlainText("管理员下一条草稿")
            self.assertTrue(admin_chat.message_edit.isEnabled())
            self.assertTrue(
                self._wait_until(
                    lambda: bool(admin_chat._failed_messages)
                    and admin_chat._failed_messages[0].get("_send_state") == "failed"
                )
            )
            self.assertEqual(len(admin_chat._failed_messages), 1)
            retry = admin_chat.findChild(QPushButton, "ChatRetryButton")
            self.assertIsNotNone(retry)
            retry.click()
            self.assertTrue(
                self._wait_until(
                    lambda: len(admin_attempts) == 2
                    and not admin_chat._failed_messages
                )
            )
            self.assertEqual(api.admin_replies[-1][1], "稍后重发的管理员回复")
            self.assertEqual(admin_chat.message_edit.toPlainText(), "管理员下一条草稿")

        user_chat.close()
        admin_chat.close()

    def test_chat_outgoing_bubbles_show_peer_read_receipts(self):
        timeline = ConversationTimeline(lambda _attachment: None)
        timeline.set_messages(
            [
                {
                    "sender_role": "user",
                    "message": "已读消息",
                    "created_at": "2026-08-19T10:00:00",
                    "admin_read_at": "2026-08-19T10:01:00",
                },
                {
                    "sender_role": "user",
                    "message": "未读消息",
                    "created_at": "2026-08-19T10:02:00",
                    "admin_read_at": None,
                },
            ],
            own_role="user",
        )
        receipt_texts = [
            label.text()
            for label in timeline.findChildren(QLabel, "ChatBubbleTime")
        ]
        self.assertTrue(any("已读" in text for text in receipt_texts))
        self.assertTrue(any("未读" in text for text in receipt_texts))

        timeline.set_messages(
            [
                {
                    "sender_role": "admin",
                    "message": "管理员消息",
                    "created_at": "2026-08-19T10:03:00",
                    "user_read_at": "2026-08-19T10:04:00",
                }
            ],
            own_role="admin",
            peer_label="用户",
        )
        receipt_texts = [
            label.text()
            for label in timeline.findChildren(QLabel, "ChatBubbleTime")
        ]
        self.assertTrue(any("已读" in text for text in receipt_texts))
        timeline.close()


if __name__ == "__main__":
    unittest.main()

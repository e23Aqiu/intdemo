from __future__ import annotations

import base64
import mimetypes
import os
from pathlib import Path

from PyQt5.QtCore import (
    QPoint,
    QRect,
    QRectF,
    QObject,
    QRunnable,
    QSize,
    Qt,
    QThreadPool,
    QTimer,
    pyqtSignal,
)
from PyQt5.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QIcon,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QTextCharFormat,
    QTextListFormat,
)
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextBrowser,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..online.api import ApiResponseError, NetworkUnavailable
from .file_dialogs import SystemFileDialog as QFileDialog
from .frameless import FramelessDialog
from .frameless import FramelessMessageBox as QMessageBox
from .loading_dialog import run_with_loading

MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
MAX_ANNOUNCEMENT_ATTACHMENT_BYTES = 25 * 1024 * 1024
MAX_CONTACT_ATTACHMENT_BYTES = 25 * 1024 * 1024
MAX_ATTACHMENTS = 8
ASSET_DIRECTORY = Path(__file__).with_name("assets")


class ApiCallSignals(QObject):
    finished = pyqtSignal(object, object)


class ApiCallTask(QRunnable):
    """Run a polling API call without blocking the main-window event loop."""

    def __init__(self, function):
        super().__init__()
        self.function = function
        self.signals = ApiCallSignals()

    def run(self):
        try:
            result = self.function()
        except Exception as exc:  # noqa: BLE001 - crosses the Qt worker boundary
            self.signals.finished.emit(None, exc)
        else:
            self.signals.finished.emit(result, None)


def start_api_task(function, completed):
    task = ApiCallTask(function)
    task.signals.finished.connect(completed)
    QThreadPool.globalInstance().start(task)
    return task


def _format_size(size):
    size = max(0, int(size or 0))
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    if size >= 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size} B"


def _display_time(value):
    return str(value or "").replace("T", " ")[:19] or "-"


def _encode_attachment_item(item):
    path = Path(item["path"])
    return {
        "file_name": item["file_name"],
        "content_type": item["content_type"],
        "kind": item["kind"],
        "content_base64": base64.b64encode(path.read_bytes()).decode("ascii"),
    }


def _open_contact_attachment(parent, session, attachment):
    file_name = str(attachment.get("file_name") or "消息附件")
    try:
        data = run_with_loading(
            parent,
            "正在读取附件…",
            lambda: session.api.download_contact_attachment(
                session.access_token(),
                str(attachment.get("id")),
            ),
        )
    except (ApiResponseError, NetworkUnavailable) as exc:
        QMessageBox.warning(parent, "附件读取失败", str(exc))
        return
    if attachment.get("kind") == "image":
        ImagePreviewDialog(
            data,
            file_name,
            parent,
            save_caption="保存消息图片",
        ).exec_()
        return
    target, _ = QFileDialog.getSaveFileName(parent, "保存消息附件", file_name, "所有文件 (*.*)")
    if not target:
        return
    try:
        Path(target).write_bytes(data)
    except OSError as exc:
        QMessageBox.warning(parent, "保存失败", str(exc))
        return
    QMessageBox.information(parent, "保存完成", f"附件已保存到：\n{target}")


class AnnouncementHoverCard(QFrame):
    """Top-bar announcement preview that is independent of native tooltips."""

    def __init__(self, parent=None):
        super().__init__(
            parent,
            Qt.ToolTip | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint,
        )
        self.announcement = None
        self.setObjectName("AnnouncementHoverCard")
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setFixedWidth(430)
        self.setStyleSheet(
            """
            QFrame#AnnouncementHoverCard {
                background: transparent;
                border: none;
            }
            QLabel#AnnouncementHoverBadge {
                color: #b9eee4;
                font-size: 11px;
                font-weight: 700;
            }
            QLabel#AnnouncementHoverState {
                color: #ffd4ae;
                background: #5d4539;
                border: 1px solid #8c654e;
                border-radius: 9px;
                padding: 2px 8px;
                font-size: 11px;
                font-weight: 700;
            }
            QLabel#AnnouncementHoverTitle {
                color: #ffffff;
                font-size: 16px;
                font-weight: 700;
            }
            QLabel#AnnouncementHoverSummary {
                color: #d9ebe7;
                font-size: 13px;
            }
            QLabel#AnnouncementHoverMeta {
                color: #99bbb5;
                font-size: 11px;
            }
            QLabel#AnnouncementHoverHint {
                color: #83d6c7;
                font-size: 11px;
                font-weight: 700;
            }
            """
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 18)
        root.setSpacing(9)
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        self.badge_label = QLabel("公告")
        self.badge_label.setObjectName("AnnouncementHoverBadge")
        self.state_label = QLabel("未读")
        self.state_label.setObjectName("AnnouncementHoverState")
        header.addWidget(self.badge_label)
        header.addStretch()
        header.addWidget(self.state_label)
        root.addLayout(header)

        self.title_label = QLabel()
        self.title_label.setObjectName("AnnouncementHoverTitle")
        self.title_label.setWordWrap(True)
        root.addWidget(self.title_label)
        self.summary_label = QLabel()
        self.summary_label.setObjectName("AnnouncementHoverSummary")
        self.summary_label.setWordWrap(True)
        root.addWidget(self.summary_label)
        self.meta_label = QLabel()
        self.meta_label.setObjectName("AnnouncementHoverMeta")
        self.meta_label.setWordWrap(True)
        root.addWidget(self.meta_label)
        self.hint_label = QLabel("点击轮播内容查看公告详情")
        self.hint_label.setObjectName("AnnouncementHoverHint")
        root.addWidget(self.hint_label)
        self.hide()

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        card_rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(card_rect, 13, 13)
        painter.fillPath(path, QColor(23, 58, 61, 248))
        painter.setPen(QPen(QColor("#4f8580"), 1))
        painter.drawPath(path)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#55c2b1"))
        painter.drawRoundedRect(QRectF(18, 0, 92, 4), 2, 2)

    def show_announcement(
        self,
        announcement,
        index,
        total,
        anchor,
        *,
        show_unread=True,
    ):
        self.announcement = announcement
        unread = bool(show_unread and not announcement.get("read_at"))
        self.badge_label.setText(f"公告 {int(index) + 1} / {max(1, int(total))}")
        self.state_label.setText("未读" if unread else "已读")
        self.state_label.setVisible(bool(show_unread))
        self.title_label.setText(str(announcement.get("title") or "公告"))
        self.summary_label.setText(
            str(
                announcement.get("ticker_text")
                or "点击查看该公告的完整内容。"
            )
        )
        self.meta_label.setText(
            f"{announcement.get('created_by_name') or '管理员'}  ·  "
            f"{_display_time(announcement.get('created_at'))}"
        )
        self.layout().activate()
        self.adjustSize()
        card_width = self.width()
        card_height = self.height()
        x = int(anchor.x() - card_width / 2)
        y = int(anchor.y() + 10)
        screen = QApplication.screenAt(anchor) or QApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            x = max(
                available.left() + 8,
                min(x, available.right() - card_width - 8),
            )
            if y + card_height > available.bottom() - 8:
                y = int(anchor.y() - card_height - 10)
            y = max(
                available.top() + 8,
                min(y, available.bottom() - card_height - 8),
            )
        self.move(x, y)
        self.show()
        self.raise_()


class AnnouncementHornButton(QPushButton):
    """Announcement shortcut with a compact unread-message counter."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._message_unread_count = 0

    @property
    def message_unread_count(self):
        return self._message_unread_count

    def set_message_unread_count(self, count):
        count = max(0, int(count or 0))
        if count == self._message_unread_count:
            return
        self._message_unread_count = count
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        if not self._message_unread_count:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        diameter = 18
        badge = QRect(self.width() - diameter - 1, 1, diameter, diameter)
        painter.setPen(QPen(QColor("#ffffff"), 1))
        painter.setBrush(QColor("#d14f45"))
        painter.drawEllipse(badge)
        font = painter.font()
        font.setPointSize(7)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor("#ffffff"))
        label = "99+" if self._message_unread_count > 99 else str(
            self._message_unread_count
        )
        painter.drawText(badge, Qt.AlignCenter, label)
        painter.end()


class AnnouncementTickerButton(QPushButton):
    """Ticker button with a branded, multi-line hover preview."""

    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        self._announcement = None
        self._announcement_index = 0
        self._announcement_total = 0
        self._show_unread = True
        self._hover_card = AnnouncementHoverCard(self)
        self.setMouseTracking(True)

    def set_announcement(self, announcement, index=0, total=0, *, show_unread=True):
        self._announcement = announcement
        self._announcement_index = int(index)
        self._announcement_total = int(total)
        self._show_unread = bool(show_unread)
        if announcement is None or not self.isEnabled():
            self._hover_card.hide()
        elif self._hover_card.isVisible():
            self._show_hover_card()

    def _show_hover_card(self, anchor=None):
        if self._announcement is None or not self.isEnabled():
            return
        anchor = anchor or self.mapToGlobal(
            QPoint(self.width() // 2, self.height())
        )
        self._hover_card.show_announcement(
            self._announcement,
            self._announcement_index,
            self._announcement_total,
            anchor,
            show_unread=self._show_unread,
        )

    def enterEvent(self, event):
        self._show_hover_card()
        super().enterEvent(event)

    def mouseMoveEvent(self, event):
        self._show_hover_card(
            self.mapToGlobal(QPoint(event.pos().x(), self.height()))
        )
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        self._hover_card.hide()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        self._hover_card.hide()
        super().mousePressEvent(event)

    def hideEvent(self, event):
        self._hover_card.hide()
        super().hideEvent(event)


class AnnouncementListDialog(FramelessDialog):
    """Archive-style list of every announcement visible to the account."""

    announcement_open_requested = pyqtSignal(object)
    announcement_read_requested = pyqtSignal(object)
    conversation_history_requested = pyqtSignal()

    def __init__(self, announcements, parent=None, *, show_unread=True):
        super().__init__(
            parent,
            resizable=True,
            show_minimize=True,
            show_maximize=True,
        )
        self.announcements = []
        self.show_unread = bool(show_unread)
        self.setWindowTitle("全部公告")
        self.setModal(True)
        self.resize(980, 680)
        self.setMinimumSize(760, 540)

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 46, 28, 24)
        root.setSpacing(14)
        heading_row = QHBoxLayout()
        heading_box = QVBoxLayout()
        title = QLabel("全部公告")
        title.setObjectName("PageTitle")
        subtitle = QLabel("按发布时间查看当前账号可见的全部公告")
        subtitle.setObjectName("Muted")
        heading_box.addWidget(title)
        heading_box.addWidget(subtitle)
        heading_row.addLayout(heading_box)
        heading_row.addStretch()
        self.count_label = QLabel()
        self.count_label.setObjectName("AnnouncementListCount")
        heading_row.addWidget(self.count_label)
        root.addLayout(heading_row)

        splitter = QSplitter(Qt.Horizontal)
        self.announcement_list = QListWidget()
        self.announcement_list.setObjectName("AnnouncementArchiveList")
        self.announcement_list.setSelectionMode(
            QAbstractItemView.SingleSelection
        )
        self.announcement_list.setMinimumWidth(330)
        self.announcement_list.itemSelectionChanged.connect(
            self._selection_changed
        )
        self.announcement_list.itemDoubleClicked.connect(
            lambda _item: self._open_selected()
        )
        splitter.addWidget(self.announcement_list)

        preview = QFrame()
        preview.setObjectName("AnnouncementListPreview")
        preview_layout = QVBoxLayout(preview)
        preview_layout.setContentsMargins(22, 20, 22, 20)
        preview_layout.setSpacing(10)
        self.preview_state = QLabel()
        self.preview_state.setObjectName("AnnouncementPreviewState")
        preview_layout.addWidget(self.preview_state, 0, Qt.AlignLeft)
        self.preview_title = QLabel("请选择一条公告")
        self.preview_title.setObjectName("AnnouncementPreviewTitle")
        self.preview_title.setWordWrap(True)
        preview_layout.addWidget(self.preview_title)
        self.preview_meta = QLabel()
        self.preview_meta.setObjectName("Muted")
        self.preview_meta.setWordWrap(True)
        preview_layout.addWidget(self.preview_meta)
        self.preview_ticker = QLabel()
        self.preview_ticker.setObjectName("AnnouncementPreviewTicker")
        self.preview_ticker.setWordWrap(True)
        preview_layout.addWidget(self.preview_ticker)
        self.preview_body = QTextBrowser()
        self.preview_body.setObjectName("AnnouncementBody")
        self.preview_body.setOpenExternalLinks(False)
        self.preview_body.setPlaceholderText("选择公告后在这里预览正文。")
        preview_layout.addWidget(self.preview_body, 1)
        self.preview_attachments = QLabel()
        self.preview_attachments.setObjectName("Muted")
        preview_layout.addWidget(self.preview_attachments)
        splitter.addWidget(preview)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        root.addWidget(splitter, 1)

        buttons = QHBoxLayout()
        buttons.addStretch()
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.reject)
        self.open_button = QPushButton("查看公告详情")
        self.open_button.setObjectName("PrimaryButton")
        self.open_button.clicked.connect(self._open_selected)
        self.mark_read_button = QPushButton("标记已读")
        self.mark_read_button.clicked.connect(self._mark_selected_read)
        self.history_button = QPushButton("历史会话")
        self.history_button.clicked.connect(self._open_history)
        buttons.addWidget(close_btn)
        buttons.addWidget(self.history_button)
        buttons.addWidget(self.mark_read_button)
        buttons.addWidget(self.open_button)
        root.addLayout(buttons)

        self.set_announcements(announcements)

    def set_announcements(self, announcements):
        selected = self.selected_announcement()
        selected_id = str(selected.get("id") or "") if selected else ""
        self.announcements = list(announcements or [])
        self.announcement_list.clear()
        unread_count = (
            sum(
                not bool(announcement.get("read_at"))
                for announcement in self.announcements
            )
            if self.show_unread
            else 0
        )
        self.count_label.setText(
            f"共 {len(self.announcements)} 条"
            + (f" · 未读 {unread_count} 条" if self.show_unread else "")
        )
        selected_row = 0
        for index, announcement in enumerate(self.announcements):
            unread = bool(self.show_unread and not announcement.get("read_at"))
            title = str(announcement.get("title") or "公告")
            summary = str(
                announcement.get("ticker_text") or "暂无轮播摘要"
            )
            created_at = _display_time(announcement.get("created_at"))
            state = (
                "● 未读  ·  "
                if unread
                else ("已读  ·  " if self.show_unread else "")
            )
            item = QListWidgetItem(f"{state}{created_at}\n{title}\n{summary}")
            item.setData(Qt.UserRole, index)
            item.setSizeHint(QSize(300, 82))
            if unread and self.show_unread:
                item.setForeground(QColor("#176f68"))
                font = item.font()
                font.setBold(True)
                item.setFont(font)
            self.announcement_list.addItem(item)
            if str(announcement.get("id") or "") == selected_id:
                selected_row = index
        has_announcements = bool(self.announcements)
        self.open_button.setEnabled(has_announcements)
        self.history_button.setVisible(self.show_unread)
        self.mark_read_button.setVisible(self.show_unread)
        self.mark_read_button.setEnabled(False)
        if has_announcements:
            self.announcement_list.setCurrentRow(selected_row)
        else:
            self._clear_preview()

    def selected_announcement(self):
        item = self.announcement_list.currentItem()
        if item is None:
            return None
        index = item.data(Qt.UserRole)
        try:
            return self.announcements[int(index)]
        except (IndexError, TypeError, ValueError):
            return None

    def _clear_preview(self):
        self.preview_state.clear()
        self.preview_title.setText("暂无公告")
        self.preview_meta.clear()
        self.preview_ticker.clear()
        self.preview_body.clear()
        self.preview_attachments.clear()

    def _selection_changed(self):
        announcement = self.selected_announcement()
        if announcement is None:
            self._clear_preview()
            self.open_button.setEnabled(False)
            return
        unread = bool(self.show_unread and not announcement.get("read_at"))
        if self.show_unread:
            self.preview_state.setText("● 未读公告" if unread else "已读公告")
        else:
            self.preview_state.setText("公告")
        self.preview_state.setProperty("unread", unread)
        self.preview_state.style().unpolish(self.preview_state)
        self.preview_state.style().polish(self.preview_state)
        self.preview_title.setText(str(announcement.get("title") or "公告"))
        self.preview_meta.setText(
            f"发布人：{announcement.get('created_by_name') or '管理员'}    "
            f"发布时间：{_display_time(announcement.get('created_at'))}"
        )
        self.preview_ticker.setText(
            str(announcement.get("ticker_text") or "暂无轮播摘要")
        )
        self.preview_body.setHtml(str(announcement.get("body_html") or ""))
        attachment_count = len(announcement.get("attachments") or [])
        self.preview_attachments.setText(
            f"附件：{attachment_count} 个" if attachment_count else "无附件"
        )
        self.open_button.setEnabled(True)
        self.mark_read_button.setVisible(self.show_unread)
        self.mark_read_button.setEnabled(self.show_unread and unread)

    def _open_selected(self):
        announcement = self.selected_announcement()
        if announcement is None:
            return
        self.accept()
        self.announcement_open_requested.emit(announcement)

    def _mark_selected_read(self):
        announcement = self.selected_announcement()
        if not self.show_unread or announcement is None or announcement.get("read_at"):
            return
        announcement["read_at"] = True
        self.set_announcements(self.announcements)
        self.announcement_read_requested.emit(announcement)

    def _open_history(self):
        self.accept()
        self.conversation_history_requested.emit()


class ContactAdminDialog(FramelessDialog):
    """Plain-text-only message composer for normal users."""

    MAX_LENGTH = 2_000

    def __init__(self, announcement, parent=None):
        super().__init__(parent)
        self.announcement = announcement
        self.setWindowTitle("联系管理员")
        self.setModal(True)
        self.setFixedSize(610, 430)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 28, 28, 24)
        layout.setSpacing(12)
        title = QLabel("联系管理员")
        title.setObjectName("PageTitle")
        layout.addWidget(title)
        related = QLabel(f"关联公告：{announcement.get('title') or '公告'}")
        related.setObjectName("Muted")
        layout.addWidget(related)
        note = QLabel("普通用户仅可发送纯文字消息，管理员会在“公告发布”页面收到。")
        note.setObjectName("Muted")
        note.setWordWrap(True)
        layout.addWidget(note)

        self.message_edit = QPlainTextEdit()
        self.message_edit.setPlaceholderText("请输入需要咨询或反馈的内容…")
        self.message_edit.textChanged.connect(self._text_changed)
        layout.addWidget(self.message_edit, 1)
        self.count_label = QLabel(f"0 / {self.MAX_LENGTH}")
        self.count_label.setObjectName("Muted")
        self.count_label.setAlignment(Qt.AlignRight)
        layout.addWidget(self.count_label)

        buttons = QHBoxLayout()
        buttons.addStretch()
        close_btn = QPushButton("取消")
        close_btn.clicked.connect(self.reject)
        send_btn = QPushButton("发送消息")
        send_btn.setObjectName("PrimaryButton")
        send_btn.clicked.connect(self._accept_message)
        buttons.addWidget(close_btn)
        buttons.addWidget(send_btn)
        layout.addLayout(buttons)

    def _text_changed(self):
        text = self.message_edit.toPlainText()
        if len(text) > self.MAX_LENGTH:
            cursor = self.message_edit.textCursor()
            position = min(cursor.position(), self.MAX_LENGTH)
            self.message_edit.setPlainText(text[: self.MAX_LENGTH])
            cursor = self.message_edit.textCursor()
            cursor.setPosition(position)
            self.message_edit.setTextCursor(cursor)
            text = self.message_edit.toPlainText()
        self.count_label.setText(f"{len(text)} / {self.MAX_LENGTH}")

    def _accept_message(self):
        if not self.message():
            QMessageBox.warning(self, "消息为空", "请输入需要发送给管理员的文字。")
            return
        self.accept()

    def message(self):
        return self.message_edit.toPlainText().strip()


class ContactAttachmentPicker(QWidget):
    """Small reusable picker for contact-message images and files."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        actions = QHBoxLayout()
        add_image_btn = QPushButton("添加图片")
        add_image_btn.clicked.connect(self._add_images)
        add_file_btn = QPushButton("添加附件")
        add_file_btn.clicked.connect(self._add_files)
        remove_btn = QPushButton("移除")
        remove_btn.clicked.connect(self._remove_selected)
        actions.addWidget(add_image_btn)
        actions.addWidget(add_file_btn)
        actions.addWidget(remove_btn)
        actions.addStretch()
        self.summary = QLabel("未添加附件")
        self.summary.setObjectName("Muted")
        actions.addWidget(self.summary)
        layout.addLayout(actions)
        self.list_widget = QListWidget()
        self.list_widget.setMaximumHeight(82)
        self.list_widget.setAlternatingRowColors(True)
        self.list_widget.hide()
        layout.addWidget(self.list_widget)

    def _add_images(self, *_args):
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "选择消息图片",
            "",
            "图片文件 (*.png *.jpg *.jpeg *.gif *.bmp *.webp);;所有文件 (*.*)",
        )
        self._add_paths(paths, "image")

    def _add_files(self, *_args):
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "选择消息附件",
            "",
            "所有文件 (*.*)",
        )
        self._add_paths(paths, "file")

    def _add_paths(self, paths, kind):
        known_paths = {str(item.get("path")) for item in self._items}
        for raw_path in paths:
            if len(self._items) >= MAX_ATTACHMENTS:
                QMessageBox.warning(
                    self,
                    "附件数量超限",
                    f"单条消息最多上传 {MAX_ATTACHMENTS} 个附件。",
                )
                return
            path = Path(raw_path)
            try:
                resolved = str(path.resolve())
                size = path.stat().st_size
            except OSError as exc:
                QMessageBox.warning(self, "无法读取附件", str(exc))
                continue
            if resolved in known_paths:
                continue
            if size > MAX_ATTACHMENT_BYTES:
                QMessageBox.warning(self, "附件过大", f"{path.name} 超过 10 MB，未添加。")
                continue
            if self.total_size() + size > MAX_CONTACT_ATTACHMENT_BYTES:
                QMessageBox.warning(
                    self,
                    "附件总大小超限",
                    "单条消息的附件总大小不能超过 25 MB。",
                )
                return
            content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            actual_kind = (
                "image"
                if kind == "image" and content_type.startswith("image/")
                else "file"
            )
            item = {
                "path": resolved,
                "file_name": path.name,
                "kind": actual_kind,
                "content_type": content_type,
                "size": size,
            }
            self._items.append(item)
            known_paths.add(resolved)
            row = QListWidgetItem(
                f"{'图片' if actual_kind == 'image' else '文件'}  ·  "
                f"{path.name}  ({_format_size(size)})"
            )
            row.setData(Qt.UserRole, resolved)
            self.list_widget.addItem(row)
        self._refresh()

    def _remove_selected(self, *_args):
        row = self.list_widget.currentRow()
        if row < 0:
            return
        path = str(self.list_widget.item(row).data(Qt.UserRole) or "")
        self.list_widget.takeItem(row)
        self._items = [item for item in self._items if str(item.get("path")) != path]
        self._refresh()

    def _refresh(self):
        count = len(self._items)
        self.list_widget.setVisible(bool(count))
        self.summary.setText(
            f"{count} 个 · {_format_size(self.total_size())}"
            if count
            else "未添加附件"
        )

    def total_size(self):
        return sum(int(item.get("size") or 0) for item in self._items)

    def has_attachments(self):
        return bool(self._items)

    def payloads(self):
        return [_encode_attachment_item(item) for item in self._items]

    def clear(self):
        self._items.clear()
        self.list_widget.clear()
        self._refresh()


class ConversationTimeline(QScrollArea):
    """Scrollable, left/right aligned message bubbles with inline attachments."""

    def __init__(self, attachment_opened, parent=None):
        super().__init__(parent)
        self._attachment_opened = attachment_opened
        self._plain_text = ""
        self.setObjectName("ChatTimeline")
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._replace_content([])

    def _replace_content(self, messages, *, own_role="user", peer_label="管理员"):
        old_content = self.takeWidget()
        if old_content is not None:
            old_content.deleteLater()
        content = QWidget()
        content.setObjectName("ChatTimelineContent")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(14, 16, 14, 16)
        layout.setSpacing(12)
        plain_blocks = []
        if not messages:
            empty = QLabel("还没有消息，发送一条消息开始沟通。")
            empty.setObjectName("ChatEmptyState")
            empty.setAlignment(Qt.AlignCenter)
            layout.addStretch()
            layout.addWidget(empty)
            layout.addStretch()
        for message in messages:
            sender_role = str(message.get("sender_role") or "")
            own = sender_role == own_role
            sender = "我" if own else peer_label
            created_at = _display_time(message.get("created_at"))
            body = str(message.get("message") or "").strip()
            attachments = list(message.get("attachments") or [])
            display_body = body or "（仅附件）"
            plain_blocks.append(f"{sender}  ·  {created_at}\n{display_body}")

            row = QWidget(content)
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(10)
            bubble = QFrame(row)
            bubble.setObjectName("ChatBubble")
            bubble.setProperty("own", own)
            bubble.setMaximumWidth(560)
            bubble_layout = QVBoxLayout(bubble)
            bubble_layout.setContentsMargins(13, 10, 13, 9)
            bubble_layout.setSpacing(6)
            sender_label = QLabel(sender)
            sender_label.setObjectName("ChatBubbleSender")
            bubble_layout.addWidget(sender_label)
            if body:
                body_label = QLabel(body)
                body_label.setObjectName("ChatBubbleBody")
                body_label.setTextFormat(Qt.PlainText)
                body_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
                body_label.setWordWrap(True)
                bubble_layout.addWidget(body_label)
            for attachment in attachments:
                attachment_text = (
                    f"{'图片' if attachment.get('kind') == 'image' else '文件'}"
                    f"  ·  {attachment.get('file_name') or '附件'}"
                    f"  ({_format_size(attachment.get('size'))})"
                )
                attachment_button = QPushButton()
                attachment_button.setObjectName("ChatAttachmentButton")
                attachment_button.setMaximumWidth(520)
                attachment_button.setText(
                    QFontMetrics(attachment_button.font()).elidedText(
                        attachment_text,
                        Qt.ElideMiddle,
                        485,
                    )
                )
                attachment_button.setToolTip(
                    f"点击打开附件\n{attachment.get('file_name') or '附件'}"
                )
                attachment_button.clicked.connect(
                    lambda _checked=False, item=attachment: self._attachment_opened(item)
                )
                bubble_layout.addWidget(attachment_button)
            time_label = QLabel(created_at)
            time_label.setObjectName("ChatBubbleTime")
            time_label.setAlignment(Qt.AlignRight if own else Qt.AlignLeft)
            bubble_layout.addWidget(time_label)
            if own:
                row_layout.addStretch(1)
                row_layout.addWidget(bubble)
            else:
                row_layout.addWidget(bubble)
                row_layout.addStretch(1)
            layout.addWidget(row)
        if messages:
            layout.addStretch(1)
        self._plain_text = "\n\n".join(plain_blocks)
        self.setWidget(content)
        QTimer.singleShot(0, self._scroll_to_bottom)

    def set_messages(self, messages, *, own_role="user", peer_label="管理员"):
        self._replace_content(
            list(messages or []),
            own_role=own_role,
            peer_label=peer_label,
        )

    def toPlainText(self):
        return self._plain_text

    def clear(self):
        self._replace_content([])

    def _scroll_to_bottom(self):
        bar = self.verticalScrollBar()
        bar.setValue(bar.maximum())


class ContactConversationDialog(FramelessDialog):
    """User-side conversation history, reply composer and resolution controls."""

    MAX_LENGTH = 2_000
    unread_count_changed = pyqtSignal(int)
    messages_changed = pyqtSignal()

    def __init__(self, announcement, session, parent=None, *, history_mode=False):
        super().__init__(
            parent,
            resizable=True,
            show_minimize=True,
            show_maximize=True,
        )
        self.announcement = announcement or {}
        self.session = session
        self.history_mode = bool(history_mode or not announcement)
        self.conversation = None
        self.conversations = []
        self.total_unread_count = 0
        self.legacy_mode = False
        self._conversation_picker_updating = False
        self._refresh_task = None
        self._read_tasks = []
        self._known_admin_message_ids = set()
        self._message_ids_initialized = False
        self.setWindowTitle("历史会话" if self.history_mode else "联系管理员")
        self.setModal(False)
        self.resize(840, 700)
        self.setMinimumSize(680, 540)
        self.setAttribute(Qt.WA_DeleteOnClose, True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 46, 22, 20)
        layout.setSpacing(12)
        header = QFrame()
        header.setObjectName("ChatHeader")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(16, 13, 16, 13)
        header_layout.setSpacing(12)
        avatar = QLabel("管")
        avatar.setObjectName("ChatAvatar")
        avatar.setAlignment(Qt.AlignCenter)
        avatar.setFixedSize(42, 42)
        header_layout.addWidget(avatar)
        heading_box = QVBoxLayout()
        heading_box.setContentsMargins(0, 0, 0, 0)
        heading_box.setSpacing(3)
        title = QLabel("与管理员的历史会话" if self.history_mode else "联系管理员")
        title.setObjectName("PageTitle")
        heading_box.addWidget(title)
        self.related_label = QLabel(
            "查看与管理员的历史会话"
            if self.history_mode
            else f"关联公告：{self.announcement.get('title') or '公告'}"
        )
        self.related_label.setObjectName("Muted")
        self.related_label.setWordWrap(True)
        heading_box.addWidget(self.related_label)
        header_layout.addLayout(heading_box, 1)
        self.notice_label = QLabel("收到管理员新回复")
        self.notice_label.setObjectName("ChatNewMessageNotice")
        self.notice_label.hide()
        header_layout.addWidget(self.notice_label)
        layout.addWidget(header)
        self.conversation_picker = None
        if self.history_mode:
            self.conversation_picker = QComboBox()
            self.conversation_picker.setPlaceholderText("暂无历史会话")
            self.conversation_picker.currentIndexChanged.connect(
                self._conversation_changed
            )
            layout.addWidget(self.conversation_picker)
        self.status_label = QLabel("正在读取会话…")
        self.status_label.setObjectName("ChatStatus")
        layout.addWidget(self.status_label)

        self.history = ConversationTimeline(
            lambda attachment: _open_contact_attachment(
                self,
                self.session,
                attachment,
            ),
            self,
        )
        layout.addWidget(self.history, 1)

        # Kept as a hidden compatibility surface for older UI automation.
        self.received_attachments = QListWidget()
        self.received_attachments.hide()

        composer = QFrame()
        composer.setObjectName("ChatComposerFrame")
        composer_layout = QVBoxLayout(composer)
        composer_layout.setContentsMargins(12, 10, 12, 10)
        composer_layout.setSpacing(7)
        self.message_edit = QPlainTextEdit()
        self.message_edit.setObjectName("ChatComposer")
        self.message_edit.setMinimumHeight(82)
        self.message_edit.setMaximumHeight(116)
        self.message_edit.setPlaceholderText("输入需要发送给管理员的内容…")
        self.message_edit.textChanged.connect(self._limit_message)
        composer_layout.addWidget(self.message_edit)
        self.attachment_picker = ContactAttachmentPicker(self)
        composer_layout.addWidget(self.attachment_picker)
        self.count_label = QLabel(f"0 / {self.MAX_LENGTH}")
        self.count_label.setObjectName("Muted")
        self.count_label.setAlignment(Qt.AlignRight)
        composer_layout.addWidget(self.count_label)
        layout.addWidget(composer)

        buttons = QHBoxLayout()
        self.resolved_btn = QPushButton("已解决，结束会话")
        self.resolved_btn.clicked.connect(lambda: self._set_status("resolved"))
        buttons.addWidget(self.resolved_btn)
        buttons.addStretch()
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.reject)
        self.send_btn = QPushButton("发送消息")
        self.send_btn.setObjectName("PrimaryButton")
        self.send_btn.clicked.connect(self._send)
        buttons.addWidget(close_btn)
        buttons.addWidget(self.send_btn)
        layout.addLayout(buttons)
        self._load()
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(12_000)
        self._refresh_timer.timeout.connect(self._refresh_silently)
        self._refresh_timer.start()
        QTimer.singleShot(0, self.message_edit.setFocus)

    def _call(self, message, function):
        try:
            return run_with_loading(self, message, function)
        except (ApiResponseError, NetworkUnavailable) as exc:
            QMessageBox.warning(self, "消息操作失败", str(exc))
            return None

    def _fetch_conversations(self, **request_kwargs):
        try:
            return self.session.api.contact_conversations(
                self.session.access_token(),
                mark_read=False,
                **request_kwargs,
            )
        except TypeError:
            # Older servers and test doubles keep their original read-on-list behavior.
            return self.session.api.contact_conversations(
                self.session.access_token(),
                **request_kwargs,
            )

    def _load(self):
        request_kwargs = {}
        if not self.history_mode and self.announcement.get("id"):
            request_kwargs["announcement_id"] = str(self.announcement["id"])
        try:
            result = run_with_loading(
                self,
                "正在读取会话…",
                lambda: self._fetch_conversations(**request_kwargs),
            )
        except ApiResponseError as exc:
            if exc.status_code == 404:
                self._enable_legacy_mode()
                return
            QMessageBox.warning(self, "消息操作失败", str(exc))
            self._render()
            return
        except NetworkUnavailable as exc:
            QMessageBox.warning(self, "消息操作失败", str(exc))
            self._render()
            return
        self.conversations = list((result or {}).get("items") or [])
        self.total_unread_count = int((result or {}).get("unread_count") or 0)
        if self.history_mode:
            self._populate_conversation_picker()
        else:
            self.conversation = next(
                (item for item in self.conversations if item.get("status") == "open"),
                self.conversations[0] if self.conversations else None,
            )
        self._render()
        self._remember_admin_messages(notify=False)
        self.unread_count_changed.emit(self.total_unread_count)
        self._mark_current_conversation_read()

    @staticmethod
    def _conversation_label(conversation):
        title = str(conversation.get("announcement_title") or "公告已删除")
        latest = conversation.get("last_message") or {}
        preview = str(latest.get("message") or conversation.get("message") or "")
        preview = " ".join(preview.split())
        if len(preview) > 28:
            preview = preview[:25] + "…"
        status = "已解决" if conversation.get("status") == "resolved" else "处理中"
        timestamp = _display_time(
            conversation.get("updated_at") or conversation.get("created_at")
        )
        suffix = f" · {preview}" if preview else ""
        return f"{title} · {timestamp} · {status}{suffix}"

    def _populate_conversation_picker(self, selected_id=None):
        if self.conversation_picker is None:
            return
        if selected_id is None and self.conversation is not None:
            selected_id = str(self.conversation.get("id") or "")
        self._conversation_picker_updating = True
        self.conversation_picker.clear()
        for conversation in self.conversations:
            self.conversation_picker.addItem(
                self._conversation_label(conversation),
                conversation,
            )
        selected_index = -1
        if selected_id:
            for index, conversation in enumerate(self.conversations):
                if str(conversation.get("id") or "") == str(selected_id):
                    selected_index = index
                    break
        if selected_index < 0 and self.conversations:
            selected_index = next(
                (
                    index
                    for index, conversation in enumerate(self.conversations)
                    if conversation.get("status") == "open"
                ),
                0,
            )
        self.conversation_picker.setCurrentIndex(selected_index)
        self._conversation_picker_updating = False
        self.conversation = (
            self.conversations[selected_index]
            if selected_index >= 0
            else None
        )
        self._update_related_label()

    def _conversation_changed(self, index):
        if self._conversation_picker_updating or self.conversation_picker is None:
            return
        item = self.conversation_picker.itemData(index)
        self.conversation = item if isinstance(item, dict) else None
        self._update_related_label()
        self._render()
        self._mark_current_conversation_read()

    def _update_related_label(self):
        if not self.history_mode:
            return
        title = (self.conversation or {}).get("announcement_title") or "公告已删除"
        self.related_label.setText(f"关联公告：{title}")

    def _enable_legacy_mode(self):
        self.legacy_mode = True
        self.conversation = None
        self.status_label.setText(
            "当前服务仅支持发送纯文字消息。"
            if not self.history_mode
            else "当前服务暂不支持读取历史会话。"
        )
        self.history.clear()
        self.attachment_picker.hide()
        self.received_attachments.hide()
        self.resolved_btn.hide()
        self.send_btn.setText("发送消息")
        if self.history_mode:
            self.send_btn.setEnabled(False)
            self.message_edit.setEnabled(False)
            self.attachment_picker.setEnabled(False)

    def _render(self):
        conversation = self.conversation
        messages = list((conversation or {}).get("messages") or [])
        self.received_attachments.clear()
        for message in messages:
            role = "管理员" if message.get("sender_role") == "admin" else "我"
            attachments = list(message.get("attachments") or [])
            for attachment in attachments:
                item = QListWidgetItem(
                    f"{role} · {attachment.get('file_name') or '附件'}  "
                    f"({_format_size(attachment.get('size'))})"
                )
                item.setData(Qt.UserRole, attachment)
                self.received_attachments.addItem(item)
        self.history.set_messages(messages, own_role="user", peer_label="管理员")
        if self.history_mode:
            self._update_related_label()
        status = str((conversation or {}).get("status") or "")
        can_compose = bool(not self.history_mode or conversation)
        self.message_edit.setEnabled(can_compose)
        self.attachment_picker.setEnabled(can_compose and not self.legacy_mode)
        self.send_btn.setEnabled(can_compose and not self.legacy_mode)
        if not conversation:
            self.status_label.setText(
                "暂无历史会话" if self.history_mode else "尚未发起会话"
            )
            self.resolved_btn.setEnabled(False)
            self.send_btn.setText("发送消息")
        elif status == "resolved":
            self.status_label.setText("该会话已解决；继续发送将发起新会话。")
            self.resolved_btn.setEnabled(False)
            self.send_btn.setText("发起新会话")
        else:
            self.status_label.setText("会话处理中，可继续回复管理员。")
            self.resolved_btn.setEnabled(True)
            self.send_btn.setText("发送回复")
        if self.history_mode and not conversation:
            self.send_btn.setEnabled(False)

    def _request_kwargs(self):
        request_kwargs = {"limit": 200 if self.history_mode else 50}
        if not self.history_mode and self.announcement.get("id"):
            request_kwargs["announcement_id"] = str(self.announcement["id"])
        return request_kwargs

    def _refresh_silently(self):
        if self._refresh_task is not None or not self.isVisible() or self.legacy_mode:
            return

        def completed(result, error):
            self._refresh_task = None
            if error is not None or not isinstance(result, dict):
                return
            selected_id = str((self.conversation or {}).get("id") or "")
            self.conversations = list(result.get("items") or [])
            self.total_unread_count = int(result.get("unread_count") or 0)
            if self.history_mode:
                self._populate_conversation_picker(selected_id=selected_id)
            else:
                self.conversation = next(
                    (
                        item
                        for item in self.conversations
                        if str(item.get("id") or "") == selected_id
                    ),
                    next(
                        (
                            item
                            for item in self.conversations
                            if item.get("status") == "open"
                        ),
                        self.conversations[0] if self.conversations else None,
                    ),
                )
            self._render()
            self._remember_admin_messages(notify=True)
            self.unread_count_changed.emit(
                self.total_unread_count
            )
            self._mark_current_conversation_read()

        self._refresh_task = start_api_task(
            lambda: self._fetch_conversations(**self._request_kwargs()),
            completed,
        )

    def _remember_admin_messages(self, *, notify):
        admin_message_ids = {
            str(message.get("id") or "")
            for conversation in self.conversations
            for message in conversation.get("messages") or []
            if message.get("sender_role") == "admin" and message.get("id")
        }
        new_ids = admin_message_ids - self._known_admin_message_ids
        should_notify = bool(notify and self._message_ids_initialized and new_ids)
        self._known_admin_message_ids = admin_message_ids
        self._message_ids_initialized = True
        if not should_notify:
            return
        self.notice_label.show()
        self.notice_label.raise_()
        application = QApplication.instance()
        if application is not None:
            application.alert(self, 0)
        QTimer.singleShot(5_000, self.notice_label.hide)

    def _mark_current_conversation_read(self):
        conversation = self.conversation
        if not conversation or not int(conversation.get("user_unread_count") or 0):
            return
        method = getattr(
            self.session.api,
            "mark_contact_conversation_read",
            None,
        )
        if not callable(method):
            return
        conversation_id = str(conversation.get("id") or "")
        if not conversation_id:
            return
        conversation_unread = int(conversation.get("user_unread_count") or 0)
        conversation["user_unread_count"] = 0
        for message in conversation.get("messages") or []:
            if message.get("sender_role") == "admin":
                message["user_read_at"] = message.get("user_read_at") or True
        self.total_unread_count = max(
            0,
            self.total_unread_count - conversation_unread,
        )
        self.unread_count_changed.emit(self.total_unread_count)
        self.messages_changed.emit()
        task = None

        def completed(_result, _error):
            if task in self._read_tasks:
                self._read_tasks.remove(task)

        task = start_api_task(
            lambda: method(
                self.session.access_token(),
                conversation_id,
            ),
            completed,
        )
        self._read_tasks.append(task)

    def _limit_message(self):
        text = self.message_edit.toPlainText()
        if len(text) > self.MAX_LENGTH:
            self.message_edit.setPlainText(text[: self.MAX_LENGTH])
            text = self.message_edit.toPlainText()
        self.count_label.setText(f"{len(text)} / {self.MAX_LENGTH}")

    def _send(self):
        message = self.message_edit.toPlainText().strip()
        if not message and (
            self.legacy_mode or not self.attachment_picker.has_attachments()
        ):
            QMessageBox.warning(self, "消息为空", "请输入文字或添加附件。")
            return
        attachments = []
        if not self.legacy_mode:
            try:
                attachments = self.attachment_picker.payloads()
            except OSError as exc:
                QMessageBox.warning(self, "附件读取失败", str(exc))
                return
        conversation = self.conversation
        announcement_id = str(
            self.announcement.get("id")
            or (conversation or {}).get("announcement_id")
            or ""
        )
        if self.legacy_mode:
            if not announcement_id:
                QMessageBox.warning(self, "消息操作失败", "当前会话没有关联公告。")
                return

            def action():
                return self.session.api.send_admin_message(
                    self.session.access_token(),
                    announcement_id,
                    message,
                )

        elif conversation and conversation.get("status") == "open":

            def action():
                return self.session.api.reply_admin_message(
                    self.session.access_token(),
                    str(conversation.get("id")),
                    message,
                    attachments,
                )

        else:
            if not announcement_id:
                QMessageBox.warning(self, "消息操作失败", "当前会话没有关联公告。")
                return

            def action():
                return self.session.api.send_admin_message(
                    self.session.access_token(),
                    announcement_id,
                    message,
                    attachments,
                )

        result = self._call("正在发送消息…", action)
        if result is None:
            return
        if self.legacy_mode:
            QMessageBox.information(self, "发送成功", "消息已发送给管理员。")
            self.accept()
            return
        self.conversation = result
        if self.history_mode:
            result_id = str(result.get("id") or "")
            self.conversations = [
                item
                for item in self.conversations
                if str(item.get("id") or "") != result_id
            ]
            self.conversations.insert(0, result)
            self._populate_conversation_picker(selected_id=result_id)
        self.message_edit.clear()
        self.attachment_picker.clear()
        self._render()
        self.messages_changed.emit()

    def _set_status(self, status):
        conversation = self.conversation
        if not conversation:
            return
        result = self._call(
            "正在更新会话状态…",
            lambda: self.session.api.update_contact_status(
                self.session.access_token(),
                str(conversation.get("id")),
                status,
            ),
        )
        if result is None:
            return
        self.conversation = result
        if self.history_mode:
            result_id = str(result.get("id") or "")
            self.conversations = [
                item
                for item in self.conversations
                if str(item.get("id") or "") != result_id
            ]
            self.conversations.insert(0, result)
            self._populate_conversation_picker(selected_id=result_id)
        self._render()
        self.messages_changed.emit()
        if status == "open":
            self.message_edit.setFocus()


class ContactHistoryDialog(ContactConversationDialog):
    """Show every conversation belonging to the current normal user."""

    def __init__(self, session, parent=None):
        super().__init__(None, session, parent, history_mode=True)


class AdminConversationDialog(FramelessDialog):
    """Independent administrator chat window for one user conversation."""

    MAX_LENGTH = 2_000
    conversation_updated = pyqtSignal(object)
    unread_count_changed = pyqtSignal(int)

    def __init__(self, conversation, session, parent=None):
        super().__init__(
            parent,
            resizable=True,
            show_minimize=True,
            show_maximize=True,
        )
        self.conversation = dict(conversation or {})
        self.session = session
        self._refresh_task = None
        self._read_tasks = []
        self._known_user_message_ids = set()
        self._message_ids_initialized = False
        self.setWindowTitle(
            f"回复用户 - {self.conversation.get('sender_display_name') or '用户'}"
        )
        self.setModal(False)
        self.resize(840, 700)
        self.setMinimumSize(680, 540)
        self.setAttribute(Qt.WA_DeleteOnClose, True)

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 46, 22, 20)
        root.setSpacing(12)
        header = QFrame()
        header.setObjectName("ChatHeader")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(16, 13, 16, 13)
        header_layout.setSpacing(12)
        avatar_text = str(
            self.conversation.get("sender_display_name")
            or self.conversation.get("sender_username")
            or "用"
        )[:1]
        avatar = QLabel(avatar_text)
        avatar.setObjectName("ChatAvatar")
        avatar.setAlignment(Qt.AlignCenter)
        avatar.setFixedSize(42, 42)
        header_layout.addWidget(avatar)
        heading_box = QVBoxLayout()
        heading_box.setContentsMargins(0, 0, 0, 0)
        heading_box.setSpacing(3)
        self.user_label = QLabel()
        self.user_label.setObjectName("PageTitle")
        heading_box.addWidget(self.user_label)
        self.related_label = QLabel()
        self.related_label.setObjectName("Muted")
        self.related_label.setWordWrap(True)
        heading_box.addWidget(self.related_label)
        header_layout.addLayout(heading_box, 1)
        self.notice_label = QLabel("收到用户新消息")
        self.notice_label.setObjectName("ChatNewMessageNotice")
        self.notice_label.hide()
        header_layout.addWidget(self.notice_label)
        root.addWidget(header)

        self.status_label = QLabel()
        self.status_label.setObjectName("ChatStatus")
        root.addWidget(self.status_label)
        self.history = ConversationTimeline(
            lambda attachment: _open_contact_attachment(
                self,
                self.session,
                attachment,
            ),
            self,
        )
        root.addWidget(self.history, 1)

        composer = QFrame()
        composer.setObjectName("ChatComposerFrame")
        composer_layout = QVBoxLayout(composer)
        composer_layout.setContentsMargins(12, 10, 12, 10)
        composer_layout.setSpacing(7)
        self.message_edit = QPlainTextEdit()
        self.message_edit.setObjectName("ChatComposer")
        self.message_edit.setMinimumHeight(82)
        self.message_edit.setMaximumHeight(116)
        self.message_edit.setPlaceholderText("输入回复内容…")
        self.message_edit.textChanged.connect(self._limit_message)
        composer_layout.addWidget(self.message_edit)
        self.attachment_picker = ContactAttachmentPicker(self)
        composer_layout.addWidget(self.attachment_picker)
        self.count_label = QLabel(f"0 / {self.MAX_LENGTH}")
        self.count_label.setObjectName("Muted")
        self.count_label.setAlignment(Qt.AlignRight)
        composer_layout.addWidget(self.count_label)
        root.addWidget(composer)

        actions = QHBoxLayout()
        actions.addStretch()
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.reject)
        self.send_btn = QPushButton("发送回复")
        self.send_btn.setObjectName("PrimaryButton")
        self.send_btn.clicked.connect(self._send)
        actions.addWidget(close_btn)
        actions.addWidget(self.send_btn)
        root.addLayout(actions)

        self._render()
        self._remember_user_messages(notify=False)
        self._mark_read()
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(12_000)
        self._refresh_timer.timeout.connect(self._refresh_silently)
        self._refresh_timer.start()
        QTimer.singleShot(0, self.message_edit.setFocus)

    def _call(self, message, function):
        try:
            return run_with_loading(self, message, function)
        except (ApiResponseError, NetworkUnavailable, OSError, ValueError) as exc:
            QMessageBox.warning(self, "消息操作失败", str(exc))
            return None

    def _render(self):
        display_name = str(
            self.conversation.get("sender_display_name")
            or self.conversation.get("sender_username")
            or "用户"
        )
        username = str(self.conversation.get("sender_username") or "-")
        self.user_label.setText(display_name)
        self.related_label.setText(
            f"账号：{username}    关联公告："
            f"{self.conversation.get('announcement_title') or '公告已删除'}"
        )
        status = str(self.conversation.get("status") or "open")
        self.status_label.setText(
            "用户已结束该会话，仍可发送补充回复。"
            if status == "resolved"
            else "会话处理中"
        )
        self.history.set_messages(
            self.conversation.get("messages") or [],
            own_role="admin",
            peer_label=display_name,
        )

    def _limit_message(self):
        text = self.message_edit.toPlainText()
        if len(text) > self.MAX_LENGTH:
            cursor = self.message_edit.textCursor()
            position = min(cursor.position(), self.MAX_LENGTH)
            self.message_edit.setPlainText(text[: self.MAX_LENGTH])
            cursor = self.message_edit.textCursor()
            cursor.setPosition(position)
            self.message_edit.setTextCursor(cursor)
            text = self.message_edit.toPlainText()
        self.count_label.setText(f"{len(text)} / {self.MAX_LENGTH}")

    def _send(self):
        message = self.message_edit.toPlainText().strip()
        if not message and not self.attachment_picker.has_attachments():
            QMessageBox.warning(self, "回复为空", "请输入文字或添加附件。")
            return
        try:
            attachments = self.attachment_picker.payloads()
        except OSError as exc:
            QMessageBox.warning(self, "附件读取失败", str(exc))
            return
        conversation_id = str(self.conversation.get("id") or "")
        result = self._call(
            "正在发送回复…",
            lambda: self.session.api.admin_reply_message(
                self.session.access_token(),
                conversation_id,
                message,
                attachments,
            ),
        )
        if result is None:
            return
        if isinstance(result, dict) and result.get("messages") is not None:
            self.conversation = result
        else:
            local_message = {
                "id": f"local-{len(self.conversation.get('messages') or [])}",
                "sender_role": "admin",
                "message": message,
                "attachments": attachments,
                "created_at": "刚刚",
            }
            self.conversation.setdefault("messages", []).append(local_message)
            self.conversation["last_message"] = local_message
        self.message_edit.clear()
        self.attachment_picker.clear()
        self._render()
        self.conversation_updated.emit(self.conversation)

    def _mark_read(self):
        if not int(self.conversation.get("unread_count") or 0):
            return
        conversation_id = str(self.conversation.get("id") or "")
        if not conversation_id:
            return
        self.conversation["unread_count"] = 0
        self.conversation["read_at"] = self.conversation.get("read_at") or True
        self.conversation_updated.emit(self.conversation)
        task = None

        def completed(_result, _error):
            if task in self._read_tasks:
                self._read_tasks.remove(task)

        task = start_api_task(
            lambda: self.session.api.admin_mark_message_read(
                self.session.access_token(),
                conversation_id,
            ),
            completed,
        )
        self._read_tasks.append(task)

    def _refresh_silently(self):
        if self._refresh_task is not None or not self.isVisible():
            return
        conversation_id = str(self.conversation.get("id") or "")

        def completed(result, error):
            self._refresh_task = None
            if error is not None or not isinstance(result, dict):
                return
            refreshed = next(
                (
                    item
                    for item in result.get("items") or []
                    if str(item.get("id") or "") == conversation_id
                ),
                None,
            )
            if refreshed is None:
                return
            self.conversation = refreshed
            self._render()
            self._remember_user_messages(notify=True)
            self.unread_count_changed.emit(
                max(
                    0,
                    int(result.get("unread_count") or 0)
                    - int(refreshed.get("unread_count") or 0),
                )
            )
            self._mark_read()

        self._refresh_task = start_api_task(
            lambda: self.session.api.admin_messages(
                self.session.access_token(),
                limit=200,
            ),
            completed,
        )

    def _remember_user_messages(self, *, notify):
        user_message_ids = {
            str(message.get("id") or "")
            for message in self.conversation.get("messages") or []
            if message.get("sender_role") == "user" and message.get("id")
        }
        new_ids = user_message_ids - self._known_user_message_ids
        should_notify = bool(notify and self._message_ids_initialized and new_ids)
        self._known_user_message_ids = user_message_ids
        self._message_ids_initialized = True
        if not should_notify:
            return
        self.notice_label.show()
        application = QApplication.instance()
        if application is not None:
            application.alert(self, 0)
        QTimer.singleShot(5_000, self.notice_label.hide)


class ImagePreviewDialog(FramelessDialog):
    def __init__(self, data, file_name, parent=None, *, save_caption="保存公告图片"):
        super().__init__(
            parent,
            resizable=True,
            show_minimize=True,
            show_maximize=True,
        )
        self.data = bytes(data)
        self.file_name = str(file_name or "公告图片")
        self.save_caption = str(save_caption or "保存图片")
        self.setWindowTitle(self.file_name)
        self.resize(900, 700)
        self.setMinimumSize(620, 460)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 46, 24, 22)
        layout.setSpacing(12)
        title = QLabel(self.file_name)
        title.setObjectName("PageTitle")
        layout.addWidget(title)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setAlignment(Qt.AlignCenter)
        image = QLabel()
        image.setAlignment(Qt.AlignCenter)
        pixmap = QPixmap()
        if pixmap.loadFromData(self.data):
            image.setPixmap(pixmap)
            image.resize(pixmap.size())
        else:
            image.setText("无法预览该图片格式")
        scroll.setWidget(image)
        layout.addWidget(scroll, 1)

        buttons = QHBoxLayout()
        buttons.addStretch()
        save_btn = QPushButton("保存图片")
        save_btn.clicked.connect(self._save)
        close_btn = QPushButton("关闭")
        close_btn.setObjectName("PrimaryButton")
        close_btn.clicked.connect(self.accept)
        buttons.addWidget(save_btn)
        buttons.addWidget(close_btn)
        layout.addLayout(buttons)

    def _save(self):
        target, _ = QFileDialog.getSaveFileName(
            self,
            self.save_caption,
            self.file_name,
            "所有文件 (*.*)",
        )
        if not target:
            return
        try:
            Path(target).write_bytes(self.data)
        except OSError as exc:
            QMessageBox.warning(self, "保存失败", str(exc))


class AnnouncementDetailDialog(FramelessDialog):
    read_confirmed = pyqtSignal()
    contact_requested = pyqtSignal(object)

    def __init__(self, announcement, session, account, parent=None):
        super().__init__(
            parent,
            resizable=True,
            show_minimize=True,
            show_maximize=True,
        )
        self.announcement = announcement
        self.session = session
        self.account = account
        self.setWindowTitle(str(announcement.get("title") or "公告详情"))
        self.setModal(True)
        self.resize(900, 720)
        self.setMinimumSize(700, 540)

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 46, 28, 24)
        root.setSpacing(12)
        title = QLabel(str(announcement.get("title") or "公告详情"))
        title.setObjectName("PageTitle")
        title.setWordWrap(True)
        root.addWidget(title)
        meta = QLabel(
            f"发布人：{announcement.get('created_by_name') or '管理员'}"
            f"    发布时间：{_display_time(announcement.get('created_at'))}"
        )
        meta.setObjectName("Muted")
        root.addWidget(meta)

        body = QTextBrowser()
        body.setObjectName("AnnouncementBody")
        body.setOpenExternalLinks(False)
        body.setHtml(str(announcement.get("body_html") or ""))
        root.addWidget(body, 1)

        attachments = list(announcement.get("attachments") or [])
        if attachments:
            attachment_group = QGroupBox(f"附件（{len(attachments)}）")
            attachment_layout = QVBoxLayout(attachment_group)
            for attachment in attachments:
                kind = "图片" if attachment.get("kind") == "image" else "文件"
                button = QPushButton(
                    f"{kind}  ·  {attachment.get('file_name') or '附件'}"
                    f"  ({_format_size(attachment.get('size'))})"
                )
                button.setToolTip("点击预览图片" if kind == "图片" else "点击下载文件")
                button.clicked.connect(
                    lambda _checked=False, item=attachment: self._open_attachment(item)
                )
                attachment_layout.addWidget(button)
            root.addWidget(attachment_group)

        buttons = QHBoxLayout()
        if not account.is_admin:
            contact_btn = QPushButton("联系管理员")
            contact_btn.setToolTip("查看历史会话、继续回复或发起新会话")
            contact_btn.clicked.connect(self._contact_admin)
            buttons.addWidget(contact_btn)
        buttons.addStretch()
        close_btn = QPushButton("关闭弹窗")
        close_btn.clicked.connect(self.accept)
        buttons.addWidget(close_btn)
        if not account.is_admin and not announcement.get("read_at"):
            confirm_btn = QPushButton("确认已读")
            confirm_btn.setObjectName("PrimaryButton")
            confirm_btn.clicked.connect(self._confirm_and_close)
            buttons.addWidget(confirm_btn)
        else:
            close_btn.setObjectName("PrimaryButton")
        root.addLayout(buttons)

    def _download(self, attachment):
        if self.session is None:
            raise NetworkUnavailable("当前未连接公告服务")
        return self.session.api.download_announcement_attachment(
            self.session.access_token(),
            str(attachment.get("id")),
        )

    def _open_attachment(self, attachment):
        file_name = str(attachment.get("file_name") or "公告附件")
        if attachment.get("kind") == "image":
            try:
                data = run_with_loading(
                    self,
                    "正在加载公告图片…",
                    lambda: self._download(attachment),
                )
            except (ApiResponseError, NetworkUnavailable, OSError) as exc:
                QMessageBox.warning(self, "图片加载失败", str(exc))
                return
            ImagePreviewDialog(data, file_name, self).exec_()
            return

        target, _ = QFileDialog.getSaveFileName(
            self,
            "保存公告附件",
            file_name,
            "所有文件 (*.*)",
        )
        if not target:
            return
        try:
            data = run_with_loading(
                self,
                "正在下载公告附件…",
                lambda: self._download(attachment),
            )
            destination = Path(target)
            temporary = destination.with_name(f".{destination.name}.tmp")
            try:
                temporary.write_bytes(data)
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
        except (ApiResponseError, NetworkUnavailable, OSError) as exc:
            QMessageBox.warning(self, "附件保存失败", str(exc))
            return
        QMessageBox.information(self, "保存完成", f"附件已保存到：\n{target}")

    def _contact_admin(self):
        self.accept()
        self.contact_requested.emit(self.announcement)

    def _confirm_and_close(self):
        if not self.account.is_admin and not self.announcement.get("read_at"):
            self.announcement["read_at"] = True
            self.read_confirmed.emit()
        self.accept()


class AnnouncementEditorDialog(FramelessDialog):
    def __init__(self, accounts, announcement=None, parent=None):
        super().__init__(
            parent,
            resizable=True,
            show_minimize=True,
            show_maximize=True,
        )
        self.accounts = [
            account
            for account in accounts
            if account.get("role") == "user"
            and account.get("is_active", True)
            and not account.get("is_archived", False)
        ]
        self.announcement = announcement
        self._removed_attachment_ids = set()
        self._pending_attachments = []
        self.setWindowTitle("编辑公告" if announcement else "发布公告")
        self.setModal(True)
        self.resize(1050, 820)
        self.setMinimumSize(820, 650)

        root = QVBoxLayout(self)
        root.setContentsMargins(26, 44, 26, 22)
        root.setSpacing(11)
        heading = QLabel("编辑公告" if announcement else "发布新公告")
        heading.setObjectName("PageTitle")
        root.addWidget(heading)

        fields = QGridLayout()
        fields.setHorizontalSpacing(12)
        fields.setVerticalSpacing(9)
        fields.addWidget(QLabel("公告标题"), 0, 0)
        self.title_edit = QLineEdit()
        self.title_edit.setAttribute(Qt.WA_InputMethodEnabled, True)
        self.title_edit.setInputMethodHints(Qt.ImhNone)
        self.title_edit.setMaxLength(200)
        self.title_edit.setPlaceholderText("请输入公告标题")
        fields.addWidget(self.title_edit, 0, 1)
        fields.addWidget(QLabel("轮播文字"), 1, 0)
        self.ticker_edit = QLineEdit()
        self.ticker_edit.setAttribute(Qt.WA_InputMethodEnabled, True)
        self.ticker_edit.setInputMethodHints(Qt.ImhNone)
        self.ticker_edit.setMaxLength(500)
        self.ticker_edit.setPlaceholderText("显示在顶部喇叭旁的简短内容")
        fields.addWidget(self.ticker_edit, 1, 1)
        fields.setColumnStretch(1, 1)
        root.addLayout(fields)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(6)
        self.font_size_combo = QComboBox()
        for size in (10, 12, 14, 16, 18, 22, 28, 36):
            self.font_size_combo.addItem(f"{size} pt", size)
        self.font_size_combo.setCurrentIndex(2)
        self.font_size_combo.setToolTip("字号")
        self.font_size_combo.currentIndexChanged.connect(self._set_font_size)
        toolbar.addWidget(self.font_size_combo)
        self.toolbar_buttons = {}
        self._add_text_tool(
            toolbar,
            "bold",
            "B",
            "加粗",
            self._toggle_bold,
            checkable=True,
            font_style="bold",
        )
        self._add_text_tool(
            toolbar,
            "italic",
            "I",
            "斜体",
            self._toggle_italic,
            checkable=True,
            font_style="italic",
        )
        self._add_text_tool(
            toolbar,
            "underline",
            "U",
            "下划线",
            self._toggle_underline,
            checkable=True,
            font_style="underline",
        )
        self._add_text_tool(
            toolbar,
            "color",
            "A",
            "文字颜色",
            self._choose_color,
        )
        self._add_icon_tool(
            toolbar,
            "align_left",
            "toolbar-align-left.svg",
            "左对齐",
            lambda _checked=False: self.body_edit.setAlignment(Qt.AlignLeft),
        )
        self._add_icon_tool(
            toolbar,
            "align_center",
            "toolbar-align-center.svg",
            "居中",
            lambda _checked=False: self.body_edit.setAlignment(Qt.AlignCenter),
        )
        self._add_icon_tool(
            toolbar,
            "align_right",
            "toolbar-align-right.svg",
            "右对齐",
            lambda _checked=False: self.body_edit.setAlignment(Qt.AlignRight),
        )
        self._add_icon_tool(
            toolbar,
            "bullets",
            "toolbar-bullets.svg",
            "项目符号",
            self._insert_bullets,
        )
        self._add_text_tool(
            toolbar,
            "clear",
            "Tx",
            "清除格式",
            self._clear_format,
        )
        toolbar.addStretch()
        root.addLayout(toolbar)

        self.body_edit = QTextEdit()
        self.body_edit.setAttribute(Qt.WA_InputMethodEnabled, True)
        self.body_edit.setInputMethodHints(Qt.ImhNone)
        self.body_edit.setAcceptRichText(True)
        self.body_edit.setPlaceholderText("请输入公告正文，可使用上方工具设置文字格式…")
        root.addWidget(self.body_edit, 1)

        options = QHBoxLayout()
        self.all_users_checkbox = QCheckBox("全部普通用户可见")
        self.all_users_checkbox.setChecked(True)
        self.all_users_checkbox.toggled.connect(self._all_users_toggled)
        self.show_on_startup_checkbox = QCheckBox("此次公告开屏展示")
        options.addWidget(self.all_users_checkbox)
        options.addWidget(self.show_on_startup_checkbox)
        options.addStretch()
        root.addLayout(options)

        self.target_list = QListWidget()
        self.target_list.setObjectName("AnnouncementChoiceList")
        self.target_list.setMaximumHeight(120)
        self.target_list.setAlternatingRowColors(True)
        self.target_list.setSpacing(2)
        self.target_list.setSelectionMode(QAbstractItemView.NoSelection)
        for account in self.accounts:
            item = QListWidgetItem(
                f"{account.get('display_name') or account.get('username')}"
                f"  ·  {account.get('username')}"
            )
            item.setData(Qt.UserRole, str(account.get("id")))
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Unchecked)
            item.setSizeHint(QSize(0, 32))
            self.target_list.addItem(item)
        self.target_list.setEnabled(False)
        root.addWidget(self.target_list)

        attachment_group = QGroupBox("图片与文件附件（单个不超过 10 MB，最多 8 个）")
        attachment_layout = QVBoxLayout(attachment_group)
        attachment_actions = QHBoxLayout()
        add_image_btn = QPushButton("＋ 添加图片")
        add_image_btn.clicked.connect(self._add_images)
        add_file_btn = QPushButton("＋ 添加文件")
        add_file_btn.clicked.connect(self._add_files)
        remove_btn = QPushButton("移除所选")
        remove_btn.clicked.connect(self._remove_selected_attachment)
        attachment_actions.addWidget(add_image_btn)
        attachment_actions.addWidget(add_file_btn)
        attachment_actions.addWidget(remove_btn)
        attachment_actions.addStretch()
        attachment_layout.addLayout(attachment_actions)
        self.attachment_list = QListWidget()
        self.attachment_list.setObjectName("AnnouncementAttachmentList")
        self.attachment_list.setMaximumHeight(118)
        self.attachment_list.setAlternatingRowColors(True)
        self.attachment_list.setSpacing(2)
        attachment_layout.addWidget(self.attachment_list)
        root.addWidget(attachment_group)

        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        publish_btn = QPushButton("保存并发布" if not announcement else "保存修改")
        publish_btn.setObjectName("PrimaryButton")
        publish_btn.clicked.connect(self._validate_and_accept)
        buttons.addWidget(cancel_btn)
        buttons.addWidget(publish_btn)
        root.addLayout(buttons)

        if announcement:
            self._load_announcement(announcement)

    def _new_tool_button(self, name, tooltip, slot, *, checkable=False):
        button = QToolButton()
        button.setObjectName("RichTextToolButton")
        button.setAccessibleName(tooltip)
        button.setToolTip(tooltip)
        button.setCheckable(checkable)
        button.setFixedSize(36, 32)
        button.setIconSize(QSize(18, 18))
        button.clicked.connect(slot)
        self.toolbar_buttons[name] = button
        return button

    def _add_text_tool(
        self,
        layout,
        name,
        text,
        tooltip,
        slot,
        *,
        checkable=False,
        font_style="",
    ):
        button = self._new_tool_button(
            name,
            tooltip,
            slot,
            checkable=checkable,
        )
        button.setToolButtonStyle(Qt.ToolButtonTextOnly)
        button.setText(text)
        font = QFont("Segoe UI", 11)
        font.setBold(font_style == "bold")
        font.setItalic(font_style == "italic")
        font.setUnderline(font_style == "underline")
        button.setFont(font)
        layout.addWidget(button)

    def _add_icon_tool(self, layout, name, icon_name, tooltip, slot):
        button = self._new_tool_button(name, tooltip, slot)
        button.setToolButtonStyle(Qt.ToolButtonIconOnly)
        button.setIcon(QIcon(str(ASSET_DIRECTORY / icon_name)))
        layout.addWidget(button)

    def _load_announcement(self, announcement):
        self.title_edit.setText(str(announcement.get("title") or ""))
        self.ticker_edit.setText(str(announcement.get("ticker_text") or ""))
        self.body_edit.setHtml(str(announcement.get("body_html") or ""))
        self.show_on_startup_checkbox.setChecked(
            bool(announcement.get("show_on_startup"))
        )
        targets = {
            str(account_id)
            for account_id in announcement.get("target_account_ids") or []
        }
        self.all_users_checkbox.setChecked(not targets)
        for index in range(self.target_list.count()):
            item = self.target_list.item(index)
            item.setCheckState(
                Qt.Checked if str(item.data(Qt.UserRole)) in targets else Qt.Unchecked
            )
        for attachment in announcement.get("attachments") or []:
            self._append_attachment_item(
                {
                    "source": "existing",
                    "id": str(attachment.get("id")),
                    "file_name": attachment.get("file_name"),
                    "kind": attachment.get("kind"),
                    "content_type": attachment.get("content_type"),
                    "size": int(attachment.get("size") or 0),
                }
            )

    def _all_users_toggled(self, checked):
        self.target_list.setEnabled(not checked)

    def _set_font_size(self, *_args):
        size = self.font_size_combo.currentData()
        if size:
            self.body_edit.setFontPointSize(float(size))

    def _toggle_bold(self, checked):
        self.body_edit.setFontWeight(QFont.Bold if checked else QFont.Normal)

    def _toggle_italic(self, checked):
        self.body_edit.setFontItalic(bool(checked))

    def _toggle_underline(self, checked):
        self.body_edit.setFontUnderline(bool(checked))

    def _choose_color(self, *_args):
        color = QColorDialog.getColor(QColor("#173a3d"), self, "选择文字颜色")
        if color.isValid():
            self.body_edit.setTextColor(color)

    def _insert_bullets(self, *_args):
        cursor = self.body_edit.textCursor()
        cursor.createList(QTextListFormat.ListDisc)

    def _clear_format(self, *_args):
        cursor = self.body_edit.textCursor()
        cursor.setCharFormat(QTextCharFormat())
        self.body_edit.setTextCursor(cursor)

    def _attachment_total(self):
        total = 0
        for index in range(self.attachment_list.count()):
            data = self.attachment_list.item(index).data(Qt.UserRole) or {}
            total += int(data.get("size") or 0)
        return total

    def _append_attachment_item(self, data):
        kind = "图片" if data.get("kind") == "image" else "文件"
        item = QListWidgetItem(
            f"{kind}  ·  {data.get('file_name')}  ({_format_size(data.get('size'))})"
        )
        item.setData(Qt.UserRole, dict(data))
        item.setSizeHint(QSize(0, 34))
        self.attachment_list.addItem(item)

    def _add_images(self, *_args):
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "选择公告图片",
            "",
            "图片文件 (*.png *.jpg *.jpeg *.gif *.bmp *.webp);;所有文件 (*.*)",
        )
        self._add_paths(paths, "image")

    def _add_files(self, *_args):
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "选择公告附件",
            "",
            "所有文件 (*.*)",
        )
        self._add_paths(paths, "file")

    def _add_paths(self, paths, kind):
        for raw_path in paths:
            if self.attachment_list.count() >= MAX_ATTACHMENTS:
                QMessageBox.warning(
                    self,
                    "附件数量超限",
                    f"单个公告最多上传 {MAX_ATTACHMENTS} 个附件。",
                )
                return
            path = Path(raw_path)
            try:
                size = path.stat().st_size
            except OSError as exc:
                QMessageBox.warning(self, "无法读取附件", str(exc))
                continue
            if size > MAX_ATTACHMENT_BYTES:
                QMessageBox.warning(
                    self,
                    "附件过大",
                    f"{path.name} 超过 10 MB，未添加。",
                )
                continue
            if self._attachment_total() + size > MAX_ANNOUNCEMENT_ATTACHMENT_BYTES:
                QMessageBox.warning(
                    self,
                    "附件总大小超限",
                    "单个公告的附件总大小不能超过 25 MB。",
                )
                return
            content_type = (
                mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            )
            actual_kind = (
                "image"
                if kind == "image" and content_type.startswith("image/")
                else "file"
            )
            data = {
                "source": "pending",
                "path": str(path.resolve()),
                "file_name": path.name,
                "kind": actual_kind,
                "content_type": content_type,
                "size": size,
            }
            self._pending_attachments.append(data)
            self._append_attachment_item(data)

    def _remove_selected_attachment(self, *_args):
        row = self.attachment_list.currentRow()
        if row < 0:
            return
        item = self.attachment_list.takeItem(row)
        data = item.data(Qt.UserRole) or {}
        if data.get("source") == "existing":
            self._removed_attachment_ids.add(str(data.get("id")))
        else:
            path = str(data.get("path") or "")
            self._pending_attachments = [
                current
                for current in self._pending_attachments
                if str(current.get("path") or "") != path
            ]

    def _target_account_ids(self):
        if self.all_users_checkbox.isChecked():
            return []
        return [
            str(item.data(Qt.UserRole))
            for index in range(self.target_list.count())
            if (item := self.target_list.item(index)).checkState() == Qt.Checked
        ]

    def _validate_and_accept(self, *_args):
        if not self.title_edit.text().strip():
            QMessageBox.warning(self, "缺少标题", "请输入公告标题。")
            return
        if not self.ticker_edit.text().strip():
            QMessageBox.warning(self, "缺少轮播文字", "请输入顶部轮播文字。")
            return
        if not self.body_edit.toPlainText().strip():
            QMessageBox.warning(self, "缺少正文", "请输入公告正文。")
            return
        if not self.all_users_checkbox.isChecked() and not self._target_account_ids():
            QMessageBox.warning(
                self,
                "未选择接收用户",
                "请选择至少一个普通用户，或勾选“全部普通用户可见”。",
            )
            return
        self.accept()

    def announcement_payload(self):
        return {
            "title": self.title_edit.text().strip(),
            "ticker_text": self.ticker_edit.text().strip(),
            "body_html": self.body_edit.toHtml(),
            "show_on_startup": self.show_on_startup_checkbox.isChecked(),
            "target_account_ids": self._target_account_ids(),
        }

    def pending_attachments(self):
        return [dict(item) for item in self._pending_attachments]

    def removed_attachment_ids(self):
        return set(self._removed_attachment_ids)

    @staticmethod
    def encode_attachment(item):
        path = Path(item["path"])
        return {
            "file_name": item["file_name"],
            "content_type": item["content_type"],
            "kind": item["kind"],
            "content_base64": base64.b64encode(path.read_bytes()).decode("ascii"),
        }


class AnnouncementAdminPage(QWidget):
    announcements_changed = pyqtSignal()
    unread_messages_changed = pyqtSignal(int)

    def __init__(self, session, parent=None):
        super().__init__(parent)
        self.session = session
        self.announcements = []
        self.accounts = []
        self.messages = []
        self._message_read_tasks = []
        self._chat_windows = {}
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 22)
        root.setSpacing(14)
        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title = QLabel("公告发布")
        title.setObjectName("PageTitle")
        subtitle = QLabel("发布富文本公告、上传图片和文件，并集中查看普通用户消息。")
        subtitle.setObjectName("Muted")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addLayout(title_box)
        header.addStretch()
        self.refresh_btn = QPushButton("刷新公告与消息")
        self.refresh_btn.clicked.connect(self.refresh)
        header.addWidget(self.refresh_btn)
        root.addLayout(header)

        self.tabs = QTabWidget()
        self.tabs.setObjectName("AnnouncementTabs")
        root.addWidget(self.tabs, 1)

        management = QWidget()
        management_layout = QVBoxLayout(management)
        management_layout.setContentsMargins(10, 12, 10, 10)
        management_layout.setSpacing(10)
        actions = QHBoxLayout()
        self.create_btn = QPushButton("＋ 发布公告")
        self.create_btn.setObjectName("PrimaryButton")
        self.create_btn.clicked.connect(self._create)
        self.edit_btn = QPushButton("编辑公告")
        self.edit_btn.clicked.connect(self._edit)
        self.toggle_btn = QPushButton("停用/重新启用")
        self.toggle_btn.clicked.connect(self._toggle)
        self.delete_btn = QPushButton("删除公告")
        self.delete_btn.setObjectName("DangerButton")
        self.delete_btn.clicked.connect(self._delete)
        actions.addWidget(self.create_btn)
        actions.addStretch()
        actions.addWidget(self.edit_btn)
        actions.addWidget(self.toggle_btn)
        actions.addWidget(self.delete_btn)
        management_layout.addLayout(actions)

        splitter = QSplitter(Qt.Vertical)
        self.announcement_table = QTableWidget(0, 8)
        self.announcement_table.setHorizontalHeaderLabels(
            ["标题", "接收范围", "已读/未读", "开屏", "状态", "附件", "版本", "更新时间"]
        )
        self.announcement_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.announcement_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.announcement_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._style_management_table(
            self.announcement_table,
            "AnnouncementManagementTable",
        )
        self.announcement_table.itemSelectionChanged.connect(
            self._announcement_selection_changed
        )
        header_view = self.announcement_table.horizontalHeader()
        header_view.setSectionResizeMode(0, QHeaderView.Stretch)
        header_view.setSectionResizeMode(1, QHeaderView.Stretch)
        for column in range(2, 8):
            header_view.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        splitter.addWidget(self.announcement_table)
        self.announcement_preview = QTextBrowser()
        self.announcement_preview.setOpenExternalLinks(False)
        self.announcement_preview.setPlaceholderText("选择公告后在这里预览正文。")
        splitter.addWidget(self.announcement_preview)
        self.announcement_receipt_detail = QPlainTextEdit()
        self.announcement_receipt_detail.setReadOnly(True)
        self.announcement_receipt_detail.setMaximumHeight(130)
        self.announcement_receipt_detail.setPlaceholderText(
            "选择公告后查看已读与未读用户名单。"
        )
        splitter.addWidget(self.announcement_receipt_detail)
        splitter.setSizes([380, 220, 120])
        management_layout.addWidget(splitter, 1)
        self.tabs.addTab(management, "公告管理")

        inbox = QWidget()
        inbox_layout = QVBoxLayout(inbox)
        inbox_layout.setContentsMargins(10, 12, 10, 10)
        inbox_layout.setSpacing(10)
        inbox_actions = QHBoxLayout()
        self.inbox_summary = QLabel("未读消息：0")
        self.inbox_summary.setStyleSheet("font-weight:700;color:#264b4c;")
        inbox_actions.addWidget(self.inbox_summary)
        inbox_actions.addStretch()
        self.admin_reply_btn = QPushButton("回复用户")
        self.admin_reply_btn.setObjectName("PrimaryButton")
        self.admin_reply_btn.clicked.connect(self._reply_message)
        inbox_actions.addWidget(self.admin_reply_btn)
        self.delete_message_btn = QPushButton("删除所选消息")
        self.delete_message_btn.setObjectName("DangerButton")
        self.delete_message_btn.clicked.connect(self._delete_message)
        inbox_actions.addWidget(self.delete_message_btn)
        inbox_layout.addLayout(inbox_actions)

        self.message_table = QTableWidget(0, 6)
        self.message_table.setHorizontalHeaderLabels(
            ["未读", "会话状态", "发送用户", "关联公告", "最新消息", "更新时间"]
        )
        self.message_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.message_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.message_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._style_management_table(
            self.message_table,
            "AnnouncementMessageTable",
        )
        self.message_table.itemSelectionChanged.connect(self._message_selection_changed)
        self.message_table.itemDoubleClicked.connect(
            lambda _item: self._open_selected_conversation()
        )
        message_header = self.message_table.horizontalHeader()
        message_header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        message_header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        message_header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        message_header.setSectionResizeMode(3, QHeaderView.Stretch)
        message_header.setSectionResizeMode(4, QHeaderView.Stretch)
        message_header.setSectionResizeMode(5, QHeaderView.ResizeToContents)
        inbox_layout.addWidget(self.message_table, 1)
        self.tabs.addTab(inbox, "用户消息")
        self._selection_state()

    @staticmethod
    def _style_management_table(table, object_name):
        table.setObjectName(object_name)
        table.setAlternatingRowColors(True)
        table.setShowGrid(False)
        table.setWordWrap(False)
        table.verticalHeader().hide()
        table.verticalHeader().setDefaultSectionSize(42)
        table.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        table.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)

    def _call(self, message, function):
        if self.session is None:
            QMessageBox.warning(self, "公告服务不可用", "当前未连接在线公告服务。")
            return None
        try:
            return run_with_loading(self, message, function)
        except (ApiResponseError, NetworkUnavailable, OSError, ValueError) as exc:
            QMessageBox.warning(self, "公告操作失败", str(exc))
            return None

    def refresh(self):
        def load():
            token = self.session.access_token()
            return {
                "announcements": self.session.api.admin_announcements(token),
                "accounts": self.session.api.admin_accounts(token),
                "messages": self.session.api.admin_messages(token),
            }

        result = self._call("正在加载公告与消息…", load)
        if result is None:
            return
        self.announcements = list(result.get("announcements") or [])
        self.accounts = list(result.get("accounts") or [])
        message_result = result.get("messages") or {}
        self.messages = list(message_result.get("items") or [])
        self._fill_announcements()
        self._fill_messages(int(message_result.get("unread_count") or 0))

    def _fill_announcements(self):
        self.announcement_table.setRowCount(len(self.announcements))
        for row, announcement in enumerate(self.announcements):
            targets = list(announcement.get("targets") or [])
            scope = (
                "全部普通用户"
                if not targets
                else "、".join(
                    str(item.get("display_name") or item.get("username"))
                    for item in targets
                )
            )
            values = [
                str(announcement.get("title") or ""),
                scope,
                self._receipt_count_text(announcement),
                "是" if announcement.get("show_on_startup") else "否",
                "已启用" if announcement.get("is_active") else "已停用",
                str(len(announcement.get("attachments") or [])),
                str(announcement.get("revision") or 1),
                _display_time(announcement.get("updated_at")),
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.UserRole, str(announcement.get("id")))
                if column in {2, 3, 4, 5, 6}:
                    item.setTextAlignment(Qt.AlignCenter)
                if column == 4:
                    item.setForeground(
                        QColor(
                            "#16806f"
                            if announcement.get("is_active")
                            else "#8b5b5b"
                        )
                    )
                self.announcement_table.setItem(row, column, item)
        self.announcement_table.clearSelection()
        self.announcement_preview.clear()
        self.announcement_receipt_detail.clear()
        self._selection_state()

    @staticmethod
    def _receipt_count_text(announcement):
        if "read_count" not in announcement or "unread_count" not in announcement:
            return "- / -"
        return (
            f"{int(announcement.get('read_count') or 0)} / "
            f"{int(announcement.get('unread_count') or 0)}"
        )

    def _fill_messages(self, unread_count):
        self.message_table.setRowCount(len(self.messages))
        for row, message in enumerate(self.messages):
            latest = message.get("last_message") or {}
            summary = str(
                latest.get("message") or message.get("message") or ""
            ).replace("\n", " ")
            if not summary and latest.get("attachments"):
                first_attachment = latest["attachments"][0]
                summary = f"[附件] {first_attachment.get('file_name') or '文件'}"
            if len(summary) > 80:
                summary = summary[:77] + "…"
            values = [
                str(int(message.get("unread_count") or 0)),
                "已解决" if message.get("status") == "resolved" else "处理中",
                (
                    f"{message.get('sender_display_name') or '-'}"
                    f" · {message.get('sender_username') or '-'}"
                ),
                str(message.get("announcement_title") or "公告已删除"),
                summary,
                _display_time(message.get("updated_at") or message.get("created_at")),
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.UserRole, str(message.get("id")))
                if column == 0:
                    item.setTextAlignment(Qt.AlignCenter)
                    if int(message.get("unread_count") or 0):
                        font = item.font()
                        font.setBold(True)
                        item.setFont(font)
                        item.setForeground(QColor("#d14f45"))
                self.message_table.setItem(row, column, item)
        self.inbox_summary.setText(f"未读消息：{unread_count}")
        self.unread_messages_changed.emit(unread_count)
        self.message_table.clearSelection()
        self._selection_state()

    def _selected_announcement(self):
        row = self.announcement_table.currentRow()
        if row < 0:
            return None
        item = self.announcement_table.item(row, 0)
        target_id = str(item.data(Qt.UserRole)) if item else ""
        return next(
            (
                announcement
                for announcement in self.announcements
                if str(announcement.get("id")) == target_id
            ),
            None,
        )

    def _selected_message(self):
        row = self.message_table.currentRow()
        if row < 0:
            return None
        item = self.message_table.item(row, 0)
        target_id = str(item.data(Qt.UserRole)) if item else ""
        return next(
            (
                message
                for message in self.messages
                if str(message.get("id")) == target_id
            ),
            None,
        )

    def _selection_state(self):
        selected = self._selected_announcement() is not None
        self.edit_btn.setEnabled(selected)
        self.toggle_btn.setEnabled(selected)
        self.delete_btn.setEnabled(selected)
        if hasattr(self, "delete_message_btn"):
            self.delete_message_btn.setEnabled(
                self._selected_message() is not None
            )
        if hasattr(self, "admin_reply_btn"):
            has_message = self._selected_message() is not None
            self.admin_reply_btn.setEnabled(has_message)

    def _announcement_selection_changed(self):
        announcement = self._selected_announcement()
        self._selection_state()
        self.announcement_preview.setHtml(
            str(announcement.get("body_html") or "") if announcement else ""
        )
        if not announcement:
            self.announcement_receipt_detail.clear()
            return
        if "read_users" not in announcement or "unread_users" not in announcement:
            self.announcement_receipt_detail.setPlainText("当前服务未返回阅读统计。")
            return
        read_users = list(announcement.get("read_users") or [])
        unread_users = list(announcement.get("unread_users") or [])

        def display(item):
            return str(item.get("display_name") or item.get("username") or "-")

        self.announcement_receipt_detail.setPlainText(
            f"已读用户（{len(read_users)}）："
            f"{('、'.join(display(item) for item in read_users) or '无')}\n\n"
            f"未读用户（{len(unread_users)}）："
            f"{('、'.join(display(item) for item in unread_users) or '无')}"
        )

    def _message_selection_changed(self):
        message = self._selected_message()
        self._selection_state()
        if message and int(message.get("unread_count") or 0):
            self._mark_message_read(message)

    def _create(self):
        dialog = AnnouncementEditorDialog(self.accounts, parent=self)
        if dialog.exec_() != dialog.Accepted:
            return
        payload = dialog.announcement_payload()
        pending = dialog.pending_attachments()

        def create():
            token = self.session.access_token()
            payload["attachments"] = [
                dialog.encode_attachment(item) for item in pending
            ]
            return self.session.api.admin_create_announcement(token, payload)

        if self._call("正在发布公告…", create) is not None:
            QMessageBox.information(self, "发布成功", "公告已发布。")
            self.announcements_changed.emit()
            self.refresh()

    def _edit(self):
        announcement = self._selected_announcement()
        if not announcement:
            return
        dialog = AnnouncementEditorDialog(
            self.accounts,
            announcement,
            self,
        )
        if dialog.exec_() != dialog.Accepted:
            return
        payload = dialog.announcement_payload()
        pending = dialog.pending_attachments()
        removed = dialog.removed_attachment_ids()
        announcement_id = str(announcement.get("id"))

        def update_announcement():
            token = self.session.access_token()
            result = self.session.api.admin_update_announcement(
                token,
                announcement_id,
                payload,
            )
            for attachment_id in removed:
                self.session.api.admin_delete_announcement_attachment(
                    token,
                    announcement_id,
                    attachment_id,
                )
            for item in pending:
                self.session.api.admin_add_announcement_attachment(
                    token,
                    announcement_id,
                    dialog.encode_attachment(item),
                )
            return result

        if self._call("正在保存公告修改…", update_announcement) is not None:
            QMessageBox.information(self, "保存成功", "公告修改已生效。")
            self.announcements_changed.emit()
            self.refresh()

    def _toggle(self):
        announcement = self._selected_announcement()
        if not announcement:
            return
        enabled = not bool(announcement.get("is_active"))
        action = "重新启用" if enabled else "停用"
        reply = QMessageBox.question(
            self,
            f"确认{action}",
            f"确定要{action}公告“{announcement.get('title')}”吗？",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        result = self._call(
            f"正在{action}公告…",
            lambda: self.session.api.admin_update_announcement(
                self.session.access_token(),
                str(announcement.get("id")),
                {"is_active": enabled},
            ),
        )
        if result is not None:
            self.announcements_changed.emit()
            self.refresh()

    def _delete(self):
        announcement = self._selected_announcement()
        if not announcement:
            return
        reply = QMessageBox.question(
            self,
            "确认删除公告",
            "删除后公告正文和附件将无法恢复，用户消息会保留但不再关联公告。\n\n"
            f"确定删除“{announcement.get('title')}”吗？",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        sentinel = object()
        result = self._call(
            "正在删除公告…",
            lambda: (
                self.session.api.admin_delete_announcement(
                    self.session.access_token(),
                    str(announcement.get("id")),
                ),
                sentinel,
            )[1],
        )
        if result is sentinel:
            self.announcements_changed.emit()
            self.refresh()

    def _mark_message_read(self, message=None):
        message = message or self._selected_message()
        if not message or not int(message.get("unread_count") or 0):
            return
        unread = int(message.get("unread_count") or 0)
        message["unread_count"] = 0
        message["read_at"] = message.get("read_at") or True
        total_unread = sum(
            int(item.get("unread_count") or 0) for item in self.messages
        )
        self._fill_message_row(message)
        self.inbox_summary.setText(f"未读消息：{total_unread}")
        self.unread_messages_changed.emit(total_unread)
        task = None

        def completed(_result, error):
            if task in self._message_read_tasks:
                self._message_read_tasks.remove(task)
            if error is not None:
                message["unread_count"] = unread
                message["read_at"] = None
                restored = sum(
                    int(item.get("unread_count") or 0) for item in self.messages
                )
                self._fill_message_row(message)
                self.inbox_summary.setText(f"未读消息：{restored}")
                self.unread_messages_changed.emit(restored)

        task = start_api_task(
            lambda: self.session.api.admin_mark_message_read(
                self.session.access_token(),
                str(message.get("id")),
            ),
            completed,
        )
        self._message_read_tasks.append(task)

    def _fill_message_row(self, message):
        message_id = str(message.get("id") or "")
        for row in range(self.message_table.rowCount()):
            item = self.message_table.item(row, 0)
            if item is None or str(item.data(Qt.UserRole) or "") != message_id:
                continue
            unread = int(message.get("unread_count") or 0)
            item.setText(str(unread))
            font = item.font()
            font.setBold(bool(unread))
            item.setFont(font)
            item.setForeground(QColor("#d14f45" if unread else "#334155"))
            return

    def _reply_message(self):
        self._open_selected_conversation()

    def _open_selected_conversation(self):
        message = self._selected_message()
        if not message:
            return
        message_id = str(message.get("id") or "")
        current = self._chat_windows.get(message_id)
        if current is not None and current.isVisible():
            current.raise_()
            current.activateWindow()
            current.message_edit.setFocus()
            return
        self._mark_message_read(message)
        dialog = AdminConversationDialog(message, self.session, parent=None)

        def update_message(updated):
            target_id = str((updated or {}).get("id") or "")
            for index, item in enumerate(self.messages):
                if str(item.get("id") or "") == target_id:
                    self.messages[index] = dict(updated)
                    break
            self._fill_messages(
                sum(int(item.get("unread_count") or 0) for item in self.messages)
            )

        def clear_dialog(*_args):
            if self._chat_windows.get(message_id) is dialog:
                self._chat_windows.pop(message_id, None)

        dialog.conversation_updated.connect(update_message)
        dialog.unread_count_changed.connect(self.unread_messages_changed)
        dialog.finished.connect(clear_dialog)
        self._chat_windows[message_id] = dialog
        dialog.show()

    def _delete_message(self):
        message = self._selected_message()
        if not message:
            return
        reply = QMessageBox.question(
            self,
            "确认删除会话",
            "删除后整段会话都无法恢复。\n\n"
            f"确定删除来自“{message.get('sender_display_name') or '-'}”的会话吗？",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        sentinel = object()
        result = self._call(
            "正在删除消息…",
            lambda: (
                self.session.api.admin_delete_message(
                    self.session.access_token(),
                    str(message.get("id")),
                ),
                sentinel,
            )[1],
        )
        if result is sentinel:
            self.refresh()

    def close_chat_windows(self):
        for dialog in list(self._chat_windows.values()):
            dialog.close()
        self._chat_windows.clear()

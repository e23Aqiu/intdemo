from __future__ import annotations

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ..config import APP_VERSION
from ..models import Account


class PersonalCenterPage(QWidget):
    """System settings page; the old class name remains API-compatible."""

    change_password_requested = pyqtSignal()
    logout_requested = pyqtSignal()
    check_update_requested = pyqtSignal()
    update_requested = pyqtSignal()
    cancel_update_requested = pyqtSignal()
    install_update_requested = pyqtSignal()

    def __init__(
        self,
        account: Account,
        parent=None,
        *,
        updates_enabled=True,
        updates_disabled_message="",
    ):
        super().__init__(parent)
        self.account = account
        self.available_update = None
        self.update_downloaded = False
        self.update_downloading = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 22)
        layout.setSpacing(14)

        title = QLabel("系统设置")
        title.setObjectName("PageTitle")
        layout.addWidget(title)
        subtitle = QLabel("管理当前账号资料、登录状态和程序版本。")
        subtitle.setObjectName("Muted")
        layout.addWidget(subtitle)

        profile_card = QFrame()
        profile_card.setObjectName("SettingCard")
        profile_layout = QHBoxLayout(profile_card)
        profile_layout.setContentsMargins(22, 18, 22, 18)
        profile_layout.setSpacing(28)

        self.identity_value = self._add_info_item(
            profile_layout,
            "用户身份",
            account.role_label,
        )
        self.name_value = self._add_info_item(
            profile_layout,
            "用户名称",
            account.name_label,
        )
        self.username_value = self._add_info_item(
            profile_layout,
            "登录账号",
            account.username,
        )
        profile_layout.addStretch()

        self.change_password_btn = QPushButton("修改密码")
        self.change_password_btn.setObjectName("PrimaryButton")
        self.change_password_btn.clicked.connect(self.change_password_requested)
        profile_layout.addWidget(self.change_password_btn)
        self.logout_btn = QPushButton("退出登录")
        self.logout_btn.setObjectName("DangerButton")
        self.logout_btn.clicked.connect(self.logout_requested)
        profile_layout.addWidget(self.logout_btn)
        layout.addWidget(profile_card)

        update_card = QFrame()
        update_card.setObjectName("SettingCard")
        update_layout = QVBoxLayout(update_card)
        update_layout.setContentsMargins(22, 20, 22, 20)
        update_layout.setSpacing(12)

        update_header = QHBoxLayout()
        update_title_block = QVBoxLayout()
        update_title = QLabel("版本更新")
        update_title.setObjectName("SettingCardTitle")
        update_title_block.addWidget(update_title)
        self.update_status_label = QLabel(
            "可手动检查新版本。"
            if updates_enabled
            else (
                str(updates_disabled_message or "").strip()
                or "当前为本地模式，未配置在线更新。"
            )
        )
        self.update_status_label.setObjectName("SettingCardDescription")
        self.update_status_label.setWordWrap(True)
        update_title_block.addWidget(self.update_status_label)
        update_header.addLayout(update_title_block, 1)

        version_block = QVBoxLayout()
        version_caption = QLabel("当前版本")
        version_caption.setObjectName("SettingFieldLabel")
        self.current_version_value = QLabel(f"v{APP_VERSION}")
        self.current_version_value.setObjectName("UpdateVersion")
        version_block.addWidget(version_caption)
        version_block.addWidget(self.current_version_value)
        update_header.addLayout(version_block)
        update_layout.addLayout(update_header)

        latest_row = QHBoxLayout()
        latest_caption = QLabel("最新版本")
        latest_caption.setObjectName("SettingFieldLabel")
        self.latest_version_value = QLabel("尚未检查")
        self.latest_version_value.setObjectName("UpdateLatestVersion")
        latest_row.addWidget(latest_caption)
        latest_row.addWidget(self.latest_version_value)
        latest_row.addStretch()
        update_layout.addLayout(latest_row)

        notes_caption = QLabel("更新内容")
        notes_caption.setObjectName("SettingFieldLabel")
        update_layout.addWidget(notes_caption)
        self.update_notes = QTextBrowser()
        self.update_notes.setObjectName("UpdateNotes")
        self.update_notes.setMinimumHeight(72)
        self.update_notes.setMaximumHeight(120)
        self.update_notes.setPlainText("检查更新后将在这里显示版本说明。")
        update_layout.addWidget(self.update_notes)

        self.update_progress = QProgressBar()
        self.update_progress.setObjectName("UpdateProgress")
        self.update_progress.setRange(0, 100)
        self.update_progress.setValue(0)
        self.update_progress.hide()
        update_layout.addWidget(self.update_progress)

        self.update_speed_label = QLabel("")
        self.update_speed_label.setObjectName("UpdateSpeed")
        self.update_speed_label.setAlignment(Qt.AlignRight)
        self.update_speed_label.hide()
        update_layout.addWidget(self.update_speed_label)

        update_actions = QHBoxLayout()
        update_actions.addStretch()
        self.check_update_btn = QPushButton("检查更新")
        self.check_update_btn.setObjectName("UpdateButton")
        self.check_update_btn.setVisible(updates_enabled)
        self.check_update_btn.clicked.connect(self.check_update_requested)
        update_actions.addWidget(self.check_update_btn)
        self.update_action_btn = QPushButton("立即更新")
        self.update_action_btn.setObjectName("PrimaryButton")
        self.update_action_btn.hide()
        self.update_action_btn.clicked.connect(self._update_action)
        update_actions.addWidget(self.update_action_btn)
        update_layout.addLayout(update_actions)
        layout.addWidget(update_card, 1)
        layout.addStretch()

    @staticmethod
    def _add_info_item(layout, caption_text, value):
        block = QVBoxLayout()
        block.setSpacing(4)
        caption = QLabel(caption_text)
        caption.setObjectName("SettingFieldLabel")
        content = QLabel(value)
        content.setObjectName("ProfileValue")
        block.addWidget(caption)
        block.addWidget(content)
        layout.addLayout(block)
        return content

    def _update_action(self):
        if self.update_downloading:
            self.cancel_update_requested.emit()
        elif self.update_downloaded:
            self.install_update_requested.emit()
        else:
            self.update_requested.emit()

    def set_update_available(self, update):
        self.available_update = update
        self.update_downloaded = False
        self.update_downloading = False
        self.latest_version_value.setText(f"v{update.version}")
        self.update_notes.setPlainText(
            update.notes or "本次更新包含功能改进和问题修复。"
        )
        package_label = (
            f"增量更新包 {update.size / 1024 / 1024:.1f} MB"
            if update.is_delta
            else f"完整更新包 {update.size / 1024 / 1024:.1f} MB"
        )
        self.update_status_label.setText(
            "发现强制更新，必须安装后才能继续使用。"
            if update.mandatory
            else f"发现新版本，可下载 {package_label}。"
        )
        self.update_action_btn.setText("立即更新")
        self.update_action_btn.setEnabled(True)
        self.update_action_btn.show()
        self.check_update_btn.setText("重新检查")
        self.check_update_btn.setEnabled(True)

    def set_update_state(self, state: str, message: str):
        self.update_status_label.setText(message)
        if state == "checking":
            self.check_update_btn.setEnabled(False)
            self.check_update_btn.setText("检查中…")
        elif state == "up_to_date":
            self.latest_version_value.setText(f"v{APP_VERSION}")
            self.update_notes.setPlainText("当前已是最新版本。")
            self.check_update_btn.setEnabled(True)
            self.check_update_btn.setText("再次检查")
            self.update_action_btn.hide()
        elif state in {"check_error", "download_error"}:
            self.update_downloading = False
            self.update_speed_label.hide()
            self.check_update_btn.setEnabled(True)
            self.check_update_btn.setText("重新检查")
            if self.available_update is not None:
                self.update_action_btn.setEnabled(True)
                self.update_action_btn.setText("重试更新")
                self.update_action_btn.show()
        elif state == "downloading":
            self.update_downloaded = False
            self.update_downloading = True
            self.update_progress.show()
            self.update_speed_label.show()
            self.update_progress.setRange(0, 100)
            self.update_progress.setValue(0)
            self.check_update_btn.setEnabled(False)
            self.update_action_btn.setEnabled(True)
            self.update_action_btn.setText("停止下载")
            self.update_action_btn.show()
        elif state == "cancelling":
            self.update_action_btn.setEnabled(False)
            self.update_action_btn.setText("正在停止…")
        elif state == "download_cancelled":
            self.update_downloading = False
            self.update_speed_label.hide()
            self.check_update_btn.setEnabled(True)
            self.update_action_btn.setEnabled(True)
            self.update_action_btn.setText("重新下载")
            self.update_action_btn.show()
        elif state == "downloaded":
            self.update_downloading = False
            self.update_downloaded = True
            self.update_speed_label.hide()
            self.update_progress.show()
            self.update_progress.setRange(0, 100)
            self.update_progress.setValue(100)
            self.update_progress.setFormat("100% · 下载及校验完成")
            self.update_action_btn.setEnabled(True)
            self.update_action_btn.setText("重启并安装")
            self.update_action_btn.show()
            self.check_update_btn.setEnabled(True)

    def set_update_progress(self, received: int, total: int):
        self.update_progress.show()
        if total <= 0:
            self.update_progress.setRange(0, 0)
            return
        self.update_progress.setRange(0, 100)
        percent = max(0, min(100, int(received * 100 / total)))
        self.update_progress.setValue(percent)
        self.update_progress.setFormat(
            f"{percent}%  ({received / 1024 / 1024:.1f} / "
            f"{total / 1024 / 1024:.1f} MB)"
        )

    def set_update_speed(self, bytes_per_second: float):
        speed = max(0.0, float(bytes_per_second or 0))
        if speed >= 1024 * 1024:
            text = f"{speed / 1024 / 1024:.1f} MB/s"
        elif speed >= 1024:
            text = f"{speed / 1024:.0f} KB/s"
        else:
            text = f"{speed:.0f} B/s"
        self.update_speed_label.setText(f"下载速度：{text}")

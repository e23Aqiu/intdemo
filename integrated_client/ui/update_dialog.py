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
)

from .frameless import FramelessDialog


def _format_speed(bytes_per_second: float) -> str:
    speed = max(0.0, float(bytes_per_second or 0))
    if speed >= 1024 * 1024:
        return f"{speed / 1024 / 1024:.1f} MB/s"
    if speed >= 1024:
        return f"{speed / 1024:.0f} KB/s"
    return f"{speed:.0f} B/s"


class UpdatePromptDialog(FramelessDialog):
    update_requested = pyqtSignal()
    background_update_requested = pyqtSignal()
    ignore_requested = pyqtSignal()
    cancel_requested = pyqtSignal()
    restart_requested = pyqtSignal()

    def __init__(self, update, parent=None):
        super().__init__(parent)
        self.update = update
        self.mandatory = bool(update.mandatory)
        self._download_active = False
        self._downloaded = False
        self._allow_close = False
        self.setWindowTitle("程序更新")
        # MainWindow applies a soft content-only block while this prompt needs
        # attention. Native Qt modality would also block the main window's
        # minimize, maximize, and close buttons.
        self.setModal(False)
        self.setWindowModality(Qt.NonModal)
        self.setMinimumSize(560, 450)
        self.resize(610, 480)

        if self.mandatory:
            self.window_controls.close_button.hide()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 26, 28, 24)
        layout.setSpacing(14)

        title = QLabel("发现必须安装的新版本" if self.mandatory else "发现新版本")
        title.setObjectName("PageTitle")
        layout.addWidget(title)

        version_row = QHBoxLayout()
        version_caption = QLabel("版本信息")
        version_caption.setObjectName("Muted")
        self.version_label = QLabel(f"v{update.version}")
        self.version_label.setObjectName("UpdateVersion")
        version_row.addWidget(version_caption)
        version_row.addWidget(self.version_label)
        package_label = QLabel(
            f"增量更新 · {update.size / 1024 / 1024:.1f} MB"
            if update.is_delta
            else f"完整更新 · {update.size / 1024 / 1024:.1f} MB"
        )
        package_label.setObjectName("UpdatePackageBadge")
        version_row.addWidget(package_label)
        version_row.addStretch()
        if self.mandatory:
            mandatory_badge = QLabel("强制更新")
            mandatory_badge.setObjectName("MandatoryUpdateBadge")
            version_row.addWidget(mandatory_badge)
        layout.addLayout(version_row)

        notes_card = QFrame()
        notes_card.setObjectName("SettingCard")
        notes_layout = QVBoxLayout(notes_card)
        notes_layout.setContentsMargins(16, 14, 16, 14)
        notes_title = QLabel("更新内容")
        notes_title.setObjectName("SettingCardTitle")
        notes_layout.addWidget(notes_title)
        self.notes = QTextBrowser()
        self.notes.setObjectName("UpdateNotes")
        self.notes.setOpenExternalLinks(False)
        self.notes.setPlainText(
            update.notes or "本次更新包含功能改进和问题修复。"
        )
        notes_layout.addWidget(self.notes, 1)
        layout.addWidget(notes_card, 1)

        self.state_label = QLabel(
            "此版本必须更新。点击更新后，下载期间仍可处理业务。"
            if self.mandatory
            else "可以立即更新、转入后台下载，或稍后在“系统设置”中更新。"
        )
        self.state_label.setObjectName("UpdateState")
        self.state_label.setWordWrap(True)
        layout.addWidget(self.state_label)

        self.progress = QProgressBar()
        self.progress.setObjectName("UpdateProgress")
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFormat("%p%")
        self.progress.hide()
        layout.addWidget(self.progress)

        self.speed_label = QLabel("")
        self.speed_label.setObjectName("UpdateSpeed")
        self.speed_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.speed_label.hide()
        layout.addWidget(self.speed_label)

        buttons = QHBoxLayout()
        buttons.addStretch()
        self.ignore_button = QPushButton("不再提示")
        self.ignore_button.setVisible(not self.mandatory)
        self.ignore_button.clicked.connect(self._ignore)
        buttons.addWidget(self.ignore_button)
        self.cancel_button = QPushButton("稍后更新")
        self.cancel_button.setVisible(not self.mandatory)
        self.cancel_button.clicked.connect(self._cancel)
        buttons.addWidget(self.cancel_button)
        self.background_button = QPushButton("后台更新")
        self.background_button.setObjectName("UpdateButton")
        self.background_button.setVisible(not self.mandatory)
        self.background_button.setToolTip(
            "开始下载并收起此窗口，可在“系统设置”中查看进度。"
        )
        self.background_button.clicked.connect(self._background_update)
        buttons.addWidget(self.background_button)
        self.update_button = QPushButton("立即更新")
        self.update_button.setObjectName("PrimaryButton")
        self.update_button.clicked.connect(self._primary_action)
        buttons.addWidget(self.update_button)
        layout.addLayout(buttons)

    def _primary_action(self):
        if self._downloaded:
            self.restart_requested.emit()
        elif not self._download_active:
            self.update_requested.emit()

    def _ignore(self):
        if self._download_active or self.mandatory:
            return
        self.ignore_requested.emit()
        self._allow_close = True
        super().reject()

    def _background_update(self):
        if self._download_active or self._downloaded or self.mandatory:
            return
        self.background_update_requested.emit()

    def _cancel(self):
        if self._download_active:
            self.cancel_requested.emit()
            self.cancel_button.setEnabled(False)
            self.cancel_button.setText("正在停止…")
            return
        if self.mandatory:
            return
        self._allow_close = True
        super().reject()

    def begin_download(self):
        self._download_active = True
        self._downloaded = False
        self.progress.show()
        self.speed_label.show()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.state_label.setText(
            "正在后台下载更新。可以继续查看数据和处理业务，也可随时停止下载。"
        )
        self.update_button.setEnabled(False)
        self.update_button.setText("正在下载…")
        self.ignore_button.setEnabled(False)
        self.background_button.setEnabled(False)
        self.cancel_button.show()
        self.cancel_button.setEnabled(True)
        self.cancel_button.setText("停止下载")
        self.window_controls.close_button.hide()

    def set_progress(self, received: int, total: int):
        if total <= 0:
            self.progress.setRange(0, 0)
            return
        self.progress.setRange(0, 100)
        percent = max(0, min(100, int(received * 100 / total)))
        self.progress.setValue(percent)
        self.progress.setFormat(
            f"{percent}%  ({received / 1024 / 1024:.1f} / "
            f"{total / 1024 / 1024:.1f} MB)"
        )

    def set_speed(self, bytes_per_second: float):
        self.speed_label.setText(f"下载速度：{_format_speed(bytes_per_second)}")

    def _restore_retry_state(self, message: str):
        self._download_active = False
        self.state_label.setText(message)
        self.speed_label.hide()
        self.update_button.setEnabled(True)
        self.update_button.setText("重新下载")
        self.ignore_button.setEnabled(not self.mandatory)
        self.background_button.setEnabled(not self.mandatory)
        self.background_button.setVisible(not self.mandatory)
        self.cancel_button.setEnabled(True)
        self.cancel_button.setText("稍后更新")
        self.cancel_button.setVisible(not self.mandatory)
        if not self.mandatory:
            self.window_controls.close_button.show()

    def set_cancelled(self):
        self._restore_retry_state("更新下载已停止，可稍后重新下载。")

    def set_error(self, message: str):
        self._restore_retry_state(f"更新失败：{message}")

    def set_downloaded(self):
        self._download_active = False
        self._downloaded = True
        self.speed_label.hide()
        self.progress.setRange(0, 100)
        self.progress.setValue(100)
        self.progress.setFormat("100% · 下载及校验完成")
        self.state_label.setText(
            "更新包已准备完成。安装会退出程序，请先结束并保存正在处理的业务。"
        )
        self.update_button.setEnabled(True)
        self.update_button.setText("重启并安装")
        self.ignore_button.hide()
        self.background_button.hide()
        if not self.mandatory:
            self.cancel_button.setEnabled(True)
            self.cancel_button.setText("稍后安装")
            self.cancel_button.show()
            self.window_controls.close_button.show()
        else:
            self.cancel_button.hide()

    def allow_close(self):
        self._allow_close = True

    def reject(self):
        if self._allow_close or (
            not self.mandatory and not self._download_active
        ):
            super().reject()

    def closeEvent(self, event):
        if self._allow_close or (
            not self.mandatory and not self._download_active
        ):
            # Let QDialog reject itself so finished() is emitted. MainWindow
            # relies on that signal to release its content-only prompt block.
            super().closeEvent(event)
        else:
            event.ignore()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape and (
            self.mandatory or self._download_active
        ):
            event.ignore()
            return
        super().keyPressEvent(event)

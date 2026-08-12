from PyQt5.QtCore import pyqtSignal
from PyQt5.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)

from .frameless import FramelessDialog


class TencentDocsLinkDialog(FramelessDialog):
    def __init__(
        self,
        current_url="",
        parent=None,
        *,
        title="导入腾讯文档",
        confirm_text="确认并打开",
    ):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setFixedSize(590, 255)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 26, 28, 24)
        layout.setSpacing(14)

        title_label = QLabel(title)
        title_label.setObjectName("PageTitle")
        layout.addWidget(title_label)
        prompt = QLabel("请输入腾讯文档在线表格链接")
        prompt.setObjectName("SettingFieldLabel")
        layout.addWidget(prompt)
        self.url_edit = QLineEdit(str(current_url or ""))
        self.url_edit.setPlaceholderText(
            "https://docs.qq.com/sheet/..."
        )
        self.url_edit.returnPressed.connect(self._accept_if_valid)
        layout.addWidget(self.url_edit)
        hint = QLabel(
            "链接和腾讯文档登录状态会按当前程序账号保存在本机。"
        )
        hint.setObjectName("Muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        confirm = QPushButton(confirm_text)
        confirm.setObjectName("PrimaryButton")
        confirm.clicked.connect(self._accept_if_valid)
        buttons.addWidget(cancel)
        buttons.addWidget(confirm)
        layout.addLayout(buttons)

        self.url_edit.setFocus()
        self.url_edit.selectAll()

    def _accept_if_valid(self):
        if self.url_edit.text().strip():
            self.accept()

    def value(self):
        return self.url_edit.text().strip()


class TencentDocsProgressDialog(FramelessDialog):
    cancel_requested = pyqtSignal()

    def __init__(
        self,
        parent=None,
        *,
        title="导入腾讯文档",
        operation="导入腾讯文档",
        cancel_text="取消导入",
    ):
        super().__init__(parent)
        self._finished = False
        self._cancel_sent = False
        self.setWindowTitle(title)
        self.setModal(True)
        self.setFixedSize(460, 210)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 28, 28, 24)
        layout.setSpacing(16)

        title_label = QLabel(f"正在{operation}")
        title_label.setObjectName("PageTitle")
        layout.addWidget(title_label)
        self.status_label = QLabel("正在准备浏览器…")
        self.status_label.setObjectName("LoadingMessage")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        self.progress = QProgressBar()
        self.progress.setObjectName("LoadingProgress")
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFormat("%p%")
        self.progress.setTextVisible(True)
        layout.addWidget(self.progress)
        self.cancel_button = QPushButton(cancel_text)
        self.cancel_button.clicked.connect(self._request_cancel)
        layout.addWidget(self.cancel_button)

    def set_status(self, message: str) -> None:
        self.status_label.setText(str(message or "正在处理…"))

    def set_progress(self, value: int) -> None:
        self.progress.setValue(max(0, min(100, int(value))))

    def _request_cancel(self) -> None:
        if self._finished or self._cancel_sent:
            return
        self._cancel_sent = True
        self.cancel_button.setEnabled(False)
        self.status_label.setText("正在取消并关闭腾讯文档浏览器…")
        self.cancel_requested.emit()

    def finish(self) -> None:
        self._finished = True
        self.accept()

    def reject(self) -> None:
        if self._finished:
            super().reject()
        else:
            self._request_cancel()

    def closeEvent(self, event) -> None:
        if self._finished:
            event.accept()
        else:
            event.ignore()
            self._request_cancel()

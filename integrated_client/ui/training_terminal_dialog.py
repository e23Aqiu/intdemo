from __future__ import annotations

from datetime import datetime

from PyQt5.QtGui import QFontDatabase
from PyQt5.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)

from .frameless import FramelessDialog


class TrainingTerminalDialog(FramelessDialog):
    """Non-modal training progress window shared by both training modes."""

    def __init__(
        self,
        *,
        captcha_name: str,
        mode_name: str,
        method_name: str,
        parent=None,
    ):
        super().__init__(
            parent,
            resizable=True,
            show_minimize=True,
            show_maximize=True,
        )
        self._running = True
        self.setWindowTitle("训练终端")
        self.resize(780, 520)
        self.setMinimumSize(620, 400)

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 48, 22, 20)
        root.setSpacing(12)

        title = QLabel(f"{captcha_name} · {mode_name}")
        title.setObjectName("SectionTitle")
        root.addWidget(title)

        self.method_label = QLabel(f"训练方法：{method_name}")
        self.method_label.setObjectName("Muted")
        self.method_label.setWordWrap(True)
        root.addWidget(self.method_label)

        self.status_label = QLabel("正在准备训练")
        self.status_label.setWordWrap(True)
        root.addWidget(self.status_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("训练进度 %p%")
        root.addWidget(self.progress_bar)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.log.document().setMaximumBlockCount(5_000)
        self.log.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        self.log.setStyleSheet(
            "QPlainTextEdit {"
            "background:#10191e;color:#dbe9e5;border:1px solid #2f4c4a;"
            "border-radius:4px;padding:10px;selection-background-color:#246f68;"
            "}"
        )
        root.addWidget(self.log, 1)

        actions = QHBoxLayout()
        actions.addStretch()
        self.copy_btn = QPushButton("复制日志")
        self.copy_btn.clicked.connect(self._copy_log)
        self.close_btn = QPushButton("隐藏")
        self.close_btn.clicked.connect(self._close_or_hide)
        actions.addWidget(self.copy_btn)
        actions.addWidget(self.close_btn)
        root.addLayout(actions)

    @property
    def running(self) -> bool:
        return self._running

    def append_event(self, payload: dict[str, object]) -> None:
        message = str(payload.get("message") or "").strip()
        if not message:
            return
        progress = payload.get("progress")
        if type(progress) is int:
            self.progress_bar.setValue(max(0, min(100, progress)))
        self.status_label.setText(message)
        timestamp = datetime.now().astimezone().strftime("%H:%M:%S")
        self.log.appendPlainText(f"[{timestamp}] {message}")
        scrollbar = self.log.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def finish(self, *, error: Exception | None = None) -> None:
        self._running = False
        if error is None:
            self.progress_bar.setValue(100)
            self.progress_bar.setFormat("训练已完成 100%")
            self.append_event(
                {
                    "progress": 100,
                    "message": "候选模型训练、校验并上传完成",
                }
            )
        else:
            self.progress_bar.setFormat("训练未完成")
            self.append_event({"message": f"训练失败：{error}"})
        self.close_btn.setText("关闭")

    def _copy_log(self) -> None:
        QApplication.clipboard().setText(self.log.toPlainText())

    def _close_or_hide(self) -> None:
        if self._running:
            self.hide()
        else:
            self.accept()

    def reject(self) -> None:
        if self._running:
            self.hide()
        else:
            super().reject()

    def closeEvent(self, event) -> None:
        if self._running:
            event.ignore()
            self.hide()
        else:
            event.accept()

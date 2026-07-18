from PyQt5.QtCore import pyqtSignal
from PyQt5.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..models import Account


class PersonalCenterPage(QWidget):
    change_password_requested = pyqtSignal()
    logout_requested = pyqtSignal()

    def __init__(self, account: Account, parent=None):
        super().__init__(parent)
        self.account = account

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 22)
        layout.setSpacing(12)

        title = QLabel("个人中心")
        title.setObjectName("PageTitle")
        layout.addWidget(title)
        subtitle = QLabel("查看当前用户信息并管理登录状态。")
        subtitle.setObjectName("Muted")
        layout.addWidget(subtitle)

        card = QFrame()
        card.setObjectName("Card")
        card.setMaximumWidth(620)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(24, 22, 24, 22)
        card_layout.setSpacing(20)

        info = QGridLayout()
        info.setHorizontalSpacing(28)
        info.setVerticalSpacing(18)
        self.identity_value = self._add_info_row(info, 0, "用户身份", account.role_label)
        self.name_value = self._add_info_row(info, 1, "用户名称", account.name_label)
        self.username_value = self._add_info_row(info, 2, "登录账号", account.username)
        card_layout.addLayout(info)

        actions = QHBoxLayout()
        self.change_password_btn = QPushButton("修改密码")
        self.change_password_btn.setObjectName("PrimaryButton")
        self.change_password_btn.clicked.connect(self.change_password_requested)
        self.logout_btn = QPushButton("退出登录")
        self.logout_btn.setObjectName("DangerButton")
        self.logout_btn.clicked.connect(self.logout_requested)
        actions.addWidget(self.change_password_btn)
        actions.addWidget(self.logout_btn)
        actions.addStretch()
        card_layout.addLayout(actions)

        layout.addWidget(card)
        layout.addStretch()

    @staticmethod
    def _add_info_row(layout, row, label, value):
        caption = QLabel(label)
        caption.setObjectName("Muted")
        content = QLabel(value)
        content.setStyleSheet("font-size:15px;font-weight:600;color:#17233c;")
        layout.addWidget(caption, row, 0)
        layout.addWidget(content, row, 1)
        return content

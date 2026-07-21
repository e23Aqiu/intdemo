from dataclasses import replace

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QComboBox,
    QDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from ..config import APP_NAME
from ..database import AuthenticationError, Database
from .frameless import FramelessDialog, FramelessMessageBox as QMessageBox


class PasswordDialog(FramelessDialog):
    def __init__(
        self, database: Database, account_id: int, forced=False,
        must_change_after=False, require_current=False, parent=None
    ):
        super().__init__(parent)
        self.database = database
        self.account_id = account_id
        self.forced = forced
        self.must_change_after = must_change_after
        self.require_current = require_current
        self.setWindowTitle("修改密码")
        self.setModal(True)
        self.setMinimumWidth(390)

        layout = QVBoxLayout(self)
        if forced:
            title_text = "请设置新密码"
        elif require_current:
            title_text = "修改登录密码"
        else:
            title_text = "重置账号密码"
        title = QLabel(title_text)
        title.setObjectName("PageTitle")
        layout.addWidget(title)

        hint = QLabel("密码至少 6 位。" + ("首次登录必须修改初始密码。" if forced else ""))
        hint.setObjectName("Muted")
        layout.addWidget(hint)

        form = QFormLayout()
        self.current_password_edit = None
        if require_current:
            self.current_password_edit = QLineEdit()
            self.current_password_edit.setEchoMode(QLineEdit.Password)
            self.current_password_edit.setPlaceholderText("请输入当前登录密码")
            form.addRow("原密码", self.current_password_edit)
        self.password_edit = QLineEdit()
        self.password_edit.setEchoMode(QLineEdit.Password)
        self.confirm_edit = QLineEdit()
        self.confirm_edit.setEchoMode(QLineEdit.Password)
        form.addRow("新密码", self.password_edit)
        form.addRow("确认密码", self.confirm_edit)
        layout.addLayout(form)

        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        save = QPushButton("保存密码")
        save.setObjectName("PrimaryButton")
        save.clicked.connect(self._save)
        buttons.addWidget(cancel)
        buttons.addWidget(save)
        layout.addLayout(buttons)

    def reject(self):
        super().reject()

    def _save(self):
        password = self.password_edit.text()
        if password != self.confirm_edit.text():
            QMessageBox.warning(self, "密码不一致", "两次输入的密码不一致。")
            return
        try:
            if self.require_current:
                self.database.change_own_password(
                    self.account_id,
                    self.current_password_edit.text(),
                    password,
                )
            else:
                self.database.change_password(
                    self.account_id,
                    password,
                    must_change=(self.must_change_after and not self.forced),
                )
        except AuthenticationError as exc:
            QMessageBox.warning(self, "原密码错误", str(exc))
            self.current_password_edit.selectAll()
            self.current_password_edit.setFocus()
            return
        except ValueError as exc:
            QMessageBox.warning(self, "密码无效", str(exc))
            return
        QMessageBox.information(self, "成功", "密码已更新。")
        super().accept()


class LoginDialog(FramelessDialog):
    def __init__(self, database: Database, parent=None):
        super().__init__(
            parent,
            resizable=False,
            show_minimize=False,
            show_maximize=False,
        )
        self.database = database
        self.account = None
        self.setWindowTitle(f"登录 - {APP_NAME}")
        self.setFixedSize(470, 430)

        root = QVBoxLayout(self)
        root.setContentsMargins(42, 36, 42, 36)
        root.setSpacing(14)
        root.addStretch()

        title = QLabel(APP_NAME)
        title.setObjectName("PageTitle")
        title.setAlignment(Qt.AlignCenter)
        root.addWidget(title)

        subtitle = QLabel("账号登录")
        subtitle.setObjectName("Muted")
        subtitle.setAlignment(Qt.AlignCenter)
        root.addWidget(subtitle)

        card = QFrame()
        card.setObjectName("Card")
        card_layout = QFormLayout(card)
        card_layout.setContentsMargins(20, 22, 20, 22)
        card_layout.setSpacing(14)

        self.username_edit = QLineEdit()
        self.username_edit.setPlaceholderText("请输入账号")
        self.password_edit = QLineEdit()
        self.password_edit.setPlaceholderText("请输入密码")
        self.password_edit.setEchoMode(QLineEdit.Password)
        self.password_edit.returnPressed.connect(self._login)
        card_layout.addRow("账号", self.username_edit)
        card_layout.addRow("密码", self.password_edit)
        root.addWidget(card)

        login_btn = QPushButton("登录")
        login_btn.setObjectName("PrimaryButton")
        login_btn.setMinimumHeight(42)
        login_btn.clicked.connect(self._login)
        root.addWidget(login_btn)
        root.addStretch()

    def _login(self):
        try:
            account = self.database.authenticate(
                self.username_edit.text(), self.password_edit.text()
            )
        except AuthenticationError as exc:
            QMessageBox.warning(self, "登录失败", str(exc))
            self.password_edit.selectAll()
            self.password_edit.setFocus()
            return

        if account.must_change_password:
            dialog = PasswordDialog(self.database, account.id, forced=True, parent=self)
            if dialog.exec_() != QDialog.Accepted:
                return
            account = replace(account, must_change_password=False)
        self.account = account
        self.accept()


class RenameAccountDialog(FramelessDialog):
    def __init__(self, account, parent=None):
        super().__init__(parent)
        self.setWindowTitle("修改用户名称")
        self.setMinimumWidth(410)

        layout = QVBoxLayout(self)
        title = QLabel("修改用户名称")
        title.setObjectName("PageTitle")
        layout.addWidget(title)
        hint = QLabel("仅修改界面显示名称，登录账号不会改变。")
        hint.setObjectName("Muted")
        layout.addWidget(hint)

        form = QFormLayout()
        username = QLabel(account.username)
        username.setObjectName("Muted")
        self.name_edit = QLineEdit(account.name_label)
        self.name_edit.setPlaceholderText("请输入用户名称")
        self.name_edit.setMaxLength(64)
        form.addRow("登录账号", username)
        form.addRow("用户名称", self.name_edit)
        layout.addLayout(form)

        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        save = QPushButton("保存名称")
        save.setObjectName("PrimaryButton")
        save.clicked.connect(self._validate)
        buttons.addWidget(cancel)
        buttons.addWidget(save)
        layout.addLayout(buttons)

        self.name_edit.selectAll()
        self.name_edit.setFocus()

    def _validate(self):
        if not self.name_edit.text().strip():
            QMessageBox.warning(self, "名称无效", "用户名称不能为空。")
            self.name_edit.setFocus()
            return
        self.accept()

    def value(self):
        return self.name_edit.text().strip()


class AccountPermissionDialog(FramelessDialog):
    def __init__(self, account, parent=None):
        super().__init__(parent)
        self.setWindowTitle("修改账号权限")
        self.setMinimumWidth(430)

        layout = QVBoxLayout(self)
        title = QLabel("修改账号权限")
        title.setObjectName("PageTitle")
        layout.addWidget(title)
        hint = QLabel(
            "管理员可以管理账号和所有站点数据；普通用户不显示账号管理入口。"
        )
        hint.setWordWrap(True)
        hint.setObjectName("Muted")
        layout.addWidget(hint)

        form = QFormLayout()
        account_label = QLabel(f"{account.name_label}（{account.username}）")
        self.role_combo = QComboBox()
        self.role_combo.addItem("普通用户", "user")
        self.role_combo.addItem("管理员", "admin")
        self.role_combo.setCurrentIndex(self.role_combo.findData(account.role))
        form.addRow("账号", account_label)
        form.addRow("账号权限", self.role_combo)
        layout.addLayout(form)

        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        save = QPushButton("保存权限")
        save.setObjectName("PrimaryButton")
        save.clicked.connect(self.accept)
        buttons.addWidget(cancel)
        buttons.addWidget(save)
        layout.addLayout(buttons)

    def value(self):
        return self.role_combo.currentData()


class CreateAccountDialog(FramelessDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("新建账号")
        self.setMinimumWidth(410)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.display_name_edit = QLineEdit()
        self.display_name_edit.setPlaceholderText("例如：萝岗中心站")
        self.username_edit = QLineEdit()
        self.password_edit = QLineEdit()
        self.password_edit.setPlaceholderText("至少 6 位")
        self.password_edit.setEchoMode(QLineEdit.Password)
        self.confirm_edit = QLineEdit()
        self.confirm_edit.setEchoMode(QLineEdit.Password)
        self.role_combo = QComboBox()
        self.role_combo.addItem("普通用户", "user")
        self.role_combo.addItem("管理员", "admin")
        form.addRow("用户名称", self.display_name_edit)
        form.addRow("账号", self.username_edit)
        form.addRow("初始密码", self.password_edit)
        form.addRow("确认密码", self.confirm_edit)
        form.addRow("角色", self.role_combo)
        layout.addLayout(form)

        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        create = QPushButton("创建账号")
        create.setObjectName("PrimaryButton")
        create.clicked.connect(self._validate)
        buttons.addWidget(cancel)
        buttons.addWidget(create)
        layout.addLayout(buttons)

    def _validate(self):
        if self.password_edit.text() != self.confirm_edit.text():
            QMessageBox.warning(self, "密码不一致", "两次输入的密码不一致。")
            return
        self.accept()

    def values(self):
        return (
            self.display_name_edit.text().strip(),
            self.username_edit.text().strip(),
            self.password_edit.text(),
            self.role_combo.currentData(),
        )

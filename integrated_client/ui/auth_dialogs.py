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
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from ..config import APP_NAME, DEFAULT_ADMIN_PASSWORD, DEFAULT_ADMIN_USERNAME
from ..database import AuthenticationError, Database


class PasswordDialog(QDialog):
    def __init__(
        self, database: Database, account_id: int, forced=False,
        must_change_after=False, parent=None
    ):
        super().__init__(parent)
        self.database = database
        self.account_id = account_id
        self.forced = forced
        self.must_change_after = must_change_after
        self.setWindowTitle("修改密码")
        self.setModal(True)
        self.setMinimumWidth(390)

        layout = QVBoxLayout(self)
        title = QLabel("请设置新密码" if forced else "重置账号密码")
        title.setObjectName("PageTitle")
        layout.addWidget(title)

        hint = QLabel("密码至少 6 位。" + ("首次登录必须修改初始密码。" if forced else ""))
        hint.setObjectName("Muted")
        layout.addWidget(hint)

        form = QFormLayout()
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
            self.database.change_password(
                self.account_id,
                password,
                must_change=(self.must_change_after and not self.forced),
            )
        except ValueError as exc:
            QMessageBox.warning(self, "密码无效", str(exc))
            return
        QMessageBox.information(self, "成功", "密码已更新。")
        super().accept()


class LoginDialog(QDialog):
    def __init__(self, database: Database, bootstrap_created=False, parent=None):
        super().__init__(parent)
        self.database = database
        self.bootstrap_created = bootstrap_created
        self.account = None
        self.setWindowTitle(f"登录 - {APP_NAME}")
        self.setFixedSize(470, 430)

        root = QVBoxLayout(self)
        root.setContentsMargins(42, 36, 42, 36)
        root.setSpacing(14)

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

        if bootstrap_created:
            info = QLabel(
                f"首次启动已创建管理员：{DEFAULT_ADMIN_USERNAME} / {DEFAULT_ADMIN_PASSWORD}\n"
                "登录后将要求立即修改密码。"
            )
            info.setWordWrap(True)
            info.setStyleSheet("color:#9a6415;background:#fff7e5;padding:10px;border-radius:6px;")
            root.addWidget(info)
            self.username_edit.setText(DEFAULT_ADMIN_USERNAME)

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


class CreateAccountDialog(QDialog):
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

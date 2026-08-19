from dataclasses import replace

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import (
    QCheckBox,
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
from ..models import Account
from ..online.secure import SecureStorageUnavailable
from ..preferences import LoginCredentialStore
from .frameless import FramelessDialog
from .frameless import FramelessMessageBox as QMessageBox
from .loading_dialog import run_with_loading


class PasswordDialog(FramelessDialog):
    def __init__(
        self, database: Database, account_id: int, forced=False,
        must_change_after=False, require_current=False, parent=None,
        session_manager=None, initial_current_password=""
    ):
        super().__init__(parent)
        self.database = database
        self.account_id = account_id
        self.forced = forced
        self.must_change_after = must_change_after
        self.require_current = require_current
        self.session_manager = session_manager
        self.initial_current_password = initial_current_password
        self.account = None
        self.new_password = ""
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
            if self.session_manager is not None:
                current_password = (
                    self.current_password_edit.text()
                    if self.current_password_edit is not None
                    else self.initial_current_password
                )
                self.account = run_with_loading(
                    self,
                    "处理中…",
                    lambda: self.session_manager.change_password(
                        current_password,
                        password,
                    ),
                )
            elif self.require_current:
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
            if self.current_password_edit is not None:
                self.current_password_edit.selectAll()
                self.current_password_edit.setFocus()
            return
        except RuntimeError as exc:
            QMessageBox.warning(self, "修改失败", str(exc))
            return
        except ValueError as exc:
            QMessageBox.warning(self, "密码无效", str(exc))
            return
        self.new_password = password
        QMessageBox.information(self, "成功", "密码已更新。")
        super().accept()


class LoginDialog(FramelessDialog):
    def __init__(
        self,
        database: Database,
        parent=None,
        session_manager=None,
        configuration_error="",
        credential_store=None,
    ):
        super().__init__(
            parent,
            resizable=False,
            show_minimize=False,
            show_maximize=False,
        )
        self.database = database
        self.session_manager = session_manager
        self.configuration_error = str(configuration_error or "")
        self.session_state = None
        self.account = None
        self.offline_business_mode = False
        self._login_in_progress = False
        self.credential_store = credential_store or LoginCredentialStore(
            self.database.path.parent,
            protector=getattr(session_manager, "protector", None),
        )
        self.setWindowTitle(f"登录 - {APP_NAME}")
        self.setFixedSize(470, 485)

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
        self.username_edit.returnPressed.connect(self._login)
        self.password_edit.returnPressed.connect(self._login)
        card_layout.addRow("账号", self.username_edit)
        card_layout.addRow("密码", self.password_edit)

        preference_row = QHBoxLayout()
        self.remember_password_checkbox = QCheckBox("记住密码")
        self.auto_login_checkbox = QCheckBox("自动登录")
        self.remember_checkbox = self.remember_password_checkbox
        self.remember_password_checkbox.toggled.connect(
            self._remember_password_toggled
        )
        self.auto_login_checkbox.toggled.connect(self._auto_login_toggled)
        preference_row.addWidget(self.remember_password_checkbox)
        preference_row.addStretch()
        preference_row.addWidget(self.auto_login_checkbox)
        card_layout.addRow("", preference_row)
        root.addWidget(card)

        self.login_btn = QPushButton("登录")
        self.login_btn.setObjectName("PrimaryButton")
        self.login_btn.setMinimumHeight(42)
        self.login_btn.setDefault(True)
        self.login_btn.setAutoDefault(True)
        self.login_btn.clicked.connect(self._login)
        self.offline_login_btn = QPushButton("离线登录")
        self.offline_login_btn.setMinimumHeight(42)
        self.offline_login_btn.setDefault(False)
        self.offline_login_btn.setAutoDefault(False)
        self.offline_login_btn.setToolTip(
            "无需账号和密码，仅可处理业务；本次不记录统计、计时或同步数据。"
        )
        self.offline_login_btn.clicked.connect(self._guest_login)
        login_actions = QHBoxLayout()
        login_actions.setSpacing(10)
        login_actions.addWidget(self.offline_login_btn)
        login_actions.addWidget(self.login_btn, 1)
        root.addLayout(login_actions)
        root.addStretch()

        remembered = self.credential_store.load()
        if remembered is not None:
            self.username_edit.setText(remembered.username)
            self.password_edit.setText(remembered.password)
            self.remember_password_checkbox.setChecked(True)
            self.auto_login_checkbox.setChecked(remembered.auto_login)
            if remembered.auto_login:
                QTimer.singleShot(
                    0,
                    lambda: self._login(automatic=True),
                )
        elif not self.credential_store.is_available:
            hint = "当前系统无法安全保存密码"
            self.remember_password_checkbox.setEnabled(False)
            self.auto_login_checkbox.setEnabled(False)
            self.remember_password_checkbox.setToolTip(hint)
            self.auto_login_checkbox.setToolTip(hint)

    def _remember_password_toggled(self, checked):
        if not checked and self.auto_login_checkbox.isChecked():
            self.auto_login_checkbox.setChecked(False)

    def _auto_login_toggled(self, checked):
        if checked and not self.remember_password_checkbox.isChecked():
            self.remember_password_checkbox.setChecked(True)

    def _login(
        self,
        _checked=False,
        *,
        automatic=False,
        explicit_offline=False,
    ):
        if self._login_in_progress or self.result() == QDialog.Accepted:
            return
        if explicit_offline and self.session_manager is None:
            QMessageBox.warning(
                self,
                "离线登录不可用",
                "本机没有可验证的在线账号离线资料。",
            )
            return
        if self.configuration_error and not explicit_offline:
            QMessageBox.warning(
                self,
                "在线服务未配置",
                self.configuration_error,
            )
            return
        password = self.password_edit.text()
        self._login_in_progress = True
        self.login_btn.setEnabled(False)
        self.offline_login_btn.setEnabled(False)
        self.login_btn.setText("登录中…")
        self.offline_login_btn.setText("验证中…")
        try:
            if self.session_manager is not None:
                account = run_with_loading(
                    self,
                    "正在验证离线授权…"
                    if explicit_offline
                    else "登录中…",
                    lambda: (
                        self.session_manager.offline_login(
                            self.username_edit.text(),
                            password,
                        )
                        if explicit_offline
                        else self.session_manager.login(
                            self.username_edit.text(),
                            password,
                        )
                    ),
                )
            else:
                account = self.database.authenticate(
                    self.username_edit.text(), password
                )
        except AuthenticationError as exc:
            if automatic:
                try:
                    self.credential_store.disable_auto_login()
                except (OSError, ValueError, SecureStorageUnavailable):
                    pass
            QMessageBox.warning(self, "登录失败", str(exc))
            self.password_edit.selectAll()
            self.password_edit.setFocus()
            return
        finally:
            self._login_in_progress = False
            self.login_btn.setEnabled(True)
            self.login_btn.setText("登录")
            self.offline_login_btn.setEnabled(True)
            self.offline_login_btn.setText("离线登录")

        if account.must_change_password:
            dialog = PasswordDialog(
                self.database,
                account.id,
                forced=True,
                parent=self,
                session_manager=self.session_manager,
                initial_current_password=password,
            )
            if dialog.exec_() != QDialog.Accepted:
                return
            account = (
                dialog.account
                if dialog.account is not None
                else replace(account, must_change_password=False)
            )
            if dialog.new_password:
                password = dialog.new_password
        if self.session_manager is not None:
            self.session_state = self.session_manager.state
            if (
                self.session_state
                and self.session_state.mode == "offline_untracked"
            ):
                self.offline_business_mode = True
                QMessageBox.information(
                    self,
                    "离线业务模式",
                    "已使用本机离线授权登录。本次可以处理业务，但不会记录统计、"
                    "计时或待同步数据。",
                )
            elif self.session_state and not self.session_state.is_online:
                QMessageBox.information(
                    self,
                    "离线登录",
                    "当前无法连接服务器，已使用本机 7 天离线授权登录。"
                    "业务数据会保存在待上传队列中。",
                )
        if self.remember_password_checkbox.isChecked():
            try:
                self.credential_store.save(
                    account.username,
                    password,
                    auto_login=self.auto_login_checkbox.isChecked(),
                )
            except (OSError, ValueError, SecureStorageUnavailable) as exc:
                self.remember_password_checkbox.setChecked(False)
                QMessageBox.warning(self, "无法记住密码", str(exc))
        else:
            self.credential_store.clear()
        self.account = account
        self.accept()

    def _guest_login(self, _checked=False):
        """Enter an untracked, in-memory guest session without credentials."""
        if self._login_in_progress or self.result() == QDialog.Accepted:
            return
        self.session_state = None
        self.offline_business_mode = True
        self.account = Account(
            id=-1,
            username="guest",
            display_name="离线游客",
            role="user",
            is_active=True,
            created_at="",
            last_login=None,
        )
        self.accept()


class ReconnectDialog(FramelessDialog):
    def __init__(self, session_manager, account, parent=None):
        super().__init__(
            parent,
            resizable=False,
            show_minimize=False,
            show_maximize=False,
        )
        self.session_manager = session_manager
        self.account = None
        self.password = ""
        self.setWindowTitle("重新上线")
        self.setFixedSize(430, 270)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(34, 28, 34, 28)
        layout.setSpacing(14)
        title = QLabel("重新上线")
        title.setObjectName("PageTitle")
        layout.addWidget(title)
        hint = QLabel(
            "当前账号和业务任务会继续保留。验证成功后将自动上传离线数据。"
        )
        hint.setObjectName("Muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        form = QFormLayout()
        username = QLabel(account.username)
        self.password_edit = QLineEdit()
        self.password_edit.setEchoMode(QLineEdit.Password)
        self.password_edit.setPlaceholderText("请输入当前账号密码")
        self.password_edit.returnPressed.connect(self._reconnect)
        form.addRow("账号", username)
        form.addRow("密码", self.password_edit)
        layout.addLayout(form)

        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        self.reconnect_button = QPushButton("重新上线")
        self.reconnect_button.setObjectName("PrimaryButton")
        self.reconnect_button.clicked.connect(self._reconnect)
        buttons.addWidget(cancel)
        buttons.addWidget(self.reconnect_button)
        layout.addLayout(buttons)
        self.password_edit.setFocus()

    def _reconnect(self):
        password = self.password_edit.text()
        if not password:
            QMessageBox.warning(self, "密码为空", "请输入当前账号密码。")
            self.password_edit.setFocus()
            return
        try:
            account = run_with_loading(
                self,
                "正在重新上线…",
                lambda: self.session_manager.reauthenticate(password),
            )
        except AuthenticationError as exc:
            QMessageBox.warning(self, "重新上线失败", str(exc))
            self.password_edit.selectAll()
            self.password_edit.setFocus()
            return
        self.account = account
        self.password = password
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
            "测试账号与普通用户权限相同，但不记录或汇总业务统计。"
        )
        hint.setWordWrap(True)
        hint.setObjectName("Muted")
        layout.addWidget(hint)

        form = QFormLayout()
        account_label = QLabel(f"{account.name_label}（{account.username}）")
        self.role_combo = QComboBox()
        self.role_combo.addItem("普通用户", "user")
        self.role_combo.addItem("测试账号", "test")
        self.role_combo.addItem("管理员", "admin")
        account_type = "test" if account.is_test else account.role
        self.role_combo.setCurrentIndex(self.role_combo.findData(account_type))
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
        self.role_combo.addItem("测试账号", "test")
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

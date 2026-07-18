from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..database import Database, DatabaseError
from ..models import Account
from .auth_dialogs import CreateAccountDialog, PasswordDialog


class AccountPage(QWidget):
    def __init__(self, database: Database, current_account: Account, parent=None):
        super().__init__(parent)
        self.database = database
        self.current_account = current_account
        self.accounts = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 22)
        layout.setSpacing(12)

        title = QLabel("账号管理")
        title.setObjectName("PageTitle")
        layout.addWidget(title)
        subtitle = QLabel("创建管理员或普通用户，管理账号状态及重置密码。")
        subtitle.setObjectName("Muted")
        layout.addWidget(subtitle)

        action_layout = QHBoxLayout()
        create_btn = QPushButton("＋ 新建账号")
        create_btn.setObjectName("PrimaryButton")
        create_btn.clicked.connect(self._create_account)
        self.reset_btn = QPushButton("重置密码")
        self.reset_btn.clicked.connect(self._reset_password)
        self.toggle_btn = QPushButton("停用/启用")
        self.toggle_btn.clicked.connect(self._toggle_active)
        refresh_btn = QPushButton("刷新")
        refresh_btn.clicked.connect(self.refresh)
        action_layout.addWidget(create_btn)
        action_layout.addWidget(self.reset_btn)
        action_layout.addWidget(self.toggle_btn)
        action_layout.addStretch()
        action_layout.addWidget(refresh_btn)
        layout.addLayout(action_layout)

        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(
            ["用户名称", "账号", "角色", "状态", "创建时间", "最后登录", "首次改密"]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        layout.addWidget(self.table, 1)
        self.refresh()

    def refresh(self):
        self.accounts = self.database.list_accounts()
        self.table.setRowCount(len(self.accounts))
        for row, account in enumerate(self.accounts):
            values = [
                account.name_label,
                account.username,
                account.role_label,
                "正常" if account.is_active else "已停用",
                account.created_at.replace("T", " ")[:19],
                (account.last_login or "-").replace("T", " ")[:19],
                "是" if account.must_change_password else "否",
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                if col == 0:
                    item.setData(Qt.UserRole, account.id)
                if col == 3:
                    item.setForeground(Qt.darkGreen if account.is_active else Qt.red)
                self.table.setItem(row, col, item)
        self.table.resizeColumnsToContents()
        self._selection_changed()

    def _selected_account(self):
        row = self.table.currentRow()
        if row < 0 or row >= len(self.accounts):
            return None
        return self.accounts[row]

    def _selection_changed(self):
        account = self._selected_account()
        enabled = account is not None
        self.reset_btn.setEnabled(enabled)
        self.toggle_btn.setEnabled(enabled and account.id != self.current_account.id)
        if account:
            self.toggle_btn.setText("停用账号" if account.is_active else "启用账号")

    def _create_account(self):
        dialog = CreateAccountDialog(self)
        if dialog.exec_() != dialog.Accepted:
            return
        display_name, username, password, role = dialog.values()
        try:
            account = self.database.create_account(
                username,
                password,
                role,
                created_by=self.current_account.id,
                display_name=display_name,
            )
        except (ValueError, DatabaseError) as exc:
            QMessageBox.warning(self, "创建失败", str(exc))
            return
        QMessageBox.information(
            self,
            "创建成功",
            f"用户 {account.name_label}（{account.username}）已创建，首次登录时需要修改初始密码。",
        )
        self.refresh()

    def _reset_password(self):
        account = self._selected_account()
        if not account:
            return
        dialog = PasswordDialog(
            self.database,
            account.id,
            forced=False,
            must_change_after=True,
            parent=self,
        )
        if dialog.exec_() == dialog.Accepted:
            self.refresh()

    def _toggle_active(self):
        account = self._selected_account()
        if not account or account.id == self.current_account.id:
            return
        action = "停用" if account.is_active else "启用"
        reply = QMessageBox.question(
            self,
            "确认操作",
            f"确定要{action}账号“{account.username}”吗？",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        try:
            self.database.set_account_active(account.id, not account.is_active)
        except DatabaseError as exc:
            QMessageBox.warning(self, "操作失败", str(exc))
            return
        self.refresh()

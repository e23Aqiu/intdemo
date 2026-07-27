from __future__ import annotations

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..online.api import ApiResponseError, NetworkUnavailable
from .account_page import AccountPage
from .auth_dialogs import RenameAccountDialog
from .frameless import FramelessDialog
from .frameless import FramelessMessageBox as QMessageBox


_CALL_FAILED = object()


class _AccountSettingsDialog(FramelessDialog):
    def __init__(self, account=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("在线账号设置")
        self.setMinimumWidth(430)
        layout = QVBoxLayout(self)
        title = QLabel("编辑在线账号" if account else "新建在线账号")
        title.setObjectName("PageTitle")
        layout.addWidget(title)
        form = QFormLayout()
        self.username = QLineEdit(account.username if account else "")
        self.username.setEnabled(account is None)
        self.display_name = QLineEdit(account.name_label if account else "")
        self.role = QComboBox()
        self.role.addItem("普通用户", "user")
        self.role.addItem("管理员", "admin")
        self.scope = QComboBox()
        self.scope.addItem("仅本人数据", "own")
        self.scope.addItem("全部站点数据", "all")
        self.device_limit = QSpinBox()
        self.device_limit.setRange(1, 10000)
        self.active = QCheckBox("允许登录")
        if account:
            self.role.setCurrentIndex(self.role.findData(account.role))
            self.scope.setCurrentIndex(self.scope.findData(account.stats_scope))
            self.device_limit.setValue(int(getattr(account, "_device_limit", 1) or 1))
            self.active.setChecked(account.is_active)
        else:
            self.device_limit.setValue(1)
            self.active.setChecked(False)
        form.addRow("登录名", self.username)
        form.addRow("站点显示名", self.display_name)
        form.addRow("角色", self.role)
        form.addRow("数据范围", self.scope)
        form.addRow("设备上限", self.device_limit)
        form.addRow("状态", self.active)
        layout.addLayout(form)
        hint = QLabel(
            "新账号初始密码固定为 123456，首次登录必须修改。"
            if account is None
            else "设备上限不能低于当前有效设备数。"
        )
        hint.setObjectName("Muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        save = QPushButton("保存")
        save.setObjectName("PrimaryButton")
        save.clicked.connect(self._accept_if_valid)
        buttons.addWidget(cancel)
        buttons.addWidget(save)
        layout.addLayout(buttons)

    def _accept_if_valid(self):
        if not self.username.text().strip() or not self.display_name.text().strip():
            QMessageBox.warning(self, "资料不完整", "登录名和站点显示名不能为空。")
            return
        self.accept()

    def values(self) -> dict:
        return {
            "username": self.username.text().strip().lower(),
            "display_name": self.display_name.text().strip(),
            "role": self.role.currentData(),
            "stats_scope": self.scope.currentData(),
            "device_limit": self.device_limit.value(),
            "is_active": self.active.isChecked(),
        }


class _DevicesDialog(FramelessDialog):
    def __init__(self, page, account, parent=None):
        super().__init__(parent)
        self.page = page
        self.account = account
        self.setWindowTitle("设备管理")
        self.resize(760, 430)
        layout = QVBoxLayout(self)
        title = QLabel(f"{account.name_label} · 已登记设备")
        title.setObjectName("PageTitle")
        layout.addWidget(title)
        hint = QLabel(
            "撤销会立即终止该设备的在线会话；"
            "已离线设备最长仍可能使用到其 7 天授权过期。"
        )
        hint.setObjectName("Muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ["设备名", "客户端", "最近在线", "登记时间", "状态"]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        layout.addWidget(self.table)
        buttons = QHBoxLayout()
        revoke = QPushButton("撤销所选设备")
        revoke.setObjectName("DangerButton")
        revoke.clicked.connect(self._revoke)
        close = QPushButton("关闭")
        close.clicked.connect(self.accept)
        buttons.addWidget(revoke)
        buttons.addStretch()
        buttons.addWidget(close)
        layout.addLayout(buttons)
        self.devices = []
        self.refresh()

    def refresh(self):
        try:
            token = self.page.session.access_token()
            self.devices = self.page.session.api.admin_devices(
                token, self.account.server_account_id
            )
        except (ApiResponseError, NetworkUnavailable) as exc:
            QMessageBox.warning(self, "读取失败", str(exc))
            return
        self.table.setRowCount(len(self.devices))
        for row, device in enumerate(self.devices):
            values = [
                device.get("name") or "-",
                device.get("client_version") or "-",
                str(device.get("last_seen_at") or "-").replace("T", " ")[:19],
                str(device.get("created_at") or "-").replace("T", " ")[:19],
                "已撤销" if device.get("revoked_at") else "有效",
            ]
            for column, value in enumerate(values):
                self.table.setItem(row, column, QTableWidgetItem(str(value)))

    def _revoke(self):
        row = self.table.currentRow()
        if row < 0 or row >= len(self.devices):
            return
        device = self.devices[row]
        if device.get("revoked_at"):
            return
        reply = QMessageBox.question(
            self,
            "确认撤销设备",
            f"确定撤销设备“{device.get('name') or device.get('device_uid')}”吗？",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        try:
            token = self.page.session.access_token()
            self.page.session.api.admin_revoke_device(token, device["id"])
        except (ApiResponseError, NetworkUnavailable) as exc:
            QMessageBox.warning(self, "撤销失败", str(exc))
            return
        self.refresh()


class _AuditDialog(FramelessDialog):
    ACTION_LABELS = {
        "account.create": "创建账号",
        "account.update": "修改账号",
        "account.archive": "归档账号",
        "account.restore": "恢复账号",
        "account.password_reset": "重置密码",
        "account.data_reset": "重置统计",
        "device.revoke": "撤销设备",
    }

    def __init__(self, page, parent=None):
        super().__init__(parent)
        self.page = page
        self.setWindowTitle("管理员审计记录")
        self.resize(940, 520)
        layout = QVBoxLayout(self)
        title = QLabel("管理员审计记录")
        title.setObjectName("PageTitle")
        layout.addWidget(title)
        hint = QLabel("展示最近 200 条在线管理操作，记录由服务器保存。")
        hint.setObjectName("Muted")
        layout.addWidget(hint)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["时间", "操作", "目标", "来源 IP", "请求编号", "详情"]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.Stretch)
        header.setSectionResizeMode(5, QHeaderView.Stretch)
        layout.addWidget(self.table, 1)
        buttons = QHBoxLayout()
        refresh = QPushButton("刷新")
        refresh.clicked.connect(self.refresh)
        close = QPushButton("关闭")
        close.clicked.connect(self.accept)
        buttons.addWidget(refresh)
        buttons.addStretch()
        buttons.addWidget(close)
        layout.addLayout(buttons)
        self.refresh()

    def refresh(self):
        payload = self.page._call(self.page.session.api.admin_audit, 200)
        if payload is _CALL_FAILED:
            return
        rows = payload.get("items") or []
        self.table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            details = row.get("details") or {}
            if isinstance(details, dict):
                details_text = "，".join(
                    f"{key}={value}" for key, value in details.items()
                )
            else:
                details_text = str(details)
            values = [
                str(row.get("created_at") or "-").replace("T", " ")[:19],
                self.ACTION_LABELS.get(
                    str(row.get("action") or ""),
                    str(row.get("action") or "-"),
                ),
                str(row.get("target_id") or "-"),
                str(row.get("ip_address") or "-"),
                str(row.get("request_id") or "-"),
                details_text or "-",
            ]
            for column, value in enumerate(values):
                self.table.setItem(
                    row_index,
                    column,
                    QTableWidgetItem(value),
                )


class OnlineAccountPage(AccountPage):
    """Offline account management UI extended with server-only controls."""

    def __init__(self, database, current_account, session, parent=None):
        self.session = session
        self.server_rows = {}
        super().__init__(
            database,
            current_account,
            parent,
            defer_refresh=True,
        )

        self.page_subtitle.setText(
            "保留离线版账号管理操作，并增加数据范围、设备、归档和审计功能。"
        )
        self.list_title.setText("在线账号列表")

        online_card, online_value = self._summary_card(
            "当前在线设备",
            "#3478f6",
        )
        self.summary_values["online"] = online_value
        self.summary_layout.addWidget(online_card, 1)

        self.create_btn.setText("＋ 新建在线账号")
        self.rename_btn.setText("修改名称")
        self.permission_btn.setText("权限与设备")
        self.reset_btn.setText("重置为 123456")
        self.toggle_btn.setText("停用/启用")
        self.delete_btn.setText("归档账号")
        self.delete_btn.setObjectName("DangerButton")

        self.devices_btn = QPushButton("设备列表")
        self.devices_btn.clicked.connect(self._devices)
        delete_index = self.account_action_layout.indexOf(self.delete_btn)
        self.account_action_layout.insertWidget(delete_index, self.devices_btn)

        self.audit_btn = QPushButton("审计记录")
        self.audit_btn.clicked.connect(self._show_audit)
        self.account_action_layout.insertWidget(1, self.audit_btn)

        self.reset_stats_btn.setText("重置在线统计")
        self.reset_stats_btn.setToolTip(
            "清除所选账号在服务器上的全部汇总数据，并写入审计记录"
        )

        # Compatibility aliases retained for existing automation and callers.
        self.edit_btn = self.permission_btn
        self.password_btn = self.reset_btn
        self.archive_btn = self.delete_btn

        self.table.setColumnCount(11)
        self.table.setHorizontalHeaderLabels(
            [
                "用户名称",
                "登录名",
                "角色",
                "数据范围",
                "状态",
                "设备",
                "在线",
                "创建时间",
                "最后登录",
                "首次改密",
                "归档",
            ]
        )
        header = self.table.horizontalHeader()
        for column in (0, 1, 7, 8):
            header.setSectionResizeMode(column, QHeaderView.Stretch)
        for column in (2, 3, 4, 5, 6, 9, 10):
            header.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        # Do not perform a blocking HTTPS request while MainWindow is still
        # being constructed. MainWindow calls refresh() when the user opens
        # the account page, after the initial event loop has started.
        self._selection_changed()

    def _call(self, function, *args):
        try:
            token = self.session.access_token()
            return function(token, *args)
        except (ApiResponseError, NetworkUnavailable) as exc:
            QMessageBox.warning(self, "在线操作失败", str(exc))
            return _CALL_FAILED

    def refresh(self):
        rows = self._call(self.session.api.admin_accounts)
        if rows is _CALL_FAILED:
            return
        selected = self._selected_account()
        selected_server_id = selected.server_account_id if selected else None
        self.accounts = []
        self.server_rows = {}
        for server_row in rows:
            account = self.database.upsert_remote_account(server_row)
            object.__setattr__(
                account,
                "_device_limit",
                int(server_row.get("device_limit") or 1),
            )
            self.accounts.append(account)
            self.server_rows[account.server_account_id] = server_row
        self._refresh_summary()
        self.summary_values["online"].setText(
            str(
                sum(
                    int(row.get("online_device_count") or 0)
                    for row in rows
                )
            )
        )
        self.table.blockSignals(True)
        self.table.clearSelection()
        self.table.setRowCount(len(self.accounts))
        for row, account in enumerate(self.accounts):
            server = self.server_rows[account.server_account_id]
            values = [
                account.name_label,
                account.username,
                account.role_label,
                "全部" if account.stats_scope == "all" else "本人",
                "启用" if account.is_active else "停用",
                (
                    f"{server.get('active_device_count', 0)}/"
                    f"{server.get('device_limit', 1)}"
                ),
                str(server.get("online_device_count", 0)),
                str(server.get("created_at") or account.created_at)
                .replace("T", " ")[:19],
                (account.last_login or "-").replace("T", " ")[:19],
                "需要修改" if account.must_change_password else "已设置",
                "已归档" if account.is_archived else "-",
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if column == 0:
                    item.setData(Qt.UserRole, account.id)
                if column == 2:
                    item.setForeground(
                        QColor("#6845bd")
                        if account.is_admin
                        else QColor("#26718b")
                    )
                if column == 4:
                    item.setForeground(
                        QColor("#188b57")
                        if account.is_active and not account.is_archived
                        else QColor("#d33f49")
                    )
                if column == 6 and int(server.get("online_device_count") or 0):
                    item.setForeground(QColor("#188b57"))
                if column == 9 and account.must_change_password:
                    item.setForeground(QColor("#b26a00"))
                if column == 10 and account.is_archived:
                    item.setForeground(QColor("#d33f49"))
                item.setTextAlignment(
                    Qt.AlignLeft | Qt.AlignVCenter
                    if column in (0, 1)
                    else Qt.AlignCenter
                )
                self.table.setItem(row, column, item)
            if account.server_account_id == selected_server_id:
                self.table.selectRow(row)
        self.table.blockSignals(False)
        self._selection_changed()

    def _selection_changed(self):
        account = self._selected_account()
        selected = account is not None
        other = selected and account.id != self.current_account.id
        is_station = selected and not account.is_admin
        self.rename_btn.setEnabled(selected)
        self.permission_btn.setEnabled(selected)
        self.password_btn.setEnabled(other)
        self.devices_btn.setEnabled(selected)
        self.toggle_btn.setEnabled(other and not account.is_archived if account else False)
        self.archive_btn.setEnabled(other)
        self.export_btn.setEnabled(is_station)
        self.import_btn.setEnabled(is_station)
        self.reset_stats_btn.setEnabled(other and is_station)
        self.data_range_selector.setEnabled(is_station)
        self.archive_btn.setText(
            "恢复账号" if account and account.is_archived else "归档账号"
        )
        self.toggle_btn.setText(
            "停用账号"
            if account and account.is_active
            else "启用账号"
        )
        if account:
            server = self.server_rows.get(account.server_account_id, {})
            state = (
                "已归档"
                if account.is_archived
                else ("正常" if account.is_active else "已停用")
            )
            suffix = (
                " · 当前登录账号"
                if account.id == self.current_account.id
                else ""
            )
            self.selection_hint.setText(
                f"已选择：{account.name_label} · {account.role_label} · {state}"
                f" · 数据范围 {'全部' if account.stats_scope == 'all' else '本人'}"
                f" · 设备 {server.get('active_device_count', 0)}/"
                f"{server.get('device_limit', 1)}{suffix}"
            )
        else:
            self.selection_hint.setText("请选择一个账号进行管理")
            self.toggle_btn.setText("停用/启用")
        self.import_btn.setToolTip(
            "在线版不允许只导入本机缓存；点击可查看说明"
            if is_station
            else "请选择普通用户站点"
        )
        self.export_btn.setToolTip(
            "导出当前客户端已同步到的站点汇总数据"
            if is_station
            else "请选择普通用户站点"
        )
        self.toggle_btn.setObjectName(
            "DangerButton"
            if other and account and account.is_active
            else ("PrimaryButton" if other and account else "")
        )
        self.toggle_btn.style().unpolish(self.toggle_btn)
        self.toggle_btn.style().polish(self.toggle_btn)

    def _create_account(self):
        dialog = _AccountSettingsDialog(parent=self)
        if dialog.exec_() != dialog.Accepted:
            return
        result = self._call(
            self.session.api.admin_create_account,
            dialog.values(),
        )
        if result is not _CALL_FAILED:
            QMessageBox.information(
                self,
                "创建成功",
                "账号已创建，初始密码为 123456；请按需启用账号。",
            )
            self.refresh()

    def _rename_account(self):
        account = self._selected_account()
        if not account:
            return
        dialog = RenameAccountDialog(account, self)
        if dialog.exec_() != dialog.Accepted:
            return
        display_name = dialog.value()
        result = self._call(
            self.session.api.admin_update_account,
            account.server_account_id,
            {"display_name": display_name},
        )
        if result is not _CALL_FAILED:
            self.account_name_changed.emit(account.id, display_name)
            self.refresh()

    def _change_permission(self):
        account = self._selected_account()
        if not account:
            return
        dialog = _AccountSettingsDialog(account, self)
        if dialog.exec_() != dialog.Accepted:
            return
        payload = dialog.values()
        payload.pop("username", None)
        result = self._call(
            self.session.api.admin_update_account,
            account.server_account_id,
            payload,
        )
        if result is not _CALL_FAILED:
            self.account_name_changed.emit(
                account.id,
                payload["display_name"],
            )
            self.account_permission_changed.emit(
                account.id,
                payload["role"],
            )
            self.refresh()

    def _reset_password(self):
        account = self._selected_account()
        if not account:
            return
        reply = QMessageBox.question(
            self,
            "确认重置密码",
            "密码将恢复为 123456，全部在线会话会被撤销，并要求下次登录改密。",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            result = self._call(
                self.session.api.admin_reset_password,
                account.server_account_id,
            )
            if result is not _CALL_FAILED:
                QMessageBox.information(
                    self,
                    "重置完成",
                    "密码已恢复为 123456，用户下次登录必须修改密码。",
                )
                self.refresh()

    def _toggle_active(self):
        account = self._selected_account()
        if not account or account.id == self.current_account.id:
            return
        enabled = not account.is_active
        action = "启用" if enabled else "停用"
        reply = QMessageBox.question(
            self,
            f"确认{action}",
            f"确定要{action}账号“{account.username}”吗？",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        result = self._call(
            self.session.api.admin_update_account,
            account.server_account_id,
            {"is_active": enabled},
        )
        if result is not _CALL_FAILED:
            self.refresh()

    def _devices(self):
        account = self._selected_account()
        if account:
            _DevicesDialog(self, account, self).exec_()
            self.refresh()

    def _delete_account(self):
        account = self._selected_account()
        if not account:
            return
        action = "恢复" if account.is_archived else "归档"
        reply = QMessageBox.question(
            self,
            f"确认{action}",
            (
                "恢复后账号仍保持停用，需要在“编辑设置”中单独启用。"
                if account.is_archived
                else "归档会禁止登录并终止在线会话，但历史统计会保留。"
            ),
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        method = (
            self.session.api.admin_restore_account
            if account.is_archived
            else self.session.api.admin_archive_account
        )
        if self._call(method, account.server_account_id) is not _CALL_FAILED:
            self.account_deleted.emit(account.id)
            self.refresh()

    def _reset_station_statistics(self):
        account = self._selected_account()
        if not account:
            return
        reply = QMessageBox.question(
            self,
            "确认重置统计数据",
            f"确定清除“{account.name_label}”的所有在线汇总数据吗？\n"
            "此操作独立于归档，并会写入审计记录；本机原始 Excel 不受影响。",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        result = self._call(
            self.session.api.admin_reset_data,
            account.server_account_id,
        )
        if result is not _CALL_FAILED:
            self.station_data_changed.emit(account.id)
            QMessageBox.information(
                self,
                "重置完成",
                "服务器汇总数据已清除；账号、设备及本机原始文件不受影响。",
            )
            self.refresh()

    def _import_station_data(self):
        account = self._selected_account()
        if not account or account.is_admin:
            return
        QMessageBox.information(
            self,
            "在线版导入说明",
            "为避免形成“只在管理员电脑可见”的本机数据，在线测试版暂不执行"
            "管理员代导入。\n\n"
            "当前仍可导出已同步汇总数据；各站点产生的新数据会通过同步队列"
            "自动上传服务器。",
        )

    def _show_audit(self):
        _AuditDialog(self, self).exec_()

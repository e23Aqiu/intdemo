from datetime import datetime
from pathlib import Path

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QFileDialog,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..database import Database, DatabaseError
from ..models import Account
from .auth_dialogs import (
    AccountPermissionDialog,
    CreateAccountDialog,
    PasswordDialog,
    RenameAccountDialog,
)
from .date_range import DateRangeSelector
from .frameless import FramelessMessageBox as QMessageBox


class AccountPage(QWidget):
    account_name_changed = pyqtSignal(int, str)
    account_permission_changed = pyqtSignal(int, str)
    account_deleted = pyqtSignal(int)
    station_data_changed = pyqtSignal(int)

    def __init__(self, database: Database, current_account: Account, parent=None):
        super().__init__(parent)
        self.database = database
        self.current_account = current_account
        self.accounts = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 22)
        layout.setSpacing(14)

        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title = QLabel("账号管理")
        title.setObjectName("PageTitle")
        title_box.addWidget(title)
        subtitle = QLabel("集中管理用户身份、登录状态与密码安全。")
        subtitle.setObjectName("Muted")
        title_box.addWidget(subtitle)
        header.addLayout(title_box)
        header.addStretch()
        refresh_btn = QPushButton("刷新数据")
        refresh_btn.clicked.connect(self.refresh)
        header.addWidget(refresh_btn)
        layout.addLayout(header)

        summary_layout = QHBoxLayout()
        summary_layout.setSpacing(10)
        self.summary_values = {}
        for key, label, color in (
            ("total", "账号总数", "#1d8178"),
            ("admin", "管理员", "#d18400"),
            ("user", "普通用户", "#3f8f88"),
            ("active", "正常账号", "#1d8d70"),
        ):
            card, value = self._summary_card(label, color)
            self.summary_values[key] = value
            summary_layout.addWidget(card, 1)
        layout.addLayout(summary_layout)

        list_card = QFrame()
        list_card.setObjectName("Card")
        card_layout = QVBoxLayout(list_card)
        card_layout.setContentsMargins(16, 14, 16, 16)
        card_layout.setSpacing(12)

        list_header = QHBoxLayout()
        list_title = QLabel("账号列表")
        list_title.setStyleSheet("font-size:16px;font-weight:700;color:#173a3d;")
        list_header.addWidget(list_title)
        list_header.addStretch()
        self.selection_hint = QLabel("请选择一个账号进行管理")
        self.selection_hint.setObjectName("Muted")
        list_header.addWidget(self.selection_hint)
        card_layout.addLayout(list_header)

        action_layout = QHBoxLayout()
        self.create_btn = QPushButton("＋ 新建账号")
        self.create_btn.setObjectName("PrimaryButton")
        self.create_btn.clicked.connect(self._create_account)
        self.reset_btn = QPushButton("重置密码")
        self.reset_btn.clicked.connect(self._reset_password)
        self.rename_btn = QPushButton("修改名称")
        self.rename_btn.clicked.connect(self._rename_account)
        self.permission_btn = QPushButton("修改权限")
        self.permission_btn.clicked.connect(self._change_permission)
        self.delete_btn = QPushButton("删除账号")
        self.delete_btn.setObjectName("DangerButton")
        self.delete_btn.clicked.connect(self._delete_account)
        self.export_btn = QPushButton("导出数据")
        self.export_btn.clicked.connect(self._export_station_data)
        self.import_btn = QPushButton("导入数据")
        self.import_btn.clicked.connect(self._import_station_data)
        self.reset_stats_btn = QPushButton("重置统计")
        self.reset_stats_btn.setObjectName("DangerButton")
        self.reset_stats_btn.clicked.connect(self._reset_station_statistics)
        self.toggle_btn = QPushButton("停用/启用")
        self.toggle_btn.clicked.connect(self._toggle_active)
        action_layout.addWidget(self.create_btn)
        action_layout.addStretch()
        action_layout.addWidget(self.rename_btn)
        action_layout.addWidget(self.permission_btn)
        action_layout.addWidget(self.reset_btn)
        action_layout.addWidget(self.toggle_btn)
        action_layout.addWidget(self.delete_btn)
        card_layout.addLayout(action_layout)

        data_bar = QFrame()
        data_bar.setObjectName("DataActionBar")
        data_bar.setStyleSheet(
            "QFrame#DataActionBar{background:#f5f8f7;border:1px solid #d8e7e3;"
            "border-radius:8px;}"
            "QLabel#DataActionTitle{border:none;background:transparent;"
            "font-weight:700;color:#315453;}"
        )
        data_layout = QHBoxLayout(data_bar)
        data_layout.setContentsMargins(12, 8, 10, 8)
        data_layout.setSpacing(8)
        data_title = QLabel("统计数据")
        data_title.setObjectName("DataActionTitle")
        data_layout.addWidget(data_title)
        self.data_range_selector = DateRangeSelector()
        data_layout.addWidget(self.data_range_selector)
        data_layout.addStretch()
        data_layout.addWidget(self.export_btn)
        data_layout.addWidget(self.import_btn)
        data_layout.addWidget(self.reset_stats_btn)
        card_layout.addWidget(data_bar)

        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(
            ["用户名称", "账号", "角色", "状态", "创建时间", "最后登录", "首次改密"]
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(42)
        header_view = self.table.horizontalHeader()
        for column in (0, 1, 4, 5):
            header_view.setSectionResizeMode(column, QHeaderView.Stretch)
        for column in (2, 3, 6):
            header_view.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        card_layout.addWidget(self.table, 1)
        layout.addWidget(list_card, 1)
        self.refresh()

    @staticmethod
    def _summary_card(label, color):
        card = QFrame()
        card.setObjectName("Card")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(16, 11, 16, 11)
        caption = QLabel(label)
        caption.setObjectName("Muted")
        value = QLabel("0")
        value.setStyleSheet(f"font-size:24px;font-weight:700;color:{color};")
        card_layout.addWidget(caption)
        card_layout.addWidget(value)
        return card, value

    def _refresh_summary(self):
        self.summary_values["total"].setText(str(len(self.accounts)))
        self.summary_values["admin"].setText(
            str(sum(account.is_admin for account in self.accounts))
        )
        self.summary_values["user"].setText(
            str(sum(not account.is_admin for account in self.accounts))
        )
        self.summary_values["active"].setText(
            str(sum(account.is_active for account in self.accounts))
        )

    def refresh(self):
        selected = self._selected_account()
        selected_id = selected.id if selected else None
        self.accounts = self.database.list_accounts()
        self._refresh_summary()
        self.table.blockSignals(True)
        self.table.clearSelection()
        self.table.setRowCount(len(self.accounts))
        for row, account in enumerate(self.accounts):
            values = [
                account.name_label,
                account.username,
                account.role_label,
                "● 正常" if account.is_active else "● 已停用",
                account.created_at.replace("T", " ")[:19],
                (account.last_login or "-").replace("T", " ")[:19],
                "需要修改" if account.must_change_password else "已设置",
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                if col == 0:
                    item.setData(Qt.UserRole, account.id)
                if col in {2, 3, 4, 5, 6}:
                    item.setTextAlignment(Qt.AlignCenter)
                if col == 2:
                    if account.is_admin:
                        item.setForeground(QColor("#6845bd"))
                        item.setBackground(QColor("#f3efff"))
                    else:
                        item.setForeground(QColor("#26718b"))
                        item.setBackground(QColor("#edf8fb"))
                if col == 3:
                    item.setForeground(
                        QColor("#188b57") if account.is_active else QColor("#d33f49")
                    )
                if col == 6 and account.must_change_password:
                    item.setForeground(QColor("#b26a00"))
                self.table.setItem(row, col, item)
            if account.id == selected_id:
                self.table.selectRow(row)
        self.table.blockSignals(False)
        self._selection_changed()

    def _selected_account(self):
        row = self.table.currentRow()
        if row < 0 or row >= len(self.accounts):
            return None
        return self.accounts[row]

    def _selection_changed(self):
        account = self._selected_account()
        enabled = account is not None
        can_manage = enabled and account.id != self.current_account.id
        is_station = enabled and not account.is_admin
        self.rename_btn.setEnabled(enabled)
        self.permission_btn.setEnabled(can_manage)
        self.export_btn.setEnabled(is_station)
        self.import_btn.setEnabled(is_station)
        self.reset_stats_btn.setEnabled(is_station)
        self.data_range_selector.setEnabled(is_station)
        self.reset_btn.setEnabled(can_manage)
        self.toggle_btn.setEnabled(can_manage)
        self.delete_btn.setEnabled(can_manage)
        if account:
            status = "正常" if account.is_active else "已停用"
            suffix = " · 当前登录账号" if account.id == self.current_account.id else ""
            self.selection_hint.setText(
                f"已选择：{account.name_label} · {account.role_label} · {status}{suffix}"
            )
            self.toggle_btn.setText("停用账号" if account.is_active else "启用账号")
            self.toggle_btn.setObjectName(
                ("DangerButton" if account.is_active else "PrimaryButton")
                if can_manage else ""
            )
            self.rename_btn.setToolTip("修改用户名称，登录账号保持不变")
            self.permission_btn.setToolTip(
                "当前登录账号不能修改权限"
                if account.id == self.current_account.id
                else "切换管理员或普通用户权限"
            )
            self.delete_btn.setToolTip(
                "当前登录账号不能删除"
                if account.id == self.current_account.id
                else "永久删除所选账号及其全部统计数据"
            )
            station_tip = (
                f"所选站点：{account.name_label}"
                if is_station
                else "仅普通用户站点支持统计数据操作"
            )
            self.export_btn.setToolTip(f"按当前日期范围导出统计数据。{station_tip}")
            self.import_btn.setToolTip(f"按当前日期范围导入统计数据。{station_tip}")
            self.reset_stats_btn.setToolTip(
                f"清除当前日期范围内的统计事件和业务明细。{station_tip}"
            )
            if account.id == self.current_account.id:
                self.reset_btn.setToolTip("请在个人中心修改当前账号密码")
                self.toggle_btn.setToolTip("当前登录账号不能在此停用")
            else:
                self.reset_btn.setToolTip("为所选账号设置临时密码")
                self.toggle_btn.setToolTip("")
        else:
            self.selection_hint.setText("请选择一个账号进行管理")
            self.toggle_btn.setText("停用/启用")
            self.toggle_btn.setObjectName("")
            self.reset_btn.setToolTip("")
            self.rename_btn.setToolTip("")
            self.permission_btn.setToolTip("")
            self.delete_btn.setToolTip("")
            self.export_btn.setToolTip("")
            self.import_btn.setToolTip("")
            self.reset_stats_btn.setToolTip("")
            self.toggle_btn.setToolTip("")
        self.toggle_btn.style().unpolish(self.toggle_btn)
        self.toggle_btn.style().polish(self.toggle_btn)

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

    def _rename_account(self):
        account = self._selected_account()
        if not account:
            return
        dialog = RenameAccountDialog(account, self)
        if dialog.exec_() != dialog.Accepted:
            return
        try:
            renamed = self.database.update_account_display_name(
                account.id,
                dialog.value(),
            )
        except (ValueError, DatabaseError) as exc:
            QMessageBox.warning(self, "修改失败", str(exc))
            return
        self.account_name_changed.emit(renamed.id, renamed.name_label)
        QMessageBox.information(
            self,
            "修改成功",
            f"用户名称已修改为“{renamed.name_label}”，登录账号仍为“{renamed.username}”。",
        )
        self.refresh()

    def _change_permission(self):
        account = self._selected_account()
        if not account or account.id == self.current_account.id:
            return
        dialog = AccountPermissionDialog(account, self)
        if dialog.exec_() != dialog.Accepted:
            return
        try:
            updated = self.database.update_account_role(
                account.id,
                dialog.value(),
            )
        except (ValueError, DatabaseError) as exc:
            QMessageBox.warning(self, "修改失败", str(exc))
            return
        self.account_permission_changed.emit(updated.id, updated.role)
        QMessageBox.information(
            self,
            "修改成功",
            f"账号“{updated.username}”的权限已修改为{updated.role_label}。",
        )
        self.refresh()

    def _export_station_data(self):
        account = self._selected_account()
        if not account or account.is_admin:
            return
        start_date, end_date = self.data_range_selector.date_range()
        range_label = self.data_range_selector.range_label()
        range_suffix = (
            "all"
            if start_date is None
            else f"{start_date:%Y%m%d}-{end_date:%Y%m%d}"
        )
        default_name = (
            f"{account.username}_data_{range_suffix}_{datetime.now():%Y%m%d_%H%M%S}.json"
        )
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            f"导出 {account.name_label} 数据",
            default_name,
            "站点数据文件 (*.json)",
        )
        if not file_path:
            return
        target = Path(file_path)
        if target.suffix.lower() != ".json":
            target = target.with_suffix(".json")
        try:
            result = self.database.export_station_data(
                account.id,
                target,
                start_date,
                end_date,
            )
        except (ValueError, DatabaseError) as exc:
            QMessageBox.warning(self, "导出失败", str(exc))
            return
        QMessageBox.information(
            self,
            "导出成功",
            f"日期范围：{range_label}\n"
            f"已导出 {result['event_count']} 条统计事件。\n{result['file_path']}",
        )

    def _import_station_data(self):
        account = self._selected_account()
        if not account or account.is_admin:
            return
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            f"导入数据到 {account.name_label}",
            "",
            "站点数据文件 (*.json)",
        )
        if not file_path:
            return
        start_date, end_date = self.data_range_selector.date_range()
        range_label = self.data_range_selector.range_label()
        reply = QMessageBox.question(
            self,
            "确认导入",
            f"确定将文件中的统计数据导入“{account.name_label}”吗？\n"
            f"日期范围：{range_label}\n"
            "已存在的相同事件会自动跳过。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        try:
            result = self.database.import_station_data(
                account.id,
                Path(file_path),
                start_date,
                end_date,
            )
        except (ValueError, DatabaseError) as exc:
            QMessageBox.warning(self, "导入失败", str(exc))
            return
        self.station_data_changed.emit(account.id)
        source = result["source_station"]
        source_name = source["display_name"] or source["username"] or "未知站点"
        QMessageBox.information(
            self,
            "导入完成",
            f"来源：{source_name}\n"
            f"日期范围：{range_label}\n"
            f"新增 {result['imported']} 条，跳过 {result['skipped']} 条重复事件，"
            f"范围外 {result['filtered_out']} 条。",
        )

    def _reset_station_statistics(self):
        account = self._selected_account()
        if not account or account.is_admin:
            return
        start_date, end_date = self.data_range_selector.date_range()
        range_label = self.data_range_selector.range_label()
        reply = QMessageBox.question(
            self,
            "确认重置统计数据",
            f"确定清除“{account.name_label}”（{account.username}）的统计数据吗？\n"
            f"日期范围：{range_label}\n\n"
            "所选范围内的统计事件及业务明细将永久删除；"
            "账号、密码、权限和状态会保留。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        try:
            result = self.database.reset_station_statistics(
                account.id,
                start_date,
                end_date,
            )
        except (ValueError, DatabaseError) as exc:
            QMessageBox.warning(self, "重置失败", str(exc))
            return
        self.station_data_changed.emit(account.id)
        QMessageBox.information(
            self,
            "重置完成",
            f"日期范围：{range_label}\n"
            f"已清除 {result['deleted']} 条统计事件。\n"
            f"“{account.name_label}”的账号信息保持不变。",
        )

    def _delete_account(self):
        account = self._selected_account()
        if not account or account.id == self.current_account.id:
            return
        reply = QMessageBox.question(
            self,
            "确认删除账号",
            f"确定永久删除“{account.name_label}”（{account.username}）吗？\n\n"
            "该账号及其全部统计事件和业务明细都会被删除，此操作无法恢复。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        try:
            result = self.database.delete_account(account.id)
        except DatabaseError as exc:
            QMessageBox.warning(self, "删除失败", str(exc))
            return
        self.account_deleted.emit(account.id)
        self.refresh()
        QMessageBox.information(
            self,
            "删除完成",
            f"账号“{account.name_label}”已删除，同时清除 "
            f"{result['deleted_events']} 条统计事件。",
        )

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

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pandas as pd
from PyQt5.QtCore import QEvent, QPoint, Qt
from PyQt5.QtGui import QMouseEvent
from PyQt5.QtWidgets import QApplication, QMessageBox, QPushButton
from openpyxl import Workbook

from integrated_client.config import DEFAULT_ADMIN_PASSWORD, DEFAULT_ADMIN_USERNAME
from integrated_client.database import (
    DEFAULT_STATION_USERS,
    Database,
    WORKFLOW_EMPTY_METRIC,
    WORKFLOW_HAS_PHONE_METRIC,
    WORKFLOW_INDIVIDUAL_METRIC,
    WORKFLOW_NO_OPERATION_METRIC,
    WORKFLOW_NO_PHONE_METRIC,
    WORKFLOW_NO_TRANSPORT_METRIC,
    WORKFLOW_TOTAL_METRIC,
)
from integrated_client.tools.aiqicha_tool import MainWindow as AiqichaToolWidget
from integrated_client.tools.aiqicha_tool import (
    ADDR_COL_NAME,
    LEGAL_COL_NAME,
    PHONE_COL_NAME,
    TARGET_COLUMNS,
    has_meaningful_value,
)
from integrated_client.ui.main_window import MainWindow
from integrated_client.ui.auth_dialogs import PasswordDialog
from integrated_client.ui.statistics_page import (
    StationDistributionChart,
    ViolationReasonChart,
    WorkflowDistributionChart,
)
from integrated_client.ui.workflow_page import DataFrameTableModel, WorkflowPage


class ToolAndUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    @classmethod
    def tearDownClass(cls):
        # PyQt 在解释器关闭阶段回收仍存活的主窗口和全局计时器时，
        # Windows 下可能返回非零退出码；显式释放可让测试结果稳定传给 CI。
        for widget in list(cls.app.topLevelWidgets()):
            workflow = getattr(widget, "workflow_page", None)
            if workflow is not None:
                workflow.shutdown()
            if hasattr(widget, "_prepared_to_close"):
                widget._prepared_to_close = True
            widget.close()
            widget.deleteLater()
        cls.app.processEvents()
        cursor_filter = getattr(cls.app, "_intdemo_disabled_cursor_filter", None)
        if cursor_filter is not None:
            cursor_filter.stop()
            cls.app.removeEventFilter(cursor_filter)
        cls.app.processEvents()

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp_dir.name) / "test.db")
        self.db.ensure_default_admin()
        self.admin = self.db.authenticate(DEFAULT_ADMIN_USERNAME, DEFAULT_ADMIN_PASSWORD)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_value_detection_and_target_header_creation(self):
        self.assertTrue(has_meaningful_value("张三"))
        self.assertFalse(has_meaningful_value("nan"))
        self.assertFalse(has_meaningful_value("未查询到公司"))

        workbook = Workbook()
        sheet = workbook.active
        sheet.cell(1, 1, "车辆所有人/企业")
        header = AiqichaToolWidget._ensure_target_headers(sheet)
        for name in TARGET_COLUMNS:
            self.assertIn(name, header)
            self.assertEqual(sheet.cell(1, header[name]).value, name)

    def test_main_window_contains_integrated_pages(self):
        window = MainWindow(self.db, self.admin)
        self.assertIs(window._pages["home"], window.statistics_page)
        self.assertIn("workflow", window._pages)
        self.assertIs(window._pages["workflow"], window.workflow_page)
        self.assertIs(window._pages["personal"], window.personal_center_page)
        self.assertNotIn("transport", window._pages)
        self.assertNotIn("aiqicha", window._pages)
        self.assertNotIn("statistics", window._pages)
        self.assertIn("accounts", window._pages)
        self.assertNotIn("statistics", window._nav_buttons)
        self.assertIn("workflow", window._nav_buttons)
        self.assertIn("personal", window._nav_buttons)
        self.assertIs(window.stack.currentWidget(), window.statistics_page)
        self.assertTrue(window._nav_buttons["home"].isChecked())
        self.assertEqual(window.page_title.text(), "数据仪表盘")
        self.assertEqual(
            [
                window.statistics_page.category_combo.itemData(index)
                for index in range(window.statistics_page.category_combo.count())
            ],
            ["station_distribution", "completion", "violation"],
        )
        self.assertEqual(
            window.statistics_page.category_combo.currentData(),
            "station_distribution",
        )
        self.assertFalse(window.statistics_page.station_combo.isEnabled())
        self.assertFalse(window.statistics_page.station_distribution_chart.isHidden())
        self.assertEqual(window.top_identity.text(), "管理员 · 系统管理员")
        self.assertEqual(window.sidebar_user.text(), "系统管理员")
        self.assertEqual(window.personal_center_page.identity_value.text(), "管理员")
        self.assertEqual(window.personal_center_page.name_value.text(), "系统管理员")
        self.assertEqual(window.personal_center_page.username_value.text(), "admin")
        password_dialog = PasswordDialog(
            self.db, self.admin.id, require_current=True
        )
        self.assertIsNotNone(password_dialog.current_password_edit)
        self.assertEqual(
            password_dialog.current_password_edit.placeholderText(),
            "请输入当前登录密码",
        )
        password_dialog.close()
        window.show_page("personal")
        self.assertIs(window.stack.currentWidget(), window.personal_center_page)
        self.assertTrue(window._nav_buttons["personal"].isChecked())
        self.assertEqual(window.page_title.text(), "个人中心")
        window.show_page("home")
        self.assertEqual(window.statistics_page.detail_tabs.count(), 2)
        self.assertEqual(
            [
                window.statistics_page.detail_tabs.tabText(index)
                for index in range(window.statistics_page.detail_tabs.count())
            ],
            ["图表分析", "完整数据"],
        )
        self.assertIs(
            window.statistics_page.detail_tabs.widget(0),
            window.statistics_page.chart_tab,
        )
        self.assertFalse(hasattr(window.workflow_page, "browser_combo"))
        self.assertEqual(
            window.workflow_page.browser_info.text(),
            "内置 Chromium（统一使用）",
        )
        self.assertIs(
            window.statistics_page.detail_tabs.widget(1),
            window.statistics_page.data_tab,
        )
        self.assertFalse(hasattr(window.statistics_page, "recent_tab"))
        self.assertFalse(hasattr(window.statistics_page, "recent_table"))
        self.assertEqual(window.statistics_page.data_title.text(), "完整数据")
        self.assertEqual(
            window.statistics_page.data_description.text(),
            "全部站点 · 各站总计数占比与有电话数占比",
        )
        self.assertEqual(window.statistics_page.data_count_label.text(), "0 行")
        self.assertEqual(
            [
                window.statistics_page.summary_table.horizontalHeaderItem(column).text()
                for column in range(window.statistics_page.summary_table.columnCount())
            ],
            ["站点", "总计数", "总数占比", "有电话数", "有电话数占比"],
        )
        self.assertFalse(window.statistics_page.summary_table.showGrid())
        self.assertTrue(window.statistics_page.summary_table.verticalHeader().isHidden())
        self.assertEqual(
            window.statistics_page.summary_table.verticalHeader().defaultSectionSize(),
            42,
        )
        self.assertTrue(window.workflow_page.shutdown())
        window._prepared_to_close = True
        window.close()

    def test_normal_user_permissions_and_stat_recording(self):
        self.assertEqual(self.db.ensure_default_station_users(), 5)
        user = self.db.create_account(
            "normal01", "Normal@123", "user", self.admin.id,
            display_name="测试站点",
        )
        window = MainWindow(self.db, user)
        self.assertNotIn("accounts", window._pages)
        self.assertNotIn("statistics", window._nav_buttons)
        self.assertIsNone(window.account_page)
        self.assertIs(window.stack.currentWidget(), window.workflow_page)
        self.assertTrue(window._nav_buttons["workflow"].isChecked())
        self.assertFalse(window._nav_buttons["home"].isChecked())
        self.assertEqual(window.page_title.text(), "一键业务处理")
        self.assertEqual(window.top_identity.text(), "用户 · 测试站点")
        self.assertEqual(window.sidebar_user.text(), "测试站点")
        self.assertEqual(window.personal_center_page.identity_value.text(), "用户")
        self.assertEqual(window.personal_center_page.name_value.text(), "测试站点")
        self.assertEqual(window.personal_center_page.username_value.text(), "normal01")
        self.assertEqual(
            window.statistics_page.category_combo.currentData(),
            "station_distribution",
        )
        self.assertFalse(window.statistics_page.station_combo.isEnabled())
        self.assertIsNone(window.statistics_page.station_combo.currentData())
        station_names = [
            window.statistics_page.station_combo.itemText(index)
            for index in range(window.statistics_page.station_combo.count())
        ]
        self.assertEqual(station_names[0], "全部站点")
        self.assertIn("萝岗中心站", station_names)
        self.assertIn(user.name_label, station_names)
        window.statistics_page.category_combo.setCurrentIndex(
            window.statistics_page.category_combo.findData("completion")
        )
        self.app.processEvents()
        self.assertTrue(window.statistics_page.station_combo.isEnabled())
        self.assertEqual(window.statistics_page.station_combo.currentData(), user.id)

        counts = {
            WORKFLOW_TOTAL_METRIC: 6,
            WORKFLOW_EMPTY_METRIC: 1,
            WORKFLOW_NO_TRANSPORT_METRIC: 1,
            WORKFLOW_NO_OPERATION_METRIC: 1,
            WORKFLOW_INDIVIDUAL_METRIC: 1,
            WORKFLOW_NO_PHONE_METRIC: 1,
            WORKFLOW_HAS_PHONE_METRIC: 1,
        }
        self.assertTrue(
            window._record_workflow_summary(
                counts,
                "unified_workflow",
                {"file_name": "test.xlsx"},
                "ui-workflow-1",
            )
        )
        totals = {
            row["metric_key"]: row["total"]
            for row in self.db.get_user_totals(user.id)
        }
        self.assertEqual(totals[WORKFLOW_TOTAL_METRIC], 6)
        self.assertEqual(totals[WORKFLOW_EMPTY_METRIC], 1)
        self.assertEqual(totals[WORKFLOW_NO_TRANSPORT_METRIC], 1)
        self.assertEqual(totals[WORKFLOW_NO_OPERATION_METRIC], 1)
        self.assertEqual(totals[WORKFLOW_INDIVIDUAL_METRIC], 1)
        self.assertEqual(totals[WORKFLOW_NO_PHONE_METRIC], 1)
        self.assertEqual(totals[WORKFLOW_HAS_PHONE_METRIC], 1)
        window.statistics_page.refresh()
        self.assertEqual(
            window.statistics_page.distribution_chart._values[WORKFLOW_TOTAL_METRIC],
            6,
        )
        self.assertEqual(
            [
                window.statistics_page.summary_table.item(row, 0).text()
                for row in range(window.statistics_page.summary_table.rowCount())
            ],
            [
                "总计数",
                "有电话（有公司名）",
                "无电话（有公司名）",
                "个体经营",
                "无营运信息",
                "无运输证号",
                "空",
            ],
        )

        accounts = {account.username: account for account in self.db.list_accounts()}
        luogang = accounts["luogang"]
        other_counts = dict(counts)
        other_counts[WORKFLOW_TOTAL_METRIC] = 4
        other_counts[WORKFLOW_EMPTY_METRIC] = 0
        other_counts[WORKFLOW_NO_TRANSPORT_METRIC] = 0
        self.db.record_activity_batch(
            luogang.id,
            other_counts,
            "unified_workflow",
            details={
                "violation_counts": {
                    "超限": {"total": 2, "has_phone": 1, "other": 1}
                }
            },
            task_id="normal-user-other-station",
        )
        station_index = window.statistics_page.station_combo.findData(luogang.id)
        window.statistics_page.station_combo.setCurrentIndex(station_index)
        self.app.processEvents()
        self.assertEqual(
            window.statistics_page.distribution_chart._values[WORKFLOW_TOTAL_METRIC],
            4,
        )
        window.statistics_page.category_combo.setCurrentIndex(
            window.statistics_page.category_combo.findData("violation")
        )
        self.app.processEvents()
        self.assertEqual(window.statistics_page.violation_chart._rows[0]["reason"], "超限")

        window.statistics_page.station_combo.setCurrentIndex(0)
        window.statistics_page.category_combo.setCurrentIndex(
            window.statistics_page.category_combo.findData("completion")
        )
        self.app.processEvents()
        self.assertEqual(
            window.statistics_page.distribution_chart._values[WORKFLOW_TOTAL_METRIC],
            10,
        )
        self.assertEqual(
            [
                window.statistics_page.summary_table.horizontalHeaderItem(column).text()
                for column in range(window.statistics_page.summary_table.columnCount())
            ],
            ["完成类型", "累计数量", "单位"],
        )
        self.assertEqual(window.statistics_page.summary_table.item(0, 0).text(), "总计数")
        self.assertEqual(window.statistics_page.summary_table.item(0, 1).text(), "10")
        self.assertTrue(window.workflow_page.shutdown())
        window._prepared_to_close = True
        window.close()

    def test_admin_can_filter_station_and_violation_category(self):
        self.assertEqual(self.db.ensure_default_station_users(), 5)
        accounts = {account.username: account for account in self.db.list_accounts()}
        luogang = accounts["luogang"]
        counts = {
            WORKFLOW_TOTAL_METRIC: 4,
            WORKFLOW_EMPTY_METRIC: 0,
            WORKFLOW_NO_TRANSPORT_METRIC: 0,
            WORKFLOW_NO_OPERATION_METRIC: 0,
            WORKFLOW_INDIVIDUAL_METRIC: 0,
            WORKFLOW_NO_PHONE_METRIC: 2,
            WORKFLOW_HAS_PHONE_METRIC: 2,
        }
        self.db.record_activity_batch(
            luogang.id,
            counts,
            "unified_workflow",
            details={
                "violation_counts": {
                    "超限": {"total": 3, "has_phone": 2, "other": 1}
                }
            },
            task_id="luogang-filter-test",
        )

        window = MainWindow(self.db, self.admin)
        station_names = [
            window.statistics_page.station_combo.itemText(index)
            for index in range(window.statistics_page.station_combo.count())
        ]
        self.assertEqual(
            station_names,
            ["全部站点"] + [display_name for display_name, _ in DEFAULT_STATION_USERS],
        )
        window.statistics_page.category_combo.setCurrentIndex(
            window.statistics_page.category_combo.findData("violation")
        )
        station_index = window.statistics_page.station_combo.findData(luogang.id)
        window.statistics_page.station_combo.setCurrentIndex(station_index)
        self.app.processEvents()

        violation_rows = window.statistics_page.violation_chart._rows
        self.assertEqual(violation_rows[0]["reason"], "超限")
        self.assertEqual(violation_rows[0]["has_phone"], 2)
        self.assertEqual(window.statistics_page.summary_table.rowCount(), 1)
        self.assertEqual(window.statistics_page.summary_table.item(0, 3).text(), "1")
        violation_headers = [
            window.statistics_page.summary_table.horizontalHeaderItem(column).text()
            for column in range(window.statistics_page.summary_table.columnCount())
        ]
        self.assertNotIn("账号", violation_headers)
        self.assertNotIn("状态", violation_headers)
        self.assertEqual(
            window.statistics_page.data_description.text(),
            "萝岗中心站 · 违规原因明细",
        )
        self.assertEqual(window.statistics_page.data_count_label.text(), "1 行")
        mode_button = window.statistics_page.violation_chart.mode_button
        self.assertIs(mode_button.parentWidget(), window.statistics_page.violation_chart)
        self.assertEqual(window.statistics_page.violation_chart._bar_mode, "share")
        self.assertEqual(mode_button.text(), "切换为电话拆分")
        mode_button.click()
        self.assertEqual(window.statistics_page.violation_chart._bar_mode, "split")
        self.assertEqual(mode_button.text(), "切换为原因占比")
        mode_button.click()
        self.assertEqual(window.statistics_page.violation_chart._bar_mode, "share")
        self.assertTrue(window.workflow_page.shutdown())
        window._prepared_to_close = True
        window.close()

    def test_disabled_widgets_use_forbidden_cursor(self):
        window = MainWindow(self.db, self.admin)
        self.assertFalse(window.statistics_page.station_combo.isEnabled())
        self.assertEqual(
            window.statistics_page.station_combo.cursor().shape(),
            Qt.ForbiddenCursor,
        )
        self.assertFalse(window.workflow_page.pause_btn.isEnabled())
        self.assertEqual(
            window.workflow_page.pause_btn.cursor().shape(),
            Qt.ForbiddenCursor,
        )

        window.workflow_page.pause_btn.setEnabled(True)
        self.assertNotEqual(
            window.workflow_page.pause_btn.cursor().shape(),
            Qt.ForbiddenCursor,
        )
        window.workflow_page.pause_btn.setEnabled(False)
        self.assertEqual(
            window.workflow_page.pause_btn.cursor().shape(),
            Qt.ForbiddenCursor,
        )

        custom_cursor_button = QPushButton("测试自定义指针")
        custom_cursor_button.setCursor(Qt.PointingHandCursor)
        custom_cursor_button.setEnabled(False)
        self.assertEqual(
            custom_cursor_button.cursor().shape(),
            Qt.ForbiddenCursor,
        )
        cursor_filter = self.app._intdemo_disabled_cursor_filter
        cursor_filter.sync_hover_target(custom_cursor_button)
        self.assertIsNotNone(QApplication.overrideCursor())
        self.assertEqual(
            QApplication.overrideCursor().shape(),
            Qt.ForbiddenCursor,
        )
        custom_cursor_button.setEnabled(True)
        cursor_filter.sync_hover_target(custom_cursor_button)
        self.assertIsNone(QApplication.overrideCursor())
        self.assertEqual(
            custom_cursor_button.cursor().shape(),
            Qt.PointingHandCursor,
        )
        custom_cursor_button.close()

        self.assertTrue(window.workflow_page.shutdown())
        window._prepared_to_close = True
        window.close()

    def test_station_distribution_shows_total_and_phone_shares(self):
        self.assertEqual(self.db.ensure_default_station_users(), 5)
        accounts = {account.username: account for account in self.db.list_accounts()}
        luogang = accounts["luogang"]
        taiping = accounts["taiping"]
        self.db.record_activity_batch(
            luogang.id,
            {WORKFLOW_TOTAL_METRIC: 30, WORKFLOW_HAS_PHONE_METRIC: 12},
            "unified_workflow",
            task_id="station-share-luogang",
        )
        self.db.record_activity_batch(
            taiping.id,
            {WORKFLOW_TOTAL_METRIC: 10, WORKFLOW_HAS_PHONE_METRIC: 8},
            "unified_workflow",
            task_id="station-share-taiping",
        )

        window = MainWindow(self.db, self.admin)
        page = window.statistics_page
        page.category_combo.setCurrentIndex(
            page.category_combo.findData("completion")
        )
        station_index = window.statistics_page.station_combo.findData(luogang.id)
        window.statistics_page.station_combo.setCurrentIndex(station_index)
        category_index = window.statistics_page.category_combo.findData(
            "station_distribution"
        )
        window.statistics_page.category_combo.setCurrentIndex(category_index)
        self.app.processEvents()

        self.assertIsInstance(page.station_distribution_chart, StationDistributionChart)
        self.assertFalse(page.station_combo.isEnabled())
        self.assertIsNone(page.station_combo.currentData())
        self.assertIn("全部站点", page.station_combo.toolTip())
        self.assertTrue(page.distribution_chart.isHidden())
        self.assertTrue(page.violation_chart.isHidden())
        self.assertFalse(page.station_distribution_chart.isHidden())

        rows = {
            row["username"]: row for row in page.station_distribution_chart._rows
        }
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows["luogang"]["total"], 30)
        self.assertEqual(rows["luogang"]["has_phone"], 12)
        self.assertAlmostEqual(rows["luogang"]["total_share"], 75.0)
        self.assertAlmostEqual(rows["luogang"]["phone_share"], 60.0)
        self.assertAlmostEqual(rows["taiping"]["total_share"], 25.0)
        self.assertAlmostEqual(rows["taiping"]["phone_share"], 40.0)
        page.station_distribution_chart.resize(1000, 280)
        page.station_distribution_chart.grab()
        self.app.processEvents()
        self.assertEqual(len(page.station_distribution_chart._slice_hitboxes), 4)
        self.assertEqual(
            {
                item["payload"]["series"]
                for item in page.station_distribution_chart._slice_hitboxes
            },
            {"各站总计数占比", "各站有电话数占比"},
        )
        self.assertEqual(page.summary_table.rowCount(), 5)
        self.assertEqual(
            [
                page.summary_table.horizontalHeaderItem(column).text()
                for column in range(page.summary_table.columnCount())
            ],
            ["站点", "总计数", "总数占比", "有电话数", "有电话数占比"],
        )
        luogang_row = next(
            row
            for row in range(page.summary_table.rowCount())
            if page.summary_table.item(row, 0).text() == "萝岗中心站"
        )
        self.assertEqual(page.summary_table.item(luogang_row, 1).text(), "30")
        self.assertEqual(page.summary_table.item(luogang_row, 2).text(), "75.0%")
        self.assertEqual(page.summary_table.item(luogang_row, 3).text(), "12")
        self.assertEqual(page.summary_table.item(luogang_row, 4).text(), "60.0%")
        self.assertEqual(
            page.data_description.text(),
            "全部站点 · 各站总计数占比与有电话数占比",
        )

        page.category_combo.setCurrentIndex(
            page.category_combo.findData("completion")
        )
        self.app.processEvents()
        self.assertTrue(page.station_combo.isEnabled())
        self.assertEqual(page.station_combo.currentData(), luogang.id)
        self.assertTrue(window.workflow_page.shutdown())
        window._prepared_to_close = True
        window.close()

    def test_account_page_summary_and_selected_account_state(self):
        self.assertEqual(self.db.ensure_default_station_users(), 5)
        window = MainWindow(self.db, self.admin)
        page = window.account_page

        self.assertEqual(page.summary_values["total"].text(), "6")
        self.assertEqual(page.summary_values["admin"].text(), "1")
        self.assertEqual(page.summary_values["user"].text(), "5")
        self.assertEqual(page.summary_values["active"].text(), "6")
        self.assertEqual(page.table.rowCount(), 6)
        self.assertFalse(page.table.showGrid())
        self.assertFalse(page.table.verticalHeader().isVisible())

        luogang_row = next(
            index
            for index, account in enumerate(page.accounts)
            if account.username == "luogang"
        )
        page.table.selectRow(luogang_row)
        self.app.processEvents()
        self.assertIn("萝岗中心站", page.selection_hint.text())
        self.assertTrue(page.rename_btn.isEnabled())
        self.assertTrue(page.permission_btn.isEnabled())
        self.assertTrue(page.export_btn.isEnabled())
        self.assertTrue(page.import_btn.isEnabled())
        self.assertTrue(page.reset_btn.isEnabled())
        self.assertTrue(page.toggle_btn.isEnabled())
        self.assertEqual(page.toggle_btn.text(), "停用账号")
        self.assertEqual(page.toggle_btn.objectName(), "DangerButton")

        current_row = next(
            index
            for index, account in enumerate(page.accounts)
            if account.id == self.admin.id
        )
        page.table.selectRow(current_row)
        self.app.processEvents()
        self.assertIn("当前登录账号", page.selection_hint.text())
        self.assertTrue(page.rename_btn.isEnabled())
        self.assertFalse(page.permission_btn.isEnabled())
        self.assertFalse(page.export_btn.isEnabled())
        self.assertFalse(page.import_btn.isEnabled())
        self.assertFalse(page.reset_btn.isEnabled())
        self.assertFalse(page.toggle_btn.isEnabled())
        self.assertIn("个人中心", page.reset_btn.toolTip())

        self.assertTrue(window.workflow_page.shutdown())
        window._prepared_to_close = True
        window.close()

    def test_admin_can_rename_users_and_current_identity(self):
        self.assertEqual(self.db.ensure_default_station_users(), 5)
        window = MainWindow(self.db, self.admin)
        page = window.account_page
        luogang = next(
            account for account in page.accounts if account.username == "luogang"
        )
        luogang_row = page.accounts.index(luogang)
        page.table.selectRow(luogang_row)

        with (
            patch(
                "integrated_client.ui.account_page.RenameAccountDialog"
            ) as dialog_class,
            patch(
                "integrated_client.ui.account_page.QMessageBox.information"
            ) as information,
        ):
            dialog = dialog_class.return_value
            dialog.Accepted = 1
            dialog.exec_.return_value = 1
            dialog.value.return_value = "萝岗业务站"
            page._rename_account()

            renamed_station = next(
                account
                for account in self.db.list_accounts()
                if account.id == luogang.id
            )
            self.assertEqual(renamed_station.name_label, "萝岗业务站")
            self.assertEqual(renamed_station.username, "luogang")
            station_index = window.statistics_page.station_combo.findData(luogang.id)
            self.assertEqual(
                window.statistics_page.station_combo.itemText(station_index),
                "萝岗业务站",
            )

            current_row = next(
                index
                for index, account in enumerate(page.accounts)
                if account.id == self.admin.id
            )
            page.table.selectRow(current_row)
            dialog.value.return_value = "主管理员"
            page._rename_account()

            self.assertEqual(window.account.name_label, "主管理员")
            self.assertEqual(page.current_account.name_label, "主管理员")
            self.assertEqual(window.top_identity.text(), "管理员 · 主管理员")
            self.assertEqual(window.sidebar_user.text(), "主管理员")
            self.assertEqual(window.personal_center_page.name_value.text(), "主管理员")
            self.assertIn("主管理员", window.windowTitle())
            self.assertEqual(information.call_count, 2)

        self.assertTrue(window.workflow_page.shutdown())
        window._prepared_to_close = True
        window.close()

    def test_admin_can_change_account_permissions(self):
        self.assertEqual(self.db.ensure_default_station_users(), 5)
        window = MainWindow(self.db, self.admin)
        page = window.account_page
        luogang = next(
            account for account in page.accounts if account.username == "luogang"
        )
        page.table.selectRow(page.accounts.index(luogang))

        with (
            patch(
                "integrated_client.ui.account_page.AccountPermissionDialog"
            ) as dialog_class,
            patch("integrated_client.ui.account_page.QMessageBox.information"),
        ):
            dialog = dialog_class.return_value
            dialog.Accepted = 1
            dialog.exec_.return_value = 1
            dialog.value.return_value = "admin"
            page._change_permission()

            promoted = next(
                account
                for account in self.db.list_accounts()
                if account.id == luogang.id
            )
            self.assertTrue(promoted.is_admin)
            self.assertEqual(page.summary_values["admin"].text(), "2")
            self.assertEqual(
                window.statistics_page.station_combo.findData(luogang.id),
                -1,
            )

            promoted_row = next(
                index
                for index, account in enumerate(page.accounts)
                if account.id == luogang.id
            )
            page.table.selectRow(promoted_row)
            dialog.value.return_value = "user"
            page._change_permission()

            demoted = next(
                account
                for account in self.db.list_accounts()
                if account.id == luogang.id
            )
            self.assertFalse(demoted.is_admin)
            self.assertEqual(page.summary_values["admin"].text(), "1")
            self.assertGreaterEqual(
                window.statistics_page.station_combo.findData(luogang.id),
                0,
            )

        self.assertTrue(window.workflow_page.shutdown())
        window._prepared_to_close = True
        window.close()

    def test_admin_can_export_and_import_selected_station_data(self):
        self.assertEqual(self.db.ensure_default_station_users(), 5)
        accounts = {account.username: account for account in self.db.list_accounts()}
        luogang = accounts["luogang"]
        taiping = accounts["taiping"]
        self.db.record_activity_batch(
            luogang.id,
            {
                WORKFLOW_TOTAL_METRIC: 5,
                WORKFLOW_HAS_PHONE_METRIC: 3,
            },
            "unified_workflow",
            task_id="ui-station-transfer",
        )
        window = MainWindow(self.db, self.admin)
        page = window.account_page
        luogang_row = next(
            index
            for index, account in enumerate(page.accounts)
            if account.id == luogang.id
        )
        page.table.selectRow(luogang_row)
        export_base = Path(self.temp_dir.name) / "ui-station-transfer"

        with (
            patch(
                "integrated_client.ui.account_page.QFileDialog.getSaveFileName",
                return_value=(str(export_base), "站点数据文件 (*.json)"),
            ),
            patch("integrated_client.ui.account_page.QMessageBox.information"),
        ):
            page._export_station_data()
        export_path = export_base.with_suffix(".json")
        self.assertTrue(export_path.exists())

        taiping_row = next(
            index
            for index, account in enumerate(page.accounts)
            if account.id == taiping.id
        )
        page.table.selectRow(taiping_row)
        with (
            patch(
                "integrated_client.ui.account_page.QFileDialog.getOpenFileName",
                return_value=(str(export_path), "站点数据文件 (*.json)"),
            ),
            patch(
                "integrated_client.ui.account_page.QMessageBox.question",
                return_value=QMessageBox.Yes,
            ),
            patch("integrated_client.ui.account_page.QMessageBox.information"),
        ):
            page._import_station_data()

        target_totals = {
            row["metric_key"]: row["total"]
            for row in self.db.get_user_totals(taiping.id)
        }
        self.assertEqual(target_totals[WORKFLOW_TOTAL_METRIC], 5)
        self.assertEqual(target_totals[WORKFLOW_HAS_PHONE_METRIC], 3)
        station_rows = {
            row["username"]: row
            for row in window.statistics_page.station_distribution_chart._rows
        }
        self.assertEqual(station_rows["taiping"]["total"], 5)
        self.assertEqual(station_rows["taiping"]["has_phone"], 3)

        self.assertTrue(window.workflow_page.shutdown())
        window._prepared_to_close = True
        window.close()

    def test_live_preview_model_updates_a_completed_row(self):
        model = DataFrameTableModel(pd.DataFrame([{"车辆标识": "粤A12345"}]))
        model.update_row(0, {LEGAL_COL_NAME: "张三", "查询状态": "完成"})

        legal_column = model.dataframe.columns.get_loc(LEGAL_COL_NAME)
        status_column = model.dataframe.columns.get_loc("查询状态")
        self.assertEqual(model.data(model.index(0, legal_column), Qt.DisplayRole), "张三")
        self.assertEqual(model.data(model.index(0, status_column), Qt.DisplayRole), "完成")

    def test_violation_chart_bar_hover_exposes_phone_breakdown(self):
        full_reason = "恶意大车小标出入口车型不符其它复杂描述内容"
        chart = ViolationReasonChart()
        chart.resize(900, 280)
        chart.set_rows(
            [{"reason": full_reason, "total": 5, "has_phone": 2, "other": 3}],
            "测试站点",
        )
        chart.mode_button.click()
        self.assertEqual(chart._bar_mode, "split")
        chart.show()
        pixmap = chart.grab()
        self.app.processEvents()
        bar_rect, bar_row = chart._bar_hitboxes[0]
        image = pixmap.toImage()
        bar_y = int(bar_rect.center().y())
        phone_x = int(bar_rect.left() + bar_rect.width() * 0.2)
        other_x = int(bar_rect.left() + bar_rect.width() * 0.8)
        self.assertEqual(image.pixelColor(phone_x, bar_y).name(), chart.PHONE_COLOR.name())
        self.assertEqual(image.pixelColor(other_x, bar_y).name(), chart.OTHER_COLOR.name())
        self.assertEqual(bar_rect.height(), chart.BAR_HEIGHT)
        self.assertEqual(chart._format_total_label(5), "5 条")
        self.assertNotIn("%", chart._format_total_label(5))

        slice_item = chart._slice_hitboxes[0]
        outer = slice_item["outer"]
        inner = slice_item["inner"]
        donut_local = QPoint(
            int(outer.center().x() + (outer.width() + inner.width()) / 4),
            int(outer.center().y()),
        )
        donut_event = QMouseEvent(
            QEvent.MouseMove,
            donut_local,
            chart.mapToGlobal(donut_local),
            Qt.NoButton,
            Qt.NoButton,
            Qt.NoModifier,
        )
        chart.mouseMoveEvent(donut_event)
        self.assertEqual(chart._hovered_slice, 0)
        self.assertEqual(
            chart._hover_card.title_text,
            full_reason,
        )
        self.assertEqual(
            chart._hover_card.details,
            [
                ("总计", "5 条"),
                ("占比", "100.0%"),
                ("有电话", "2 条"),
                ("其他数据", "3 条"),
            ],
        )
        self.assertTrue(chart._hover_card.isVisible())
        self.assertTrue(chart._hover_card.isWindow())
        self.assertGreaterEqual(
            chart._hover_card.height(),
            50 + chart._hover_card._detail_row_count * 22,
        )
        donut_card_height = chart._hover_card.height()
        donut_card_width = chart._hover_card.width()

        bar_local = QPoint(int(bar_rect.center().x()), int(bar_rect.center().y()))
        bar_event = QMouseEvent(
            QEvent.MouseMove,
            bar_local,
            chart.mapToGlobal(bar_local),
            Qt.NoButton,
            Qt.NoButton,
            Qt.NoModifier,
        )
        chart.mouseMoveEvent(bar_event)
        self.assertEqual(bar_row["reason"], full_reason)
        self.assertEqual(chart._hover_card.title_text, "电话数据拆分")
        self.assertEqual(
            chart._hover_card.details,
            [("有电话", "2 条"), ("其他数据", "3 条")],
        )
        self.assertEqual(chart._hover_card.height(), donut_card_height)
        self.assertEqual(chart._hover_card.width(), donut_card_width)
        self.assertIsNone(chart._hovered_slice)
        phase = chart._animation_phase
        chart._advance_animation()
        self.assertNotEqual(chart._animation_phase, phase)
        self.assertTrue(chart._animation_timer.isActive())
        chart.hide()
        self.app.processEvents()
        self.assertFalse(chart._animation_timer.isActive())
        chart.close()

    def test_completion_donut_hover_exposes_category_details(self):
        chart = WorkflowDistributionChart()
        self.assertEqual(
            [metric_key for metric_key, _, _ in chart.SEGMENTS],
            [
                WORKFLOW_HAS_PHONE_METRIC,
                WORKFLOW_NO_PHONE_METRIC,
                WORKFLOW_INDIVIDUAL_METRIC,
                WORKFLOW_NO_OPERATION_METRIC,
                WORKFLOW_NO_TRANSPORT_METRIC,
                WORKFLOW_EMPTY_METRIC,
            ],
        )
        chart.resize(900, 280)
        chart.set_values(
            {
                WORKFLOW_TOTAL_METRIC: 4,
                WORKFLOW_EMPTY_METRIC: 4,
                WORKFLOW_NO_TRANSPORT_METRIC: 0,
                WORKFLOW_NO_OPERATION_METRIC: 0,
                WORKFLOW_INDIVIDUAL_METRIC: 0,
                WORKFLOW_NO_PHONE_METRIC: 0,
                WORKFLOW_HAS_PHONE_METRIC: 0,
            },
            "测试站点",
        )
        chart.show()
        chart.grab()
        self.app.processEvents()
        self.assertEqual(len(chart._slice_hitboxes), 1)
        self.assertTrue(all(rect.height() == chart.BAR_HEIGHT for rect in chart._bar_rects))
        slice_item = chart._slice_hitboxes[0]
        outer = slice_item["outer"]
        inner = slice_item["inner"]
        donut_local = QPoint(
            int(outer.center().x() + (outer.width() + inner.width()) / 4),
            int(outer.center().y()),
        )
        donut_event = QMouseEvent(
            QEvent.MouseMove,
            donut_local,
            chart.mapToGlobal(donut_local),
            Qt.NoButton,
            Qt.NoButton,
            Qt.NoModifier,
        )
        chart.mouseMoveEvent(donut_event)
        self.assertEqual(chart._hovered_slice, 0)
        self.assertEqual(chart._hover_card.title_text, "空")
        self.assertEqual(
            chart._hover_card.details,
            [("数量", "4 条"), ("占比", "100.0%")],
        )
        self.assertTrue(chart._hover_card.isVisible())
        self.assertTrue(chart._hover_card.isWindow())
        chart.close()

    def test_violation_chart_can_switch_to_reason_share_bars(self):
        chart = ViolationReasonChart()
        chart.resize(900, 280)
        chart.set_rows(
            [
                {"reason": "原因A", "total": 2, "has_phone": 1, "other": 1},
                {"reason": "原因B", "total": 8, "has_phone": 3, "other": 5},
            ],
            "测试站点",
        )
        chart.set_bar_mode("share")
        chart.show()
        chart._animation_timer.stop()
        chart._animation_phase = 0.0
        image = chart.grab().toImage()
        self.app.processEvents()
        self.assertEqual(chart._bar_mode, "share")
        self.assertTrue(chart.mode_button.isChecked())
        self.assertEqual(chart.mode_button.text(), "切换为电话拆分")
        self.assertEqual(chart._format_bar_label(2, 10), "2 条  ·  20.0%")
        bar_rect, _ = chart._bar_hitboxes[0]
        self.assertLess(chart.mode_button.geometry().bottom(), int(bar_rect.top()))
        bar_y = int(bar_rect.center().y())
        value_x = int(bar_rect.left() + bar_rect.width() * 0.1)
        empty_x = int(bar_rect.left() + bar_rect.width() * 0.5)
        self.assertEqual(
            image.pixelColor(value_x, bar_y).name(), chart.COLORS[0].name()
        )
        self.assertEqual(image.pixelColor(empty_x, bar_y).name(), "#edf1f6")
        chart.set_bar_mode("split")
        self.assertEqual(chart._format_bar_label(2, 10), "2 条")
        self.assertFalse(chart.mode_button.isChecked())
        chart.close()

    def test_hover_card_is_not_clipped_by_a_narrow_chart(self):
        chart = ViolationReasonChart()
        chart.resize(300, 280)
        chart.set_rows(
            [{"reason": "恶意U", "total": 12, "has_phone": 3, "other": 9}],
            "测试站点",
        )
        chart.show()
        chart.grab()
        self.app.processEvents()
        slice_item = chart._slice_hitboxes[0]
        outer = slice_item["outer"]
        inner = slice_item["inner"]
        donut_local = QPoint(
            int(outer.center().x() + (outer.width() + inner.width()) / 4),
            int(outer.center().y()),
        )
        donut_event = QMouseEvent(
            QEvent.MouseMove,
            donut_local,
            chart.mapToGlobal(donut_local),
            Qt.NoButton,
            Qt.NoButton,
            Qt.NoModifier,
        )
        chart.mouseMoveEvent(donut_event)
        card = chart._hover_card
        chart_right = chart.mapToGlobal(QPoint(chart.width(), 0)).x()
        self.assertTrue(card.isWindow())
        self.assertGreater(card.geometry().right(), chart_right)
        self.assertGreaterEqual(card.height(), 50 + card._detail_row_count * 22)
        screen = QApplication.screenAt(card.geometry().center())
        self.assertIsNotNone(screen)
        self.assertTrue(screen.availableGeometry().contains(card.geometry()))
        chart.close()

    def test_workflow_saves_aiqicha_fields_to_original_workbook(self):
        file_path = Path(self.temp_dir.name) / "workflow.xlsx"
        dataframe = pd.DataFrame(
            [{"车辆标识": "粤A12345", "已协助补缴": "是", "车辆所有人/企业": "示例企业"}]
        )
        dataframe.to_excel(file_path, index=False, engine="openpyxl")

        page = WorkflowPage()
        page.file_path = str(file_path)
        page.df = dataframe.copy()
        for column in TARGET_COLUMNS:
            page.df[column] = ""
        page.df.at[0, LEGAL_COL_NAME] = "张三"
        page.df.at[0, ADDR_COL_NAME] = "测试路1号"
        page.df.at[0, PHONE_COL_NAME] = "13800000000"

        self.assertTrue(page._save_aiqicha_results())
        saved = pd.read_excel(file_path, dtype=str, engine="openpyxl").fillna("")
        self.assertEqual(saved.at[0, LEGAL_COL_NAME], "张三")
        self.assertEqual(saved.at[0, ADDR_COL_NAME], "测试路1号")
        self.assertEqual(saved.at[0, PHONE_COL_NAME], "13800000000")
        page.shutdown()

    def test_workflow_counts_only_once_with_mutually_exclusive_categories(self):
        dataframe = pd.DataFrame(
            [
                {"运输证号_纯数字": "", "车辆所有人/企业": "", PHONE_COL_NAME: "13800000000", "原因": "超限|证件异常"},
                {"运输证号_纯数字": "440100001", "车辆所有人/企业": "已补缴", PHONE_COL_NAME: "", "原因": "恶意U/J形行驶|车型异常"},
                {"运输证号_纯数字": "", "车辆所有人/企业": "无运输证号", PHONE_COL_NAME: "", "原因": "证件异常/证件异常"},
                {"运输证号_纯数字": "", "车辆所有人/企业": "示例运输公司", PHONE_COL_NAME: "020-88886666", "原因": "车型异常"},
                {"运输证号_纯数字": "440100002", "车辆所有人/企业": "无营运信息", PHONE_COL_NAME: "", "原因": "营运异常"},
                {"运输证号_纯数字": "440100003", "车辆所有人/企业": "张三个体工商户", PHONE_COL_NAME: "", "原因": "经营类型"},
                {"运输证号_纯数字": "440100004", "车辆所有人/企业": "甲运输公司", PHONE_COL_NAME: "未公示", "原因": "超限/电话异常"},
                {"运输证号_纯数字": "440100005", "车辆所有人/企业": "乙运输公司", PHONE_COL_NAME: "13800000000", "原因": "超限/证件异常"},
            ]
        )
        calls = []

        def recorder(counts, source, details, task_id):
            calls.append((counts, source, details, task_id))
            return True

        page = WorkflowPage(recorder)
        page.file_path = str(Path(self.temp_dir.name) / "classified.xlsx")
        page.df = dataframe
        page._task_id = "classification-task"
        page._record_workflow_stats()
        page._record_workflow_stats()

        self.assertEqual(len(calls), 1)
        counts = calls[0][0]
        self.assertEqual(counts[WORKFLOW_TOTAL_METRIC], 8)
        self.assertEqual(counts[WORKFLOW_EMPTY_METRIC], 2)
        self.assertEqual(counts[WORKFLOW_NO_TRANSPORT_METRIC], 2)
        self.assertEqual(counts[WORKFLOW_NO_OPERATION_METRIC], 1)
        self.assertEqual(counts[WORKFLOW_INDIVIDUAL_METRIC], 1)
        self.assertEqual(counts[WORKFLOW_NO_PHONE_METRIC], 1)
        self.assertEqual(counts[WORKFLOW_HAS_PHONE_METRIC], 1)
        self.assertEqual(
            counts[WORKFLOW_TOTAL_METRIC],
            counts[WORKFLOW_EMPTY_METRIC]
            + counts[WORKFLOW_NO_TRANSPORT_METRIC]
            + counts[WORKFLOW_NO_OPERATION_METRIC]
            + counts[WORKFLOW_INDIVIDUAL_METRIC]
            + counts[WORKFLOW_NO_PHONE_METRIC]
            + counts[WORKFLOW_HAS_PHONE_METRIC],
        )
        violations = calls[0][2]["violation_counts"]
        self.assertEqual(
            violations["超限"], {"total": 1, "has_phone": 0, "other": 1}
        )
        self.assertEqual(
            violations["证件异常"], {"total": 1, "has_phone": 0, "other": 1}
        )
        self.assertEqual(
            violations["车型异常"], {"total": 2, "has_phone": 0, "other": 2}
        )
        self.assertEqual(
            violations["恶意U/J形行驶"],
            {"total": 1, "has_phone": 0, "other": 1},
        )
        self.assertIn("证件异常/证件异常", violations)
        self.assertIn("超限/电话异常", violations)
        self.assertIn("超限/证件异常", violations)
        self.assertNotIn("恶意U", violations)
        self.assertNotIn("J形行驶", violations)
        page.shutdown()

    def test_failed_workflow_does_not_record_counts(self):
        file_path = Path(self.temp_dir.name) / "failed.xlsx"
        dataframe = pd.DataFrame(
            [{"车辆标识": "粤A12345", "已协助补缴": "是", "运输证号_纯数字": ""}]
        )
        dataframe.to_excel(file_path, index=False, engine="openpyxl")
        calls = []

        class FinishedWorker:
            def isRunning(self):
                return False

            def deleteLater(self):
                pass

        page = WorkflowPage(lambda *args: calls.append(args) or True)
        page.file_path = str(file_path)
        page.df = dataframe
        page.pipeline_running = True
        page.current_step = 3
        worker = FinishedWorker()
        page.current_worker = worker
        page._save_aiqicha_results = lambda: True
        page._aiqicha_finished(worker, False)

        self.assertEqual(calls, [])
        page.shutdown()

    def test_workflow_automatically_advances_between_three_steps(self):
        file_path = Path(self.temp_dir.name) / "sequence.xlsx"
        pd.DataFrame(
            [{"车辆标识": "粤A12345", "已协助补缴": "是", "车辆所有人/企业": "示例企业"}]
        ).to_excel(file_path, index=False, engine="openpyxl")

        class FinishedWorker:
            def isRunning(self):
                return False

            def deleteLater(self):
                pass

        page = WorkflowPage()
        page.file_path = str(file_path)
        page.pipeline_running = True
        page.current_step = 1
        page._start_backfill_worker = lambda: setattr(page, "_step_two_started", True)
        first_worker = FinishedWorker()
        page.current_worker = first_worker
        page._transport_finished(first_worker, "完成")
        self.app.processEvents()
        self.assertEqual(page.current_step, 2)
        self.assertTrue(page._step_two_started)

        page._start_aiqicha_worker = lambda: setattr(page, "_step_three_started", True)
        second_worker = FinishedWorker()
        page.current_worker = second_worker
        page._backfill_finished(second_worker, "结束")
        self.app.processEvents()
        self.assertEqual(page.current_step, 3)
        self.assertTrue(page._step_three_started)
        page.shutdown()


if __name__ == "__main__":
    unittest.main()

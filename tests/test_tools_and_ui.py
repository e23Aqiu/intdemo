import json
import math
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pandas as pd
from PyQt5.QtCore import QCoreApplication, QDate, QEvent, QPoint, QRect, QSize, Qt
from PyQt5.QtGui import QMouseEvent, QPalette
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableView,
    QToolButton,
)
from openpyxl import Workbook, load_workbook
from openpyxl.styles import PatternFill

from integrated_client.config import DEFAULT_ADMIN_PASSWORD, DEFAULT_ADMIN_USERNAME
from integrated_client.database import (
    DEFAULT_STATION_PASSWORD,
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
from integrated_client.tools.transport_tool import TargetedWorkbookWriter, Worker
from integrated_client.ui.main_window import MainWindow
from integrated_client.ui.auth_dialogs import LoginDialog, PasswordDialog
from integrated_client.ui.frameless import (
    FramelessMessageBox,
    HTBOTTOMRIGHT,
    HTCAPTION,
    HTCLIENT,
    HTTOPLEFT,
    MINMAXINFO,
    WVR_REDRAW,
)
from integrated_client.ui.statistics_page import (
    StatisticsPage,
    StationDistributionChart,
    ViolationReasonChart,
    WorkflowDistributionChart,
)
from integrated_client.ui.theme import APP_STYLESHEET, _control_asset_path
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
        for widget in list(self.app.topLevelWidgets()):
            workflow = getattr(widget, "workflow_page", None)
            if workflow is not None:
                workflow.shutdown()
            else:
                shutdown = getattr(widget, "shutdown", None)
                if callable(shutdown):
                    shutdown()
            if hasattr(widget, "_prepared_to_close"):
                widget._prepared_to_close = True
            widget.close()
            widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.app.processEvents()
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

    def test_targeted_writer_preserves_rows_below_a_blank_row_and_workbook_layout(self):
        file_path = Path(self.temp_dir.name) / "targeted-write.xlsx"
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "业务数据"
        sheet.append(["车辆标识", "已协助补缴", "查询状态", "金额"])
        sheet.append(["粤A10001(黄色)", "", "已完成", 10])
        sheet.append([None, None, None, None])
        sheet.append(
            [
                "粤A10002(黄色)",
                "保留下方数据",
                "待处理",
                "=1+2",
                "无表头列也要保留",
            ]
        )
        sheet.freeze_panes = "A2"
        sheet.column_dimensions["A"].width = 28
        sheet["A4"].fill = PatternFill("solid", fgColor="FFF2CC")
        notes = workbook.create_sheet("说明")
        notes["A1"] = "这个工作表必须保留"
        notes.merge_cells("A1:B1")
        workbook.save(file_path)
        workbook.close()

        writer = TargetedWorkbookWriter(
            file_path,
            ("运输证号_纯数字", "查询状态"),
        )
        writer.write_row(
            1,
            {
                "运输证号_纯数字": "",
                "查询状态": "中间空行停止",
            },
        )
        writer.save()
        writer.close()

        saved = load_workbook(file_path, data_only=False)
        sheet = saved["业务数据"]
        headers = {
            cell.value: cell.column
            for cell in sheet[1]
            if cell.value is not None
        }
        self.assertEqual(sheet.cell(3, headers["查询状态"]).value, "中间空行停止")
        self.assertEqual(sheet["A4"].value, "粤A10002(黄色)")
        self.assertEqual(sheet["B4"].value, "保留下方数据")
        self.assertEqual(sheet["C4"].value, "待处理")
        self.assertEqual(sheet["D4"].value, "=1+2")
        self.assertEqual(sheet["E4"].value, "无表头列也要保留")
        self.assertEqual(headers["运输证号_纯数字"], 6)
        self.assertEqual(sheet["A4"].fill.fgColor.rgb, "00FFF2CC")
        self.assertEqual(sheet.freeze_panes, "A2")
        self.assertEqual(sheet.column_dimensions["A"].width, 28)
        self.assertEqual(saved["说明"]["A1"].value, "这个工作表必须保留")
        self.assertIn("A1:B1", {str(cell_range) for cell_range in saved["说明"].merged_cells.ranges})
        saved.close()
        self.assertEqual(
            list(file_path.parent.glob(f".{file_path.stem}.*.tmp{file_path.suffix}")),
            [],
        )

    def test_transport_worker_skips_rows_with_existing_company_data(self):
        file_path = Path(self.temp_dir.name) / "existing-company.xlsx"
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["车辆标识", "已协助补缴", "车辆所有人/企业"])
        sheet.append(["冀T2892_黄色", "", "无运输证号"])
        workbook.save(file_path)
        workbook.close()

        class FakePage:
            def __init__(self):
                self.goto_calls = 0

            def goto(self, *_args, **_kwargs):
                self.goto_calls += 1
                raise AssertionError("已有企业信息时不应访问运输证查询网站")

        class FakeContext:
            def __init__(self, page):
                self.page = page

            def new_page(self):
                return self.page

        class FakeBrowser:
            def __init__(self, page):
                self.page = page

            def new_context(self, **_kwargs):
                return FakeContext(self.page)

            def close(self):
                return None

        class FakeChromium:
            def __init__(self, page):
                self.page = page

            def launch(self, **_kwargs):
                return FakeBrowser(self.page)

        class FakeRuntime:
            def __init__(self, page):
                self.chromium = FakeChromium(page)

            def stop(self):
                return None

        class FakePlaywrightManager:
            def __init__(self, runtime):
                self.runtime = runtime

            def start(self):
                return self.runtime

        fake_page = FakePage()
        runtime = FakeRuntime(fake_page)
        worker = Worker(
            str(file_path),
            True,
            False,
            2,
            True,
            2,
            False,
        )
        finished_results = []
        worker.finished.connect(finished_results.append)
        with patch(
            "integrated_client.tools.transport_tool.sync_playwright",
            return_value=FakePlaywrightManager(runtime),
        ), patch(
            "integrated_client.tools.transport_tool.get_builtin_chromium_path",
            return_value=r"C:\browser\chrome.exe",
        ):
            worker.run()

        self.assertEqual(fake_page.goto_calls, 0)
        self.assertEqual(finished_results, ["完成"])
        saved = load_workbook(file_path)
        sheet = saved.active
        headers = {
            cell.value: cell.column
            for cell in sheet[1]
            if cell.value is not None
        }
        self.assertEqual(sheet.cell(2, headers["车辆所有人/企业"]).value, "无运输证号")
        self.assertEqual(sheet.cell(2, headers["查询状态"]).value, "已有企业信息（跳过）")
        saved.close()

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
        self.assertEqual(window.sidebar.width(), 230)
        self.assertEqual(window.sidebar_brand_badge.text(), "运")
        self.assertEqual(window.sidebar_brand_badge.objectName(), "BrandBadge")
        self.assertEqual(window.sidebar_role.text(), "管理员  ·  admin")
        self.assertEqual(window.sidebar_avatar.text(), "系")
        expected_nav = {
            "home": ("数据仪表盘", "nav-dashboard.svg"),
            "workflow": ("一键业务处理", "nav-workflow.svg"),
            "accounts": ("账号管理", "nav-accounts.svg"),
            "personal": ("个人中心", "nav-user.svg"),
        }
        for key, (label, icon_name) in expected_nav.items():
            button = window._nav_buttons[key]
            self.assertEqual(button.text(), label)
            self.assertEqual(button.iconSize(), QSize(19, 19))
            self.assertFalse(button.icon().isNull())
            self.assertTrue(Path(_control_asset_path(icon_name)).is_file())
        spec_text = (
            Path(__file__).resolve().parents[1] / "integrated_client.spec"
        ).read_text(encoding="utf-8")
        for _, icon_name in expected_nav.values():
            self.assertIn(icon_name, spec_text)
        for selector in (
            "QLabel#BrandBadge",
            "QFrame#SidebarProfile",
            "QLabel#SidebarAvatar",
            "QLabel#SidebarRole",
            "border-left: 4px solid #8ce3d1",
        ):
            self.assertIn(selector, APP_STYLESHEET)
        self.assertIs(window.page_scroll_area.widget(), window.stack)
        self.assertTrue(window.page_scroll_area.widgetResizable())
        self.assertEqual(
            window.page_scroll_area.horizontalScrollBarPolicy(),
            Qt.ScrollBarAsNeeded,
        )
        self.assertEqual(
            window.page_scroll_area.verticalScrollBarPolicy(),
            Qt.ScrollBarAsNeeded,
        )
        self.assertEqual(window.stack.minimumSize(), window.PAGE_CANVAS_SIZE)
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
        self.assertFalse(hasattr(window, "top_identity"))
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
        self.assertFalse(window.statistics_page.detail_tabs.documentMode())
        self.assertIn(
            "border-top-left-radius: 0;",
            APP_STYLESHEET,
        )
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
        self.app.processEvents()
        self.assertEqual(
            window.workflow_page.mode_combo.view().window().frameShape(),
            QFrame.NoFrame,
        )
        workflow_popup = window.workflow_page.mode_combo.view().window()
        self.assertIs(
            workflow_popup.property("_intdemo_combo_owner"),
            window.workflow_page.mode_combo,
        )
        self.assertTrue(workflow_popup.windowFlags() & Qt.FramelessWindowHint)
        self.assertTrue(workflow_popup.windowFlags() & Qt.NoDropShadowWindowHint)
        self.assertTrue(workflow_popup.testAttribute(Qt.WA_TranslucentBackground))
        self.assertEqual(
            (
                workflow_popup.contentsMargins().left(),
                workflow_popup.contentsMargins().top(),
                workflow_popup.contentsMargins().right(),
                workflow_popup.contentsMargins().bottom(),
            ),
            (0, 0, 0, 0),
        )
        self.assertEqual(
            window.statistics_page.category_combo.view().window().frameShape(),
            QFrame.NoFrame,
        )
        window.show()
        self.app.processEvents()
        category_combo = window.statistics_page.category_combo
        category_combo.showPopup()
        self.app.processEvents()
        self.app.processEvents()
        category_popup = category_combo.view().window()
        combo_rect = QRect(
            category_combo.mapToGlobal(QPoint(0, 0)),
            category_combo.size(),
        )
        popup_rect = category_popup.frameGeometry()
        self.assertTrue(combo_rect.intersects(popup_rect))
        view_margins = category_combo.view().contentsMargins()
        if popup_rect.top() >= combo_rect.top():
            popup_overlap = combo_rect.bottom() - popup_rect.top() + 1
            expected_overlap = view_margins.top() + 1
        else:
            popup_overlap = popup_rect.bottom() - combo_rect.top() + 1
            expected_overlap = view_margins.bottom() + 1
        self.assertEqual(popup_overlap, expected_overlap)
        self.assertFalse(category_popup.mask().isEmpty())
        self.assertFalse(category_popup.mask().contains(QPoint(0, 0)))
        self.assertTrue(
            category_popup.mask().contains(category_popup.rect().center())
        )
        category_combo.hidePopup()
        self.assertEqual(
            [
                window.workflow_page.page_tabs.tabText(index)
                for index in range(window.workflow_page.page_tabs.count())
            ],
            ["业务处理", "运行设置"],
        )
        self.assertEqual(
            window.workflow_page.page_tabs.objectName(),
            "WorkflowTabs",
        )
        self.assertFalse(window.workflow_page.page_tabs.documentMode())
        self.assertIn("QTabWidget#WorkflowTabs::pane", APP_STYLESHEET)
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

    def test_frameless_controls_are_embedded_without_an_extra_title_bar(self):
        window = MainWindow(self.db, self.admin)
        window.show()
        self.app.processEvents()

        self.assertTrue(window.windowFlags() & Qt.FramelessWindowHint)
        top_bar = window.window_controls.parentWidget()
        self.assertEqual(top_bar.objectName(), "TopBar")
        self.assertEqual(top_bar.height(), 62)
        self.assertEqual(window.centralWidget().layout().count(), 2)
        self.assertFalse(window.window_controls.minimize_button.isHidden())
        self.assertFalse(window.window_controls.maximize_button.isHidden())
        self.assertFalse(window.window_controls.close_button.isHidden())
        self.assertEqual(window.minimumSize().width(), 800)
        self.assertEqual(window.minimumSize().height(), 600)
        self.assertFalse(window.page_scroll_area.horizontalScrollBar().isVisible())
        self.assertFalse(window.page_scroll_area.verticalScrollBar().isVisible())
        window.resize(800, 600)
        self.app.processEvents()
        self.assertTrue(window.page_scroll_area.horizontalScrollBar().isVisible())
        self.assertTrue(window.page_scroll_area.verticalScrollBar().isVisible())
        self.assertIn("QScrollBar::handle:horizontal:hover", APP_STYLESHEET)
        self.assertIn("QScrollBar::handle:vertical:pressed", APP_STYLESHEET)
        native_limits = MINMAXINFO()
        window._update_minimum_track_size(native_limits)
        scale = max(1.0, float(window.devicePixelRatioF()))
        self.assertEqual(
            native_limits.ptMinTrackSize.x,
            math.ceil(window.minimumWidth() * scale),
        )
        self.assertEqual(
            native_limits.ptMinTrackSize.y,
            math.ceil(window.minimumHeight() * scale),
        )
        self.assertEqual(window._windows_nccalcsize_result(1), WVR_REDRAW)
        self.assertEqual(window._windows_nccalcsize_result(0), 0)

        caption_point = top_bar.mapTo(window, QPoint(320, top_bar.height() // 2))
        control_point = window.window_controls.mapTo(
            window, window.window_controls.rect().center()
        )
        self.assertEqual(window._window_hit_test(caption_point), HTCAPTION)
        self.assertEqual(window._window_hit_test(control_point), HTCLIENT)
        self.assertEqual(window._window_hit_test(QPoint(0, 0)), HTTOPLEFT)
        self.assertEqual(
            window._window_hit_test(QPoint(window.width() - 1, window.height() - 1)),
            HTBOTTOMRIGHT,
        )

        window.window_controls.toggle_maximized()
        self.app.processEvents()
        self.assertTrue(window.isMaximized())
        self.assertEqual(window.window_controls.maximize_button.toolTip(), "还原")
        window.window_controls.toggle_maximized()
        self.app.processEvents()
        self.assertFalse(window.isMaximized())

        login = LoginDialog(self.db)
        login.show()
        self.app.processEvents()
        self.assertTrue(login.windowFlags() & Qt.FramelessWindowHint)
        self.assertTrue(login.window_controls.minimize_button.isHidden())
        self.assertTrue(login.window_controls.maximize_button.isHidden())
        self.assertEqual(login.minimumSize(), login.maximumSize())
        self.assertEqual(login._window_hit_test(QPoint(60, 20)), HTCAPTION)
        self.assertEqual(login.username_edit.text(), "")
        self.assertFalse(
            any(
                "首次启动已创建管理员" in label.text()
                for label in login.findChildren(QLabel)
            )
        )
        login_title = login.findChild(QLabel, "PageTitle")
        login_button = login.findChild(QPushButton, "PrimaryButton")
        self.assertLessEqual(
            abs(
                login_title.geometry().top()
                - (login.height() - login_button.geometry().bottom())
            ),
            1,
        )
        login.close()

        message_box = FramelessMessageBox(window)
        message_box.setWindowTitle("确认操作")
        message_box.setText("确认操作")
        message_box.setInformativeText("确定继续吗？")
        message_box.setIcon(QMessageBox.Information)
        message_box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        message_box.show()
        self.app.processEvents()
        self.assertTrue(message_box.windowFlags() & Qt.FramelessWindowHint)
        self.assertTrue(message_box.window_controls.minimize_button.isHidden())
        self.assertTrue(message_box.window_controls.maximize_button.isHidden())
        self.assertFalse(message_box.window_controls.close_button.isHidden())
        self.assertFalse(message_box.icon_label.pixmap().isNull())
        self.assertEqual(
            message_box.ICON_STYLES[QMessageBox.Information][1], "#1d8178"
        )
        self.assertEqual(message_box.button(QMessageBox.Yes).text(), "是")
        self.assertEqual(message_box.button(QMessageBox.No).text(), "否")
        message_box.close()

        window.workflow_page.shutdown()
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
        self.assertFalse(hasattr(window, "top_identity"))
        self.assertEqual(window.sidebar_user.text(), "测试站点")
        self.assertEqual(window.sidebar_role.text(), "用户  ·  normal01")
        self.assertEqual(window.sidebar_avatar.text(), "测")
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
            ],
        )
        self.assertTrue(
            all(
                window.statistics_page.kpi_layout.itemAtPosition(
                    index // 3,
                    index % 3,
                )
                is not None
                for index in range(6)
            )
        )
        self.assertIsNone(
            window.statistics_page.kpi_layout.itemAtPosition(0, 3)
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

    def test_only_admin_can_view_empty_anomalies_grouped_by_station(self):
        self.assertEqual(self.db.ensure_default_station_users(), 5)
        accounts = {account.username: account for account in self.db.list_accounts()}
        luogang = accounts["luogang"]
        taiping = accounts["taiping"]
        for station, total, empty, task_id in (
            (luogang, 5, 2, "admin-anomaly-luogang"),
            (taiping, 4, 1, "admin-anomaly-taiping"),
        ):
            self.db.record_activity_batch(
                station.id,
                {
                    WORKFLOW_TOTAL_METRIC: total,
                    WORKFLOW_EMPTY_METRIC: empty,
                    WORKFLOW_HAS_PHONE_METRIC: total - empty,
                },
                "unified_workflow",
                task_id=task_id,
            )

        page = StatisticsPage(self.db, self.admin)
        self.assertTrue(page.anomaly_button.isHidden())
        page.category_combo.setCurrentIndex(
            page.category_combo.findData("completion")
        )
        self.app.processEvents()

        self.assertFalse(page.anomaly_button.isHidden())
        self.assertEqual(page.anomaly_button.objectName(), "AnomalyButton")
        self.assertEqual(page.anomaly_button.text(), "异常数据（3）")
        page.anomaly_button.click()
        self.app.processEvents()

        self.assertTrue(page.anomaly_button.isChecked())
        self.assertEqual(page.anomaly_button.text(), "返回完成类型（3）")
        self.assertEqual(page.data_title.text(), "异常数据")
        self.assertIs(page.detail_tabs.currentWidget(), page.data_tab)
        self.assertFalse(page.detail_tabs.isTabEnabled(0))
        self.assertEqual(
            [
                page.summary_table.horizontalHeaderItem(column).text()
                for column in range(page.summary_table.columnCount())
            ],
            ["用户（站）", "登录账号", "异常条数", "本站总计数", "异常占比"],
        )
        anomaly_rows = {
            page.summary_table.item(row, 1).text(): [
                page.summary_table.item(row, column).text()
                for column in range(page.summary_table.columnCount())
            ]
            for row in range(page.summary_table.rowCount())
        }
        self.assertEqual(
            anomaly_rows["luogang"],
            ["萝岗中心站", "luogang", "2", "5", "40.0%"],
        )
        self.assertEqual(
            anomaly_rows["taiping"],
            ["太平中心站", "taiping", "1", "4", "25.0%"],
        )
        self.assertEqual(page.data_count_label.text(), "2 行")

        anomaly_export = Path(self.temp_dir.name) / "anomaly-dashboard.xlsx"
        export_result = page._save_dashboard_excel(anomaly_export)
        self.assertEqual(export_result["category_name"], "异常数据")
        export_workbook = load_workbook(anomaly_export)
        self.assertEqual(export_workbook.active["D2"].value, "异常数据")
        export_workbook.close()

        page.station_combo.setCurrentIndex(page.station_combo.findData(luogang.id))
        self.app.processEvents()
        self.assertEqual(page.anomaly_button.text(), "返回完成类型（2）")
        self.assertEqual(page.summary_table.rowCount(), 1)
        self.assertEqual(page.summary_table.item(0, 1).text(), "luogang")

        page.anomaly_button.click()
        self.app.processEvents()
        self.assertFalse(page.anomaly_button.isChecked())
        self.assertTrue(page.detail_tabs.isTabEnabled(0))
        self.assertIs(page.detail_tabs.currentWidget(), page.chart_tab)
        self.assertEqual(page.data_title.text(), "完整数据")
        self.assertNotIn(
            "空",
            [
                page.summary_table.item(row, 0).text()
                for row in range(page.summary_table.rowCount())
            ],
        )

        user_page = StatisticsPage(self.db, luogang)
        user_page.category_combo.setCurrentIndex(
            user_page.category_combo.findData("completion")
        )
        self.app.processEvents()
        self.assertFalse(hasattr(user_page, "anomaly_button"))

        for widget in (user_page, page):
            widget.close()
            widget.deleteLater()
        self.app.processEvents()

    def test_dashboard_date_range_filters_all_chart_categories(self):
        self.assertEqual(self.db.ensure_default_station_users(), 5)
        luogang = next(
            account for account in self.db.list_accounts()
            if account.username == "luogang"
        )
        for task_id, created_at, total, reason in (
            ("ui-date-early", "2025-01-01T09:00:00+08:00", 2, "早期异常"),
            ("ui-date-middle", "2025-01-15T09:00:00+08:00", 4, "中期异常"),
        ):
            self.db.record_activity_batch(
                luogang.id,
                {
                    WORKFLOW_TOTAL_METRIC: total,
                    WORKFLOW_HAS_PHONE_METRIC: total - 1,
                },
                "unified_workflow",
                details={
                    "violation_counts": {
                        reason: {
                            "total": total,
                            "has_phone": total - 1,
                            "other": 1,
                        }
                    }
                },
                task_id=task_id,
            )
            with self.db._connect() as conn:
                conn.execute(
                    "UPDATE activity_events SET created_at=? WHERE task_id=?",
                    (created_at, task_id),
                )

        window = MainWindow(self.db, self.admin)
        window.show()
        window.resize(800, 600)
        self.app.processEvents()
        page = window.statistics_page
        selector = page.date_range_selector
        self.assertTrue(selector.all_dates_check.isChecked())
        filter_groups = (
            page.station_filter_group,
            page.category_filter_group,
            page.date_filter_group,
        )
        self.assertEqual(
            [group.objectName() for group in filter_groups],
            ["DashboardFilterGroup"] * 3,
        )
        self.assertEqual(len({group.y() for group in filter_groups}), 1)
        self.assertLess(
            page.station_filter_group.x(),
            page.category_filter_group.x(),
        )
        self.assertLess(
            page.category_filter_group.x(),
            page.date_filter_group.x(),
        )
        self.assertEqual(page.date_filter_label.text(), "日期范围")
        self.assertEqual(
            page.date_filter_label.objectName(),
            "DashboardFilterLabel",
        )
        self.assertGreaterEqual(selector.width(), selector.minimumWidth())
        self.assertGreaterEqual(
            selector.all_dates_panel.width(),
            selector.all_dates_panel.minimumWidth(),
        )
        self.assertGreaterEqual(
            selector.all_dates_check.width(),
            selector.all_dates_check.sizeHint().width(),
        )
        for editor in (selector.start_edit, selector.end_edit):
            self.assertGreaterEqual(editor.minimumWidth(), 160)
            self.assertGreaterEqual(editor.width(), editor.minimumWidth())
            calendar = editor.calendarWidget()
            self.assertGreaterEqual(calendar.minimumWidth(), 332)
            self.assertEqual(calendar.firstDayOfWeek(), Qt.Monday)
            self.assertEqual(
                calendar.findChild(QToolButton, "qt_calendar_prevmonth").text(),
                "‹",
            )
            self.assertEqual(
                calendar.findChild(QToolButton, "qt_calendar_nextmonth").text(),
                "›",
            )
            calendar_view = calendar.findChild(
                QTableView, "qt_calendar_calendarview"
            )
            self.assertTrue(calendar_view.hasMouseTracking())
            self.assertTrue(calendar_view.viewport().hasMouseTracking())
            self.assertEqual(
                calendar_view.itemDelegate().__class__.__name__,
                "CalendarHoverDelegate",
            )

        selector.all_dates_check.setChecked(False)
        click_editor = selector.end_edit
        stable_date = QDate(2031, 7, 19)
        click_editor.setDate(stable_date)
        line_edit = click_editor.lineEdit()
        self.assertTrue(line_edit.isReadOnly())
        QTest.mouseClick(
            line_edit,
            Qt.LeftButton,
            pos=line_edit.rect().center(),
        )
        self.app.processEvents()
        self.assertTrue(click_editor.calendarWidget().isVisible())
        self.assertEqual(click_editor.date(), stable_date)
        QTest.keyClick(click_editor.calendarWidget(), Qt.Key_Escape)
        QTest.keyClicks(click_editor, "2024-01-01")
        QTest.keyClick(click_editor, Qt.Key_Up)
        self.assertEqual(click_editor.date(), stable_date)
        self.assertLess(
            selector.layout().indexOf(selector.end_edit),
            selector.layout().indexOf(selector.all_dates_panel),
        )
        self.assertLess(
            selector.layout().indexOf(selector.range_separator),
            selector.layout().indexOf(selector.all_dates_panel),
        )
        selector.start_edit.setDate(QDate(2025, 1, 15))
        selector.end_edit.setDate(QDate(2025, 1, 15))
        self.app.processEvents()

        station_rows = {
            row["username"]: row for row in page.station_distribution_chart._rows
        }
        self.assertEqual(station_rows["luogang"]["total"], 4)
        self.assertIn("2025-01-15 至 2025-01-15", page.data_description.text())

        page.category_combo.setCurrentIndex(
            page.category_combo.findData("completion")
        )
        page.station_combo.setCurrentIndex(page.station_combo.findData(luogang.id))
        self.app.processEvents()
        self.assertEqual(
            page.distribution_chart._values[WORKFLOW_TOTAL_METRIC],
            4,
        )

        page.category_combo.setCurrentIndex(page.category_combo.findData("violation"))
        self.app.processEvents()
        self.assertEqual(page.violation_chart._rows[0]["reason"], "中期异常")
        self.assertEqual(page.violation_chart._rows[0]["total"], 4)

        self.assertTrue(window.workflow_page.shutdown())
        window._prepared_to_close = True
        window.close()

    def test_dashboard_exports_current_station_and_date_range_to_excel(self):
        self.assertEqual(self.db.ensure_default_station_users(), 5)
        accounts = {account.username: account for account in self.db.list_accounts()}
        station = accounts["luogang"]
        self.db.record_activity_batch(
            station.id,
            {
                WORKFLOW_TOTAL_METRIC: 7,
                WORKFLOW_HAS_PHONE_METRIC: 4,
            },
            "unified_workflow",
            task_id="dashboard-excel-export",
        )
        with self.db._connect() as conn:
            conn.execute(
                "UPDATE activity_events SET created_at=? WHERE task_id=?",
                ("2025-03-15 10:00:00", "dashboard-excel-export"),
            )

        window = MainWindow(self.db, self.admin)
        page = window.statistics_page
        page.category_combo.setCurrentIndex(
            page.category_combo.findData("completion")
        )
        page.station_combo.setCurrentIndex(
            page.station_combo.findData(station.id)
        )
        selector = page.date_range_selector
        selector.all_dates_check.setChecked(False)
        selector.start_edit.setDate(QDate(2025, 3, 1))
        selector.end_edit.setDate(QDate(2025, 3, 31))
        self.app.processEvents()

        self.assertEqual(page.export_btn.text(), "导出 Excel")
        self.assertEqual(page.export_btn.objectName(), "PrimaryButton")
        export_base = Path(self.temp_dir.name) / "dashboard-export"
        with patch(
            "integrated_client.ui.statistics_page.QFileDialog.getSaveFileName",
            return_value=(str(export_base), "Excel 工作簿 (*.xlsx)"),
        ), patch(
            "integrated_client.ui.statistics_page.QMessageBox.information"
        ) as information:
            page._export_dashboard_excel()

        export_path = export_base.with_suffix(".xlsx")
        self.assertTrue(export_path.exists())
        workbook = load_workbook(export_path, data_only=True)
        sheet = workbook["仪表盘数据"]
        self.assertEqual(sheet["A1"].value, "数据仪表盘导出")
        self.assertEqual(sheet["B2"].value, station.name_label)
        self.assertEqual(sheet["D2"].value, "按完成类型")
        self.assertEqual(sheet["F2"].value, "2025-03-01 至 2025-03-31")
        self.assertEqual(
            [sheet.cell(5, column).value for column in range(1, 6)],
            ["站名", "时间范围", "完成类型", "累计数量", "单位"],
        )
        self.assertEqual(sheet["A6"].value, station.name_label)
        self.assertEqual(sheet["B6"].value, "2025-03-01 至 2025-03-31")
        self.assertEqual(sheet["C6"].value, "总计数")
        self.assertEqual(sheet["D6"].value, 7)
        self.assertEqual(sheet["E6"].value, "条")
        self.assertEqual(sheet.freeze_panes, "A6")
        self.assertGreaterEqual(sheet.column_dimensions["F"].width, 25)
        workbook.close()
        information.assert_called_once()

        self.assertTrue(window.workflow_page.shutdown())
        window._prepared_to_close = True
        window.close()

    def test_workflow_settings_browser_check_and_collapsible_panels(self):
        page = WorkflowPage()
        page.resize(1400, 900)
        page.show()
        self.app.processEvents()
        self.assertIs(page.page_tabs.widget(0), page.workflow_tab)
        self.assertIs(page.page_tabs.widget(1), page.settings_tab)
        self.assertEqual(page.browser_check_state, "unchecked")
        self.assertIn("尚未检测", page.browser_status_label.text())
        self.assertTrue(page.browser_detail_label.isHidden())
        self.assertEqual(page.browser_detail_toggle_btn.text(), "查看详情")
        for selector in (
            "QComboBox::down-arrow",
            "QComboBox::drop-down:on",
            "QCheckBox::indicator:checked",
            "QSpinBox::up-button",
        ):
            self.assertIn(selector, APP_STYLESHEET)
        for asset_name in (
            "check.svg",
            "minus.svg",
            "chevron-down.svg",
            "chevron-up.svg",
        ):
            self.assertTrue(Path(_control_asset_path(asset_name)).is_file())
        self.assertEqual(
            [
                page.mode_card.objectName(),
                page.captcha_card.objectName(),
                page.query_card.objectName(),
            ],
            ["SettingCard", "SettingCard", "SettingCard"],
        )
        self.assertIn("人工接管", page.mode_hint.text())
        page.mode_combo.setCurrentIndex(page.mode_combo.findData(True))
        self.assertIn("全自动运行", page.mode_hint.text())
        self.assertFalse(page.manual_captcha.isEnabled())
        page.mode_combo.setCurrentIndex(page.mode_combo.findData(False))

        with patch.object(page, "check_browser") as automatic_check:
            page.page_tabs.setCurrentWidget(page.settings_tab)
            page.page_tabs.setCurrentWidget(page.workflow_tab)
            page.page_tabs.setCurrentWidget(page.settings_tab)
        automatic_check.assert_called_once_with()

        with patch(
            "integrated_client.ui.workflow_page.check_builtin_chromium",
            return_value=(r"C:\\browser\\chrome.exe", "123.0"),
        ) as browser_check:
            page.check_browser()
            worker = page.browser_check_worker
            self.assertIsNotNone(worker)
            self.assertTrue(worker.wait(3000))
            self.app.processEvents()

        browser_check.assert_called_once_with()
        self.assertEqual(page.browser_check_state, "ready")
        self.assertIn("运行正常", page.browser_status_label.text())
        self.assertIn("Chromium 123.0", page.browser_status_label.text())
        self.assertIn("chrome.exe", page.browser_detail_label.text())
        self.assertTrue(page.browser_detail_label.isHidden())
        page.browser_detail_toggle_btn.click()
        self.assertFalse(page.browser_detail_label.isHidden())
        self.assertEqual(page.browser_detail_toggle_btn.text(), "收起详情")
        page.browser_detail_toggle_btn.click()
        self.assertTrue(page.browser_detail_label.isHidden())
        self.assertEqual(page.browser_detail_toggle_btn.text(), "查看详情")

        self.assertTrue(page.preview_panel.is_expanded())
        self.assertTrue(page.log_panel.is_expanded())
        file_group_height = page.file_group.height()
        step_card_heights = [card.height() for card in page.step_cards]
        page.preview_toggle_btn.click()
        self.assertFalse(page.preview_panel.is_expanded())
        self.assertTrue(page.table.isHidden())
        self.assertIn("展开", page.preview_toggle_btn.text())
        page.preview_toggle_btn.click()
        self.assertTrue(page.preview_panel.is_expanded())
        self.assertFalse(page.table.isHidden())

        page.preview_toggle_btn.click()
        page.log_toggle_btn.click()
        self.assertFalse(page.log_panel.is_expanded())
        self.assertTrue(page.log_text.isHidden())
        self.assertIn("展开", page.log_toggle_btn.text())
        self.app.processEvents()
        self.assertFalse(page.collapsed_content_spacer.isHidden())
        self.assertEqual(page.file_group.height(), file_group_height)
        self.assertEqual(
            [card.height() for card in page.step_cards],
            step_card_heights,
        )
        page.log_toggle_btn.click()
        self.assertTrue(page.log_panel.is_expanded())
        self.assertFalse(page.log_text.isHidden())
        self.assertTrue(page.collapsed_content_spacer.isHidden())
        self.assertTrue(page.shutdown())
        page.close()

    def test_workflow_start_browser_check_dialog_actions(self):
        self.assertFalse(
            WorkflowPage._browser_start_result_allows_pipeline(
                QMessageBox.Cancel
            )
        )
        self.assertTrue(
            WorkflowPage._browser_start_result_allows_pipeline(
                QMessageBox.Ignore
            )
        )
        self.assertTrue(
            WorkflowPage._browser_start_result_allows_pipeline(QMessageBox.Ok)
        )
        self.assertEqual(
            WorkflowPage.BROWSER_CHECK_ANIMATION_FRAMES,
            (
                "正在检测内置浏览器",
                "正在检测内置浏览器 ·",
                "正在检测内置浏览器 ··",
                "正在检测内置浏览器 ···",
            ),
        )
        self.assertIn("QProgressBar#BrowserCheckProgress", APP_STYLESHEET)

    def test_disabled_widgets_use_forbidden_cursor(self):
        previous_stylesheet = self.app.styleSheet()
        self.addCleanup(self.app.setStyleSheet, previous_stylesheet)
        self.app.setStyleSheet(APP_STYLESHEET)
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
        window.show()
        self.app.processEvents()

        disabled_button = QPushButton("禁用按钮")
        disabled_button.setEnabled(False)
        disabled_button.show()
        disabled_combo = QComboBox()
        disabled_combo.addItem("禁用内容")
        disabled_combo.setEnabled(False)
        disabled_combo.show()
        disabled_label = QLabel("禁用内容")
        disabled_label.setEnabled(False)
        disabled_label.show()
        self.app.processEvents()
        self.assertEqual(
            disabled_button.palette()
            .color(QPalette.Disabled, QPalette.Button)
            .name(),
            "#f3f5f8",
        )
        self.assertEqual(
            disabled_button.palette()
            .color(QPalette.Disabled, QPalette.ButtonText)
            .name(),
            "#aab3bf",
        )
        self.assertEqual(
            disabled_combo.palette()
            .color(QPalette.Disabled, QPalette.Base)
            .name(),
            "#f5f7fa",
        )
        self.assertEqual(
            disabled_combo.palette()
            .color(QPalette.Disabled, QPalette.Text)
            .name(),
            "#a3adba",
        )
        self.assertEqual(
            disabled_label.palette()
            .color(QPalette.Disabled, QPalette.WindowText)
            .name(),
            "#a3adba",
        )
        disabled_button.close()
        disabled_combo.close()
        disabled_label.close()

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
        chart = page.station_distribution_chart
        slice_item = chart._slice_hitboxes[0]
        outer = slice_item["outer"]
        inner = slice_item["inner"]
        slice_local = QPoint(
            int(outer.center().x() + (outer.width() + inner.width()) / 4),
            int(outer.center().y()),
        )
        slice_event = QMouseEvent(
            QEvent.MouseMove,
            slice_local,
            chart.mapToGlobal(slice_local),
            Qt.NoButton,
            Qt.NoButton,
            Qt.NoModifier,
        )
        chart.mouseMoveEvent(slice_event)
        slice_payload = slice_item["payload"]
        self.assertEqual(
            chart._hover_card.details,
            [
                ("数量", f'{slice_payload["value"]} 条'),
                ("占比", chart._format_share(slice_payload["share"])),
            ],
        )
        self.assertEqual(chart._hover_card.width(), 240)

        legend_rect, legend_row = chart._legend_hitboxes[0]
        legend_local = legend_rect.center().toPoint()
        legend_event = QMouseEvent(
            QEvent.MouseMove,
            legend_local,
            chart.mapToGlobal(legend_local),
            Qt.NoButton,
            Qt.NoButton,
            Qt.NoModifier,
        )
        chart.mouseMoveEvent(legend_event)
        self.app.processEvents()
        self.assertEqual(
            chart._hover_card.details,
            [
                ("总计数", f'{legend_row["total"]} 条'),
                ("总数占比", chart._format_share(legend_row["total_share"])),
                ("有电话数", f'{legend_row["has_phone"]} 条'),
                ("有电话占比", chart._format_share(legend_row["phone_share"])),
            ],
        )
        self.assertEqual(chart._hover_card.width(), 288)
        for index in range(chart._hover_card._details_layout.count()):
            detail_label = chart._hover_card._details_layout.itemAt(index).widget()
            self.assertGreaterEqual(detail_label.width(), detail_label.sizeHint().width())
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
        self.assertTrue(page.reset_stats_btn.isEnabled())
        self.assertTrue(page.data_range_selector.isEnabled())
        self.assertTrue(page.reset_btn.isEnabled())
        self.assertTrue(page.toggle_btn.isEnabled())
        self.assertTrue(page.delete_btn.isEnabled())
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
        self.assertFalse(page.reset_stats_btn.isEnabled())
        self.assertFalse(page.data_range_selector.isEnabled())
        self.assertFalse(page.reset_btn.isEnabled())
        self.assertFalse(page.toggle_btn.isEnabled())
        self.assertFalse(page.delete_btn.isEnabled())
        self.assertIn("个人中心", page.reset_btn.toolTip())

        self.assertTrue(window.workflow_page.shutdown())
        window._prepared_to_close = True
        window.close()

    def test_admin_can_delete_an_account_and_refresh_dashboard(self):
        self.assertEqual(self.db.ensure_default_station_users(), 5)
        window = MainWindow(self.db, self.admin)
        page = window.account_page
        target = next(
            account for account in page.accounts if account.username == "luogang"
        )
        self.db.record_activity(target.id, WORKFLOW_TOTAL_METRIC, 3, "manual")
        page.table.selectRow(page.accounts.index(target))

        with (
            patch(
                "integrated_client.ui.account_page.QMessageBox.question",
                return_value=QMessageBox.Yes,
            ) as question,
            patch(
                "integrated_client.ui.account_page.QMessageBox.information"
            ) as information,
        ):
            page._delete_account()

        confirmation = question.call_args.args[2]
        self.assertIn(target.name_label, confirmation)
        self.assertIn(target.username, confirmation)
        self.assertIn("无法恢复", confirmation)
        self.assertIn("清除 1 条统计事件", information.call_args.args[2])
        self.assertNotIn(target.id, [account.id for account in page.accounts])
        self.assertEqual(page.summary_values["total"].text(), "5")
        self.assertEqual(
            window.statistics_page.station_combo.findData(target.id),
            -1,
        )
        self.assertEqual(self.db.ensure_default_station_users(), 0)

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
            self.assertEqual(window.sidebar_user.text(), "主管理员")
            self.assertEqual(window.sidebar_user.toolTip(), "主管理员")
            self.assertEqual(window.sidebar_avatar.text(), "主")
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
        today = QDate.currentDate()
        page.data_range_selector.all_dates_check.setChecked(False)
        page.data_range_selector.start_edit.setDate(today)
        page.data_range_selector.end_edit.setDate(today)
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
        exported_payload = json.loads(export_path.read_text(encoding="utf-8"))
        self.assertEqual(
            exported_payload["date_range"],
            {
                "start": today.toString("yyyy-MM-dd"),
                "end": today.toString("yyyy-MM-dd"),
            },
        )

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

        self.assertTrue(page.reset_stats_btn.isEnabled())
        with (
            patch(
                "integrated_client.ui.account_page.QMessageBox.question",
                return_value=QMessageBox.Yes,
            ) as question,
            patch(
                "integrated_client.ui.account_page.QMessageBox.information"
            ) as information,
        ):
            page._reset_station_statistics()

        confirmation_text = question.call_args.args[2]
        self.assertIn(taiping.name_label, confirmation_text)
        self.assertIn(taiping.username, confirmation_text)
        self.assertIn("永久删除", confirmation_text)
        self.assertIn("已清除 2 条统计事件", information.call_args.args[2])
        reset_totals = {
            row["metric_key"]: row["total"]
            for row in self.db.get_user_totals(taiping.id)
        }
        self.assertEqual(reset_totals[WORKFLOW_TOTAL_METRIC], 0)
        self.assertEqual(reset_totals[WORKFLOW_HAS_PHONE_METRIC], 0)
        self.assertEqual(
            self.db.authenticate("taiping", DEFAULT_STATION_PASSWORD).id,
            taiping.id,
        )
        station_rows = {
            row["username"]: row
            for row in window.statistics_page.station_distribution_chart._rows
        }
        self.assertEqual(station_rows["taiping"]["total"], 0)

        admin_row = next(
            index
            for index, account in enumerate(page.accounts)
            if account.id == self.admin.id
        )
        page.table.selectRow(admin_row)
        self.assertFalse(page.reset_stats_btn.isEnabled())

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
        self.assertTrue(
            chart._hover_card.windowFlags() & Qt.NoDropShadowWindowHint
        )
        self.assertTrue(
            chart._hover_card.testAttribute(Qt.WA_TranslucentBackground)
        )
        self.assertTrue(chart._hover_card.testAttribute(Qt.WA_StyledBackground))
        hover_card_image = chart._hover_card.grab().toImage()
        self.assertEqual(hover_card_image.pixelColor(0, 0).alpha(), 0)
        self.assertGreater(
            hover_card_image.pixelColor(hover_card_image.rect().center()).alpha(),
            0,
        )
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

    def test_completion_donut_and_bar_hover_expose_category_details(self):
        chart = WorkflowDistributionChart()
        self.assertEqual(
            [metric_key for metric_key, _, _ in chart.SEGMENTS],
            [
                WORKFLOW_HAS_PHONE_METRIC,
                WORKFLOW_NO_PHONE_METRIC,
                WORKFLOW_INDIVIDUAL_METRIC,
                WORKFLOW_NO_OPERATION_METRIC,
                WORKFLOW_NO_TRANSPORT_METRIC,
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
                WORKFLOW_HAS_PHONE_METRIC: 4,
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
        self.assertEqual(chart._hover_card.title_text, "有公司名、有电话")
        self.assertEqual(
            chart._hover_card.details,
            [("数量", "4 条"), ("占比", "100.0%")],
        )
        self.assertTrue(chart._hover_card.isVisible())
        self.assertTrue(chart._hover_card.isWindow())

        has_phone_bar = chart._bar_rects[0]
        bar_local = QPoint(
            int(has_phone_bar.center().x()),
            int(has_phone_bar.center().y()),
        )
        bar_event = QMouseEvent(
            QEvent.MouseMove,
            bar_local,
            chart.mapToGlobal(bar_local),
            Qt.NoButton,
            Qt.NoButton,
            Qt.NoModifier,
        )
        chart.mouseMoveEvent(bar_event)
        self.assertIsNone(chart._hovered_slice)
        self.assertEqual(chart._hover_card.title_text, "有公司名、有电话")
        self.assertEqual(
            chart._hover_card.details,
            [("数量", "4 条"), ("占比", "100.0%")],
        )
        self.assertTrue(chart._hover_card.isVisible())
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
        self.assertEqual(len(chart._bar_hitboxes), 2)
        self.assertEqual(
            int(
                chart._bar_hitboxes[1][0].top()
                - chart._bar_hitboxes[0][0].top()
            ),
            chart.BAR_ROW_HEIGHT,
        )
        bar_rect, _ = chart._bar_hitboxes[0]
        self.assertLess(chart.mode_button.geometry().bottom(), int(bar_rect.top()))
        bar_y = int(bar_rect.center().y())
        value_x = int(bar_rect.left() + bar_rect.width() * 0.1)
        empty_x = int(bar_rect.left() + bar_rect.width() * 0.5)
        self.assertEqual(
            image.pixelColor(value_x, bar_y).name(), chart.COLORS[0].name()
        )
        self.assertEqual(image.pixelColor(empty_x, bar_y).name(), "#e8f1ef")
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

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pandas as pd
from PyQt5.QtCore import QEvent, QPoint, Qt
from PyQt5.QtGui import QMouseEvent
from PyQt5.QtWidgets import QApplication
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
from integrated_client.ui.statistics_page import (
    ViolationReasonChart,
    WorkflowDistributionChart,
)
from integrated_client.ui.workflow_page import DataFrameTableModel, WorkflowPage


class ToolAndUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

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
        self.assertNotIn("transport", window._pages)
        self.assertNotIn("aiqicha", window._pages)
        self.assertNotIn("statistics", window._pages)
        self.assertIn("accounts", window._pages)
        self.assertNotIn("statistics", window._nav_buttons)
        self.assertIn("workflow", window._nav_buttons)
        self.assertEqual(window.statistics_page.detail_tabs.count(), 3)
        self.assertEqual(
            [
                window.statistics_page.detail_tabs.tabText(index)
                for index in range(window.statistics_page.detail_tabs.count())
            ],
            ["图表分析", "完整数据", "最近统计记录"],
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
        self.assertIs(
            window.statistics_page.detail_tabs.widget(2),
            window.statistics_page.recent_tab,
        )
        self.assertTrue(window.workflow_page.shutdown())
        window._prepared_to_close = True
        window.close()

    def test_normal_user_permissions_and_stat_recording(self):
        user = self.db.create_account("normal01", "Normal@123", "user", self.admin.id)
        window = MainWindow(self.db, user)
        self.assertNotIn("accounts", window._pages)
        self.assertNotIn("statistics", window._nav_buttons)
        self.assertIsNone(window.account_page)

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
        station_index = window.statistics_page.station_combo.findData(luogang.id)
        window.statistics_page.station_combo.setCurrentIndex(station_index)
        window.statistics_page.category_combo.setCurrentIndex(1)
        self.app.processEvents()

        violation_rows = window.statistics_page.violation_chart._rows
        self.assertEqual(violation_rows[0]["reason"], "超限")
        self.assertEqual(violation_rows[0]["has_phone"], 2)
        self.assertEqual(window.statistics_page.summary_table.rowCount(), 1)
        self.assertEqual(window.statistics_page.summary_table.item(0, 3).text(), "1")
        mode_button = window.statistics_page.violation_chart.mode_button
        self.assertIs(mode_button.parentWidget(), window.statistics_page.violation_chart)
        self.assertEqual(mode_button.text(), "切换为原因占比")
        mode_button.click()
        self.assertEqual(window.statistics_page.violation_chart._bar_mode, "share")
        self.assertEqual(mode_button.text(), "切换为电话拆分")
        mode_button.click()
        self.assertEqual(window.statistics_page.violation_chart._bar_mode, "split")
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
                {"运输证号_纯数字": "440100001", "车辆所有人/企业": "已补缴", PHONE_COL_NAME: "", "原因": "超限｜车型异常"},
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
            violations["超限"], {"total": 4, "has_phone": 1, "other": 3}
        )
        self.assertEqual(
            violations["证件异常"], {"total": 3, "has_phone": 1, "other": 2}
        )
        self.assertEqual(
            violations["车型异常"], {"total": 2, "has_phone": 0, "other": 2}
        )
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

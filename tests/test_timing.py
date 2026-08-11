import os
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from integrated_client.config import DEFAULT_ADMIN_PASSWORD, DEFAULT_ADMIN_USERNAME
from integrated_client.database import Database, WORKFLOW_TOTAL_METRIC
from integrated_client.timing import WorkflowTimingService


class WorkflowTimingTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp_dir.name) / "test.db")
        self.db.ensure_default_admin()
        self.account = self.db.authenticate(
            DEFAULT_ADMIN_USERNAME,
            DEFAULT_ADMIN_PASSWORD,
        )
        self.now = 1000.0
        self.clock = lambda: self.now
        self.source_path = Path(self.temp_dir.name) / "业务数据.xlsx"
        self.dataframe = pd.DataFrame(
            {
                "车辆标识": ["粤A12345_黄色", "粤B67890_黄色"],
                "已协助补缴": ["", ""],
                "原因": ["", "其它原因"],
            }
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_stopped_run_resumes_same_batch_and_accumulates_active_time(self):
        first = WorkflowTimingService(self.db, self.account.id, clock=self.clock)
        first_result = first.start_run(
            self.source_path,
            self.dataframe,
            run_id="run-1",
        )
        self.assertFalse(first_result["resumed"])
        first.start_step(1)

        self.now += 20 * 60
        first.request_stop()
        first.finish_step("stopped", "用户手动停止")
        first_snapshot = first.finish_run("stopped", "用户手动停止")
        self.assertEqual(first_snapshot["run_active_ms"], 20 * 60 * 1000)

        resumed_dataframe = self.dataframe.copy()
        resumed_dataframe["运输证号_纯数字"] = ["123", ""]
        resumed_dataframe["查询状态"] = ["查询成功", ""]
        second = WorkflowTimingService(self.db, self.account.id, clock=self.clock)
        second_result = second.start_run(
            self.source_path,
            resumed_dataframe,
            run_id="run-2",
        )
        self.assertTrue(second_result["resumed"])
        self.assertEqual(second_result["batch_id"], first_result["batch_id"])
        self.assertEqual(second_result["previous_active_ms"], 20 * 60 * 1000)

        second.start_step(1)
        self.now += 10 * 60
        second.finish_step("succeeded")
        final_snapshot = second.finish_run("succeeded")
        self.assertEqual(final_snapshot["run_active_ms"], 10 * 60 * 1000)
        self.assertEqual(final_snapshot["batch_active_ms"], 30 * 60 * 1000)

        summary = self.db.get_workflow_batch_summary(first_result["batch_id"])
        self.assertEqual(summary["status"], "succeeded")
        self.assertEqual(summary["run_count"], 2)
        self.assertEqual(summary["stopped_count"], 1)
        self.assertEqual(summary["active_ms"], 30 * 60 * 1000)

    def test_empty_unnamed_columns_do_not_split_or_reset_a_legacy_batch(self):
        with_placeholders = self.dataframe.copy()
        with_placeholders["Unnamed: 3"] = ""
        with_placeholders["Unnamed: 4"] = ""
        self.assertEqual(
            WorkflowTimingService.build_source_signature(self.dataframe),
            WorkflowTimingService.build_source_signature(with_placeholders),
        )
        candidates = WorkflowTimingService.build_source_signature_candidates(
            with_placeholders
        )
        self.assertEqual(len(candidates), 2)

        first = WorkflowTimingService(self.db, self.account.id, clock=self.clock)
        first_result = first.start_run(
            self.source_path,
            self.dataframe,
            run_id="placeholder-original",
        )
        self.now += 10
        first.finish_run("stopped")

        legacy_signature = candidates[1]
        legacy = self.db.start_workflow_run(
            self.account.id,
            str(self.source_path.resolve()),
            legacy_signature,
            "placeholder-legacy",
            "legacy-session",
            os.getpid(),
        )
        self.assertNotEqual(legacy["batch_id"], first_result["batch_id"])
        self.db.finish_workflow_run(
            "placeholder-legacy",
            "stopped",
            20_000,
            0,
        )

        resumed = WorkflowTimingService(self.db, self.account.id, clock=self.clock)
        resumed_result = resumed.start_run(
            self.source_path,
            with_placeholders,
            run_id="placeholder-merged",
        )
        self.assertTrue(resumed_result["resumed"])
        self.assertEqual(resumed_result["previous_active_ms"], 30_000)
        with self.db._connect() as conn:
            batches = conn.execute(
                "SELECT batch_id FROM workflow_batches WHERE user_id=?",
                (self.account.id,),
            ).fetchall()
        self.assertEqual(len(batches), 1)
        resumed.finish_run("stopped")

        with_data = with_placeholders.copy()
        with_data.loc[0, "Unnamed: 3"] = "真实原始数据"
        self.assertNotEqual(
            WorkflowTimingService.build_source_signature(self.dataframe),
            WorkflowTimingService.build_source_signature(with_data),
        )

    def test_pause_time_is_separate_and_retry_is_recorded(self):
        service = WorkflowTimingService(self.db, self.account.id, clock=self.clock)
        result = service.start_run(
            self.source_path,
            self.dataframe,
            run_id="pause-run",
        )
        step = service.start_step(1)
        self.now += 10
        service.pause("等待人工验证码")
        self.now += 30
        service.record_retry("transport_captcha", "验证码错误")
        service.resume("验证码已完成")
        self.now += 5
        service.finish_step("succeeded")
        snapshot = service.finish_run("succeeded")

        self.assertEqual(snapshot["run_active_ms"], 15_000)
        self.assertEqual(snapshot["run_paused_ms"], 30_000)
        self.assertEqual(snapshot["run_total_ms"], 45_000)
        self.assertEqual(snapshot["batch_total_ms"], 45_000)
        with self.db._connect() as conn:
            attempt = conn.execute(
                "SELECT * FROM workflow_step_attempts WHERE attempt_id=?",
                (step["attempt_id"],),
            ).fetchone()
            events = {
                row["event_type"]
                for row in conn.execute(
                    "SELECT event_type FROM workflow_timer_events WHERE batch_id=?",
                    (result["batch_id"],),
                ).fetchall()
            }
        self.assertEqual(attempt["retry_count"], 1)
        self.assertEqual(attempt["active_ms"], 15_000)
        self.assertEqual(attempt["paused_ms"], 30_000)
        self.assertTrue({"paused", "resumed", "retry"}.issubset(events))

    def test_stale_running_task_is_marked_interrupted_and_can_resume(self):
        service = WorkflowTimingService(self.db, self.account.id, clock=self.clock)
        result = service.start_run(
            self.source_path,
            self.dataframe,
            run_id="crashed-run",
        )
        service.start_step(1)
        with self.db._connect() as conn:
            conn.execute(
                """
                UPDATE workflow_runs
                SET heartbeat_ts=0, heartbeat_at='2026-01-01T00:00:00+00:00',
                    active_ms=12345, process_id=99999999
                WHERE run_id='crashed-run'
                """
            )

        recovered = self.db.recover_stale_workflow_runs(
            stale_after_seconds=15,
            now_ts=100,
        )
        self.assertEqual(recovered, 1)
        self.assertEqual(
            self.db.get_workflow_run("crashed-run")["status"],
            "interrupted",
        )

        resumed = WorkflowTimingService(self.db, self.account.id, clock=self.clock)
        resumed_result = resumed.start_run(
            self.source_path,
            self.dataframe,
            run_id="after-crash-run",
        )
        self.assertTrue(resumed_result["resumed"])
        self.assertEqual(resumed_result["batch_id"], result["batch_id"])
        self.assertEqual(resumed_result["previous_active_ms"], 12_345)
        resumed.finish_run("stopped", "测试结束")

    def test_process_liveness_check_distinguishes_current_and_missing_process(self):
        self.assertTrue(self.db._process_is_running(os.getpid()))
        self.assertFalse(self.db._process_is_running(99999999))

    def test_active_and_paused_segments_are_rounded_to_milliseconds(self):
        service = WorkflowTimingService(self.db, self.account.id, clock=self.clock)
        service.start_run(
            self.source_path,
            self.dataframe,
            run_id="millisecond-run",
        )
        service.start_step(1)
        self.now += 1.234
        service.pause("毫秒暂停")
        self.now += 0.567
        service.resume("毫秒恢复")
        self.now += 2.345
        snapshot = service.finish_run("succeeded")

        self.assertEqual(snapshot["run_active_ms"], 3_579)
        self.assertEqual(snapshot["run_paused_ms"], 567)
        self.assertEqual(snapshot["run_total_ms"], 4_146)
        self.assertEqual(snapshot["batch_total_ms"], 4_146)
        stored = self.db.get_workflow_run("millisecond-run")
        self.assertEqual(stored["active_ms"], 3_579)
        self.assertEqual(stored["paused_ms"], 567)
        self.assertEqual(
            WorkflowTimingService.format_duration(3_579),
            "00:00:03.579",
        )

    def test_dashboard_timing_totals_only_settle_successful_batches(self):
        first = WorkflowTimingService(self.db, self.account.id, clock=self.clock)
        first.start_run(self.source_path, self.dataframe, run_id="summary-stopped")
        first.start_step(1)
        self.now += 60
        first.pause("等待人工处理")
        self.now += 30
        first.resume("继续执行")
        self.now += 30
        first.finish_run("stopped", "用户手动停止")

        incomplete = self.db.get_workflow_timing_totals(user_id=self.account.id)
        self.assertEqual(incomplete["active_ms"], 0)
        self.assertEqual(incomplete["paused_ms"], 0)
        self.assertEqual(incomplete["run_count"], 0)
        self.assertEqual(incomplete["completed_items"], 0)

        second = WorkflowTimingService(self.db, self.account.id, clock=self.clock)
        second.start_run(self.source_path, self.dataframe, run_id="summary-succeeded")
        second.start_step(1)
        self.now += 90
        second.finish_run("succeeded")
        self.db.record_activity_batch(
            self.account.id,
            {WORKFLOW_TOTAL_METRIC: 10},
            source="unified_workflow",
            task_id="summary-succeeded",
        )

        changed_input = self.dataframe.copy()
        changed_input.loc[0, "原因"] = "换错表格"
        third = WorkflowTimingService(self.db, self.account.id, clock=self.clock)
        third.start_run(self.source_path, changed_input, run_id="summary-abandoned")
        third.start_step(1)
        self.now += 30
        third.finish_run("stopped", "上传了错误表格")

        with self.db._connect() as conn:
            conn.execute(
                "UPDATE workflow_runs SET started_at=? WHERE run_id IN (?, ?)",
                (
                    "2025-05-10T09:00:00+08:00",
                    "summary-stopped",
                    "summary-succeeded",
                ),
            )
            conn.execute(
                "UPDATE workflow_runs SET started_at=? WHERE run_id=?",
                ("2025-06-10T09:00:00+08:00", "summary-abandoned"),
            )
            conn.execute(
                "UPDATE activity_events SET created_at=? WHERE task_id=?",
                ("2025-05-10T09:03:30+08:00", "summary-succeeded"),
            )

        totals = self.db.get_workflow_timing_totals(user_id=self.account.id)
        self.assertEqual(totals["active_ms"], 180_000)
        self.assertEqual(totals["paused_ms"], 30_000)
        self.assertEqual(totals["total_ms"], 210_000)
        self.assertEqual(totals["run_count"], 2)
        self.assertEqual(totals["completed_items"], 10)

        may = self.db.get_workflow_timing_totals(
            user_id=self.account.id,
            start_date="2025-05-01",
            end_date="2025-05-31",
        )
        self.assertEqual(may["active_ms"], 180_000)
        self.assertEqual(may["paused_ms"], 30_000)
        self.assertEqual(may["completed_items"], 10)

        june = self.db.get_workflow_timing_totals(
            user_id=self.account.id,
            start_date="2025-06-01",
            end_date="2025-06-30",
        )
        self.assertEqual(june["active_ms"], 0)
        self.assertEqual(june["paused_ms"], 0)
        self.assertEqual(june["run_count"], 0)
        self.assertEqual(june["completed_items"], 0)


if __name__ == "__main__":
    unittest.main()

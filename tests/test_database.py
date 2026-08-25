import json
import tempfile
import unittest
from pathlib import Path

from integrated_client.config import DEFAULT_ADMIN_PASSWORD, DEFAULT_ADMIN_USERNAME
from integrated_client.database import (
    AuthenticationError,
    DEFAULT_STATION_PASSWORD,
    DEFAULT_STATION_USERS,
    Database,
    DatabaseError,
    WORKFLOW_EMPTY_METRIC,
    WORKFLOW_HAS_PHONE_METRIC,
    WORKFLOW_INDIVIDUAL_METRIC,
    WORKFLOW_NO_OPERATION_METRIC,
    WORKFLOW_NO_PHONE_METRIC,
    WORKFLOW_NO_TRANSPORT_METRIC,
    WORKFLOW_TOTAL_METRIC,
)
from integrated_client.security import validate_password


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp_dir.name) / "test.db")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_default_admin_and_forced_password_change(self):
        self.assertTrue(self.db.ensure_default_admin())
        self.assertFalse(self.db.ensure_default_admin())

        admin = self.db.authenticate(DEFAULT_ADMIN_USERNAME, DEFAULT_ADMIN_PASSWORD)
        self.assertTrue(admin.is_admin)
        self.assertTrue(admin.must_change_password)

        self.db.change_password(admin.id, "NewAdmin@123", must_change=False)
        with self.assertRaises(AuthenticationError):
            self.db.authenticate(DEFAULT_ADMIN_USERNAME, DEFAULT_ADMIN_PASSWORD)
        changed = self.db.authenticate(DEFAULT_ADMIN_USERNAME, "NewAdmin@123")
        self.assertFalse(changed.must_change_password)

    def test_user_must_provide_current_password_to_change_own_password(self):
        self.db.ensure_default_admin()
        admin = self.db.authenticate(DEFAULT_ADMIN_USERNAME, DEFAULT_ADMIN_PASSWORD)

        with self.assertRaisesRegex(AuthenticationError, "原密码不正确"):
            self.db.change_own_password(admin.id, "WrongPassword", "NewAdmin@123")
        self.assertEqual(
            self.db.authenticate(DEFAULT_ADMIN_USERNAME, DEFAULT_ADMIN_PASSWORD).id,
            admin.id,
        )

        self.db.change_own_password(
            admin.id,
            DEFAULT_ADMIN_PASSWORD,
            "NewAdmin@123",
        )
        with self.assertRaises(AuthenticationError):
            self.db.authenticate(DEFAULT_ADMIN_USERNAME, DEFAULT_ADMIN_PASSWORD)
        changed = self.db.authenticate(DEFAULT_ADMIN_USERNAME, "NewAdmin@123")
        self.assertFalse(changed.must_change_password)

    def test_account_lifecycle_and_last_admin_protection(self):
        self.db.ensure_default_admin()
        admin = self.db.authenticate(DEFAULT_ADMIN_USERNAME, DEFAULT_ADMIN_PASSWORD)
        user = self.db.create_account("worker01", "Worker@123", "user", admin.id)
        self.assertEqual(user.role, "user")
        self.assertTrue(user.must_change_password)

        with self.assertRaises(DatabaseError):
            self.db.create_account("worker01", "Worker@123", "user", admin.id)

        self.db.set_account_active(user.id, False)
        with self.assertRaises(AuthenticationError):
            self.db.authenticate("worker01", "Worker@123")

        with self.assertRaises(DatabaseError):
            self.db.set_account_active(admin.id, False)

    def test_admin_can_update_account_display_name(self):
        self.db.ensure_default_admin()
        admin = self.db.authenticate(DEFAULT_ADMIN_USERNAME, DEFAULT_ADMIN_PASSWORD)
        user = self.db.create_account(
            "worker_name",
            "Worker@123",
            "user",
            admin.id,
            display_name="原用户名称",
        )

        renamed = self.db.update_account_display_name(user.id, "  新用户名称  ")
        self.assertEqual(renamed.id, user.id)
        self.assertEqual(renamed.username, "worker_name")
        self.assertEqual(renamed.name_label, "新用户名称")
        self.assertEqual(
            self.db.authenticate("worker_name", "Worker@123").name_label,
            "新用户名称",
        )
        with self.assertRaises(ValueError):
            self.db.update_account_display_name(user.id, "   ")
        with self.assertRaises(ValueError):
            self.db.update_account_display_name(user.id, "名称" * 33)
        with self.assertRaises(DatabaseError):
            self.db.update_account_display_name(999999, "不存在")

    def test_admin_can_update_account_permissions(self):
        self.db.ensure_default_admin()
        admin = self.db.authenticate(DEFAULT_ADMIN_USERNAME, DEFAULT_ADMIN_PASSWORD)
        user = self.db.create_account(
            "permission_user",
            "Worker@123",
            "user",
            admin.id,
            display_name="权限测试站",
        )

        with self.assertRaisesRegex(DatabaseError, "最后一个可用管理员"):
            self.db.update_account_role(admin.id, "user")
        promoted = self.db.update_account_role(user.id, "admin")
        self.assertTrue(promoted.is_admin)
        demoted = self.db.update_account_role(admin.id, "user")
        self.assertFalse(demoted.is_admin)
        with self.assertRaises(ValueError):
            self.db.update_account_role(user.id, "owner")
        with self.assertRaises(DatabaseError):
            self.db.update_account_role(999999, "user")

    def test_station_data_export_import_and_duplicate_protection(self):
        self.db.ensure_default_admin()
        admin = self.db.authenticate(DEFAULT_ADMIN_USERNAME, DEFAULT_ADMIN_PASSWORD)
        source = self.db.create_account(
            "source_station",
            "Worker@123",
            "user",
            admin.id,
            display_name="来源站",
        )
        target = self.db.create_account(
            "target_station",
            "Worker@123",
            "user",
            admin.id,
            display_name="目标站",
        )
        counts = {
            WORKFLOW_TOTAL_METRIC: 8,
            WORKFLOW_EMPTY_METRIC: 1,
            WORKFLOW_NO_TRANSPORT_METRIC: 1,
            WORKFLOW_NO_OPERATION_METRIC: 1,
            WORKFLOW_INDIVIDUAL_METRIC: 1,
            WORKFLOW_NO_PHONE_METRIC: 2,
            WORKFLOW_HAS_PHONE_METRIC: 2,
        }
        self.db.record_activity_batch(
            source.id,
            counts,
            "unified_workflow",
            details={
                "violation_counts": {
                    "证件异常": {"total": 3, "has_phone": 2, "other": 1}
                }
            },
            task_id="station-transfer-task",
        )
        self.db.record_activity(source.id, "future_metric", 4, "manual")

        export_path = Path(self.temp_dir.name) / "station-data.json"
        exported = self.db.export_station_data(source.id, export_path)
        self.assertEqual(exported["event_count"], 8)
        payload = json.loads(export_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["format"], Database.STATION_EXPORT_FORMAT)
        self.assertEqual(payload["station"]["username"], "source_station")
        self.assertNotIn("password", export_path.read_text(encoding="utf-8").lower())
        self.assertTrue(all(event["event_uid"] for event in payload["events"]))

        imported = self.db.import_station_data(target.id, export_path)
        self.assertEqual(imported["imported"], 8)
        self.assertEqual(imported["skipped"], 0)
        source_totals = {
            row["metric_key"]: row["total"]
            for row in self.db.get_user_totals(source.id)
        }
        target_totals = {
            row["metric_key"]: row["total"]
            for row in self.db.get_user_totals(target.id)
        }
        self.assertEqual(target_totals, source_totals)
        target_violations = {
            row["reason"]: row for row in self.db.get_violation_totals(target.id)
        }
        self.assertEqual(target_violations["证件异常"]["has_phone"], 2)

        duplicate = self.db.import_station_data(target.id, export_path)
        self.assertEqual(duplicate["imported"], 0)
        self.assertEqual(duplicate["skipped"], 8)
        with self.assertRaisesRegex(DatabaseError, "仅普通用户站点"):
            self.db.export_station_data(admin.id, export_path)
        with self.assertRaisesRegex(DatabaseError, "仅普通用户站点"):
            self.db.import_station_data(admin.id, export_path)

        invalid_path = Path(self.temp_dir.name) / "invalid.json"
        invalid_path.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(DatabaseError, "不是有效"):
            self.db.import_station_data(target.id, invalid_path)

    def test_yellow_vehicle_totals_filter_new_events_and_keep_legacy_events(self):
        self.db.ensure_default_admin()
        admin = self.db.authenticate(DEFAULT_ADMIN_USERNAME, DEFAULT_ADMIN_PASSWORD)
        station = self.db.create_account(
            "yellow_station",
            "Worker@123",
            "user",
            admin.id,
        )

        # Historical events have no yellow subset and must therefore count as yellow.
        self.db.record_activity_batch(
            station.id,
            {
                WORKFLOW_TOTAL_METRIC: 4,
                WORKFLOW_HAS_PHONE_METRIC: 2,
            },
            "unified_workflow",
            details={
                "violation_counts": {
                    "历史原因": {"total": 4, "has_phone": 2, "other": 2}
                }
            },
            task_id="legacy-yellow-compatible",
        )
        self.db.record_activity_batch(
            station.id,
            {
                WORKFLOW_TOTAL_METRIC: 6,
                WORKFLOW_HAS_PHONE_METRIC: 3,
            },
            "unified_workflow",
            details={
                "yellow_counts": {
                    WORKFLOW_TOTAL_METRIC: 2,
                    WORKFLOW_HAS_PHONE_METRIC: 1,
                },
                "violation_counts": {
                    "混合原因": {"total": 5, "has_phone": 3, "other": 2}
                },
                "yellow_violation_counts": {
                    "混合原因": {"total": 2, "has_phone": 1, "other": 1}
                },
            },
            task_id="new-yellow-breakdown",
        )

        all_totals = {
            row["metric_key"]: row["total"]
            for row in self.db.get_user_totals(station.id)
        }
        yellow_totals = {
            row["metric_key"]: row["total"]
            for row in self.db.get_user_totals(station.id, yellow_only=True)
        }
        self.assertEqual(all_totals[WORKFLOW_TOTAL_METRIC], 10)
        self.assertEqual(all_totals[WORKFLOW_HAS_PHONE_METRIC], 5)
        self.assertEqual(yellow_totals[WORKFLOW_TOTAL_METRIC], 6)
        self.assertEqual(yellow_totals[WORKFLOW_HAS_PHONE_METRIC], 3)

        all_violations = {
            row["reason"]: row for row in self.db.get_violation_totals(station.id)
        }
        yellow_violations = {
            row["reason"]: row
            for row in self.db.get_violation_totals(station.id, yellow_only=True)
        }
        self.assertEqual(all_violations["历史原因"]["total"], 4)
        self.assertEqual(yellow_violations["历史原因"]["total"], 4)
        self.assertEqual(all_violations["混合原因"]["total"], 5)
        self.assertEqual(yellow_violations["混合原因"]["total"], 2)

        target = self.db.create_account(
            "yellow_import_target",
            "Worker@123",
            "user",
            admin.id,
        )
        export_path = Path(self.temp_dir.name) / "yellow-statistics.json"
        self.db.export_station_data(station.id, export_path)
        self.db.import_station_data(target.id, export_path)
        imported_yellow_totals = {
            row["metric_key"]: row["total"]
            for row in self.db.get_user_totals(target.id, yellow_only=True)
        }
        self.assertEqual(imported_yellow_totals[WORKFLOW_TOTAL_METRIC], 6)
        self.assertEqual(imported_yellow_totals[WORKFLOW_HAS_PHONE_METRIC], 3)

    def test_date_ranges_filter_dashboard_transfer_and_reset_data(self):
        self.db.ensure_default_admin()
        admin = self.db.authenticate(DEFAULT_ADMIN_USERNAME, DEFAULT_ADMIN_PASSWORD)
        source = self.db.create_account(
            "dated_source",
            "Worker@123",
            "user",
            admin.id,
            display_name="日期来源站",
        )
        target = self.db.create_account(
            "dated_target",
            "Worker@123",
            "user",
            admin.id,
            display_name="日期目标站",
        )
        dated_batches = (
            ("range-early", "2025-01-01T09:00:00+08:00", 2, 1, "早期异常"),
            ("range-middle", "2025-01-15T10:00:00+08:00", 4, 3, "中期异常"),
            ("range-late", "2025-02-01T11:00:00+08:00", 8, 5, "后期异常"),
        )
        for task_id, created_at, total, has_phone, reason in dated_batches:
            self.db.record_activity_batch(
                source.id,
                {
                    WORKFLOW_TOTAL_METRIC: total,
                    WORKFLOW_HAS_PHONE_METRIC: has_phone,
                },
                "unified_workflow",
                details={
                    "violation_counts": {
                        reason: {
                            "total": total,
                            "has_phone": has_phone,
                            "other": total - has_phone,
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

        middle_totals = {
            row["metric_key"]: row["total"]
            for row in self.db.get_user_totals(
                source.id,
                "2025-01-15",
                "2025-01-15",
            )
        }
        self.assertEqual(middle_totals[WORKFLOW_TOTAL_METRIC], 4)
        self.assertEqual(middle_totals[WORKFLOW_HAS_PHONE_METRIC], 3)
        all_rows = self.db.get_all_account_totals("2025-01-15", "2025-01-15")
        source_total = next(
            row["total"]
            for row in all_rows
            if row["user_id"] == source.id
            and row["metric_key"] == WORKFLOW_TOTAL_METRIC
        )
        self.assertEqual(source_total, 4)
        self.assertEqual(
            self.db.get_violation_totals(
                source.id,
                start_date="2025-01-15",
                end_date="2025-01-15",
            )[0]["reason"],
            "中期异常",
        )
        recent = self.db.get_recent_activity(
            source.id,
            start_date="2025-01-15",
            end_date="2025-01-15",
        )
        self.assertEqual(len(recent), 2)

        export_path = Path(self.temp_dir.name) / "dated-all.json"
        exported = self.db.export_station_data(source.id, export_path)
        self.assertEqual(exported["event_count"], 6)
        imported = self.db.import_station_data(
            target.id,
            export_path,
            "2025-01-15",
            "2025-01-15",
        )
        self.assertEqual(imported["file_total"], 6)
        self.assertEqual(imported["filtered_out"], 4)
        self.assertEqual(imported["imported"], 2)
        target_totals = {
            row["metric_key"]: row["total"]
            for row in self.db.get_user_totals(target.id)
        }
        self.assertEqual(target_totals[WORKFLOW_TOTAL_METRIC], 4)
        self.assertEqual(target_totals[WORKFLOW_HAS_PHONE_METRIC], 3)

        reset = self.db.reset_station_statistics(
            source.id,
            "2025-01-15",
            "2025-01-15",
        )
        self.assertEqual(reset["deleted"], 2)
        remaining_totals = {
            row["metric_key"]: row["total"]
            for row in self.db.get_user_totals(source.id)
        }
        self.assertEqual(remaining_totals[WORKFLOW_TOTAL_METRIC], 10)
        self.assertEqual(remaining_totals[WORKFLOW_HAS_PHONE_METRIC], 6)
        with self.assertRaisesRegex(ValueError, "开始日期不能晚于结束日期"):
            self.db.get_user_totals(source.id, "2025-02-01", "2025-01-01")

    def test_delete_account_removes_statistics_and_preserves_default_deletion(self):
        self.db.ensure_default_admin()
        admin = self.db.authenticate(DEFAULT_ADMIN_USERNAME, DEFAULT_ADMIN_PASSWORD)
        self.assertEqual(self.db.ensure_default_station_users(), 5)
        target = next(
            account for account in self.db.list_accounts()
            if account.username == "luogang"
        )
        child = self.db.create_account(
            "created_child",
            "Worker@123",
            "user",
            target.id,
        )
        self.db.record_activity(target.id, WORKFLOW_TOTAL_METRIC, 7, "manual")

        result = self.db.delete_account(target.id)

        self.assertEqual(result["deleted_events"], 1)
        self.assertEqual(result["account"]["username"], "luogang")
        with self.assertRaises(AuthenticationError):
            self.db.authenticate("luogang", DEFAULT_STATION_PASSWORD)
        self.assertEqual(self.db.ensure_default_station_users(), 0)
        accounts = {account.id: account for account in self.db.list_accounts()}
        self.assertIn(child.id, accounts)
        with self.db._connect() as conn:
            created_by = conn.execute(
                "SELECT created_by FROM accounts WHERE id=?",
                (child.id,),
            ).fetchone()["created_by"]
        self.assertIsNone(created_by)
        with self.assertRaisesRegex(DatabaseError, "最后一个管理员"):
            self.db.delete_account(admin.id)
        with self.assertRaisesRegex(DatabaseError, "账号不存在"):
            self.db.delete_account(999999)

    def test_reset_station_statistics_preserves_account_and_other_users(self):
        self.db.ensure_default_admin()
        admin = self.db.authenticate(DEFAULT_ADMIN_USERNAME, DEFAULT_ADMIN_PASSWORD)
        target = self.db.create_account(
            "reset_target",
            "Worker@123",
            "user",
            admin.id,
            display_name="待重置站",
        )
        other = self.db.create_account(
            "reset_other",
            "Worker@123",
            "user",
            admin.id,
            display_name="保留站",
        )
        self.db.record_activity_batch(
            target.id,
            {
                WORKFLOW_TOTAL_METRIC: 6,
                WORKFLOW_HAS_PHONE_METRIC: 4,
            },
            "unified_workflow",
            details={
                "violation_counts": {
                    "证件异常": {"total": 2, "has_phone": 1, "other": 1}
                }
            },
            task_id="reset-target-task",
        )
        self.db.record_activity(other.id, WORKFLOW_TOTAL_METRIC, 9, "manual")

        result = self.db.reset_station_statistics(target.id)

        self.assertEqual(result["deleted"], 2)
        self.assertEqual(result["station"]["display_name"], "待重置站")
        target_totals = {
            row["metric_key"]: row["total"]
            for row in self.db.get_user_totals(target.id)
        }
        other_totals = {
            row["metric_key"]: row["total"]
            for row in self.db.get_user_totals(other.id)
        }
        self.assertEqual(target_totals[WORKFLOW_TOTAL_METRIC], 0)
        self.assertEqual(target_totals[WORKFLOW_HAS_PHONE_METRIC], 0)
        self.assertEqual(self.db.get_violation_totals(target.id), [])
        self.assertEqual(other_totals[WORKFLOW_TOTAL_METRIC], 9)
        self.assertEqual(
            self.db.authenticate("reset_target", "Worker@123").id,
            target.id,
        )
        with self.assertRaisesRegex(DatabaseError, "仅普通用户站点支持重置"):
            self.db.reset_station_statistics(admin.id)

    def test_station_users_display_names_and_six_character_passwords(self):
        self.db.ensure_default_admin()
        self.assertEqual(self.db.ensure_default_station_users(), 5)
        self.assertEqual(self.db.ensure_default_station_users(), 0)

        accounts = {account.username: account for account in self.db.list_accounts()}
        for display_name, username in DEFAULT_STATION_USERS:
            self.assertIn(username, accounts)
            self.assertEqual(accounts[username].display_name, display_name)
            self.assertEqual(accounts[username].role, "user")
            self.assertFalse(accounts[username].must_change_password)
            authenticated = self.db.authenticate(username, DEFAULT_STATION_PASSWORD)
            self.assertEqual(authenticated.name_label, display_name)

        validate_password("123456")
        with self.assertRaises(ValueError):
            validate_password("12345")

        luogang = accounts["luogang"]
        self.db.change_password(luogang.id, "654321", must_change=False)
        self.db.ensure_default_station_users()
        self.assertEqual(self.db.authenticate("luogang", "654321").name_label, "萝岗中心站")
        self.db.update_account_display_name(luogang.id, "萝岗业务站")
        self.db.ensure_default_station_users()
        self.assertEqual(
            self.db.authenticate("luogang", "654321").name_label,
            "萝岗业务站",
        )
        with self.assertRaises(AuthenticationError):
            self.db.authenticate("luogang", DEFAULT_STATION_PASSWORD)

    def test_violation_reason_totals_can_filter_by_station(self):
        self.db.ensure_default_admin()
        admin = self.db.authenticate(DEFAULT_ADMIN_USERNAME, DEFAULT_ADMIN_PASSWORD)
        station_a = self.db.create_account(
            "stationa", "123456", "user", admin.id, display_name="甲中心站"
        )
        station_b = self.db.create_account(
            "stationb", "123456", "user", admin.id, display_name="乙中心站"
        )
        counts = {
            WORKFLOW_TOTAL_METRIC: 3,
            WORKFLOW_EMPTY_METRIC: 0,
            WORKFLOW_NO_TRANSPORT_METRIC: 0,
            WORKFLOW_NO_OPERATION_METRIC: 0,
            WORKFLOW_INDIVIDUAL_METRIC: 0,
            WORKFLOW_NO_PHONE_METRIC: 1,
            WORKFLOW_HAS_PHONE_METRIC: 2,
        }
        self.db.record_activity_batch(
            station_a.id,
            counts,
            "unified_workflow",
            details={
                "violation_counts": {
                    "超限|证件异常": {"total": 3, "has_phone": 2, "other": 1},
                    "证件异常": {"total": 1, "has_phone": 0, "other": 1},
                    "其它改变缴费路径逃费|恶意U": {
                        "total": 2,
                        "has_phone": 1,
                        "other": 1,
                    },
                    "J形行驶": {
                        "total": 2,
                        "has_phone": 1,
                        "other": 1,
                    },
                    "车型异常": {
                        "total": 2,
                        "has_phone": 1,
                        "other": 1,
                    },
                }
            },
            task_id="station-a-reasons",
        )
        self.db.record_activity_batch(
            station_b.id,
            counts,
            "unified_workflow",
            details={
                "violation_counts": {
                    "超限": {"total": 2, "has_phone": 1, "other": 1},
                }
            },
            task_id="station-b-reasons",
        )

        station_rows = {
            row["reason"]: row for row in self.db.get_violation_totals(station_a.id)
        }
        self.assertEqual(station_rows["超限"], {
            "reason": "超限", "total": 3, "has_phone": 2, "other": 1
        })
        self.assertEqual(station_rows["证件异常"], {
            "reason": "证件异常", "total": 4, "has_phone": 2, "other": 2
        })
        self.assertEqual(station_rows["恶意U/J形行驶"], {
            "reason": "恶意U/J形行驶", "total": 2, "has_phone": 1, "other": 1
        })
        self.assertEqual(station_rows["其它改变缴费路径逃费"], {
            "reason": "其它改变缴费路径逃费", "total": 2,
            "has_phone": 1, "other": 1
        })
        self.assertNotIn("恶意U", station_rows)
        self.assertNotIn("J形行驶", station_rows)
        all_rows = {row["reason"]: row for row in self.db.get_violation_totals(users_only=True)}
        self.assertEqual(all_rows["超限"]["total"], 5)
        self.assertEqual(all_rows["超限"]["has_phone"], 3)

    def test_extensible_activity_totals(self):
        self.db.ensure_default_admin()
        admin = self.db.authenticate(DEFAULT_ADMIN_USERNAME, DEFAULT_ADMIN_PASSWORD)
        user = self.db.create_account("worker02", "Worker@456", "user", admin.id)

        workflow_counts = {
            WORKFLOW_TOTAL_METRIC: 12,
            WORKFLOW_EMPTY_METRIC: 1,
            WORKFLOW_NO_TRANSPORT_METRIC: 2,
            WORKFLOW_NO_OPERATION_METRIC: 1,
            WORKFLOW_INDIVIDUAL_METRIC: 2,
            WORKFLOW_NO_PHONE_METRIC: 3,
            WORKFLOW_HAS_PHONE_METRIC: 3,
        }
        self.db.record_activity_batch(
            user.id,
            workflow_counts,
            "unified_workflow",
            task_id="workflow-task-1",
        )
        # 同一个任务编号重复提交时不得重复入账。
        self.db.record_activity_batch(
            user.id,
            workflow_counts,
            "unified_workflow",
            task_id="workflow-task-1",
        )
        self.db.record_activity(user.id, "future_metric", 4, "test")
        self.db.record_activity(user.id, "transport_query_completed", 99, "legacy_test")

        totals = {row["metric_key"]: row["total"] for row in self.db.get_user_totals(user.id)}
        self.assertEqual(totals[WORKFLOW_TOTAL_METRIC], 12)
        self.assertEqual(totals[WORKFLOW_EMPTY_METRIC], 1)
        self.assertEqual(totals[WORKFLOW_NO_TRANSPORT_METRIC], 2)
        self.assertEqual(totals[WORKFLOW_NO_OPERATION_METRIC], 1)
        self.assertEqual(totals[WORKFLOW_INDIVIDUAL_METRIC], 2)
        self.assertEqual(totals[WORKFLOW_NO_PHONE_METRIC], 3)
        self.assertEqual(totals[WORKFLOW_HAS_PHONE_METRIC], 3)
        self.assertEqual(totals["future_metric"], 4)
        self.assertNotIn("transport_query_completed", totals)

        all_rows = self.db.get_all_account_totals()
        self.assertTrue(any(row["username"] == "worker02" for row in all_rows))
        recent = self.db.get_recent_activity(user.id)
        self.assertEqual(len(recent), 8)

    def test_legacy_step_metrics_are_hidden_but_history_is_preserved(self):
        self.db.ensure_default_admin()
        admin = self.db.authenticate(DEFAULT_ADMIN_USERNAME, DEFAULT_ADMIN_PASSWORD)
        with self.db._connect() as conn:
            conn.execute(
                """
                INSERT INTO metric_definitions(metric_key, label, unit, sort_order, is_active)
                VALUES ('aiqicha_query_completed', '旧爱企查计数', '条', 30, 1)
                """
            )
            conn.execute(
                """
                INSERT INTO metric_definitions(metric_key, label, unit, sort_order, is_active)
                VALUES ('workflow_total_completed', '旧版完整流程总计', '条', 10, 1)
                """
            )
            conn.execute(
                """
                INSERT INTO activity_events(
                    user_id, metric_key, amount, source, details_json, created_at
                ) VALUES (?, 'aiqicha_query_completed', 7, 'legacy', '{}', ?)
                """,
                (admin.id, self.db._now()),
            )
            conn.execute(
                """
                INSERT INTO activity_events(
                    user_id, metric_key, amount, source, details_json, created_at
                ) VALUES (?, 'workflow_total_completed', 11, 'legacy_v1', '{}', ?)
                """,
                (admin.id, self.db._now()),
            )

        self.db.initialize()
        active_keys = {row["metric_key"] for row in self.db.get_metric_definitions()}
        self.assertNotIn("aiqicha_query_completed", active_keys)
        self.assertNotIn("workflow_total_completed", active_keys)
        self.assertEqual(self.db.get_recent_activity(admin.id), [])
        with self.db._connect() as conn:
            history_count = conn.execute(
                """
                SELECT COUNT(*) FROM activity_events
                WHERE metric_key IN ('aiqicha_query_completed', 'workflow_total_completed')
                """
            ).fetchone()[0]
            is_active = conn.execute(
                "SELECT is_active FROM metric_definitions WHERE metric_key='aiqicha_query_completed'"
            ).fetchone()[0]
        self.assertEqual(history_count, 2)
        self.assertEqual(is_active, 0)


if __name__ == "__main__":
    unittest.main()

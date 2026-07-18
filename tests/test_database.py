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

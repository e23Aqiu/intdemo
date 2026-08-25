import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional

from .config import DEFAULT_ADMIN_PASSWORD, DEFAULT_ADMIN_USERNAME, get_database_path
from .models import Account
from .security import hash_password, validate_password, verify_password


WORKFLOW_TOTAL_METRIC = "workflow_detail_total"
WORKFLOW_EMPTY_METRIC = "workflow_detail_empty"
WORKFLOW_NO_TRANSPORT_METRIC = "workflow_detail_no_transport"
WORKFLOW_NO_OPERATION_METRIC = "workflow_detail_no_operation"
WORKFLOW_INDIVIDUAL_METRIC = "workflow_detail_individual"
WORKFLOW_NO_PHONE_METRIC = "workflow_detail_company_no_phone"
WORKFLOW_HAS_PHONE_METRIC = "workflow_detail_company_has_phone"
WORKFLOW_METRIC_KEYS = (
    WORKFLOW_TOTAL_METRIC,
    WORKFLOW_EMPTY_METRIC,
    WORKFLOW_NO_TRANSPORT_METRIC,
    WORKFLOW_NO_OPERATION_METRIC,
    WORKFLOW_INDIVIDUAL_METRIC,
    WORKFLOW_NO_PHONE_METRIC,
    WORKFLOW_HAS_PHONE_METRIC,
)

DEFAULT_STATION_USERS = (
    ("萝岗中心站", "luogang"),
    ("太平中心站", "taiping"),
    ("道滘中心站", "daojiao"),
    ("宝安中心站", "baoan"),
    ("南头中心站", "nantou"),
)
DEFAULT_STATION_PASSWORD = "123456"


def split_violation_reasons(value) -> List[str]:
    """仅按半角竖线分隔违规原因，并在单条记录内去重。"""
    reasons = []
    seen = set()
    for raw_reason in str(value).split("|"):
        reason = raw_reason.strip()
        if not reason or reason in seen:
            continue
        seen.add(reason)
        reasons.append(reason)
    return reasons


class DatabaseError(RuntimeError):
    pass


class AuthenticationError(DatabaseError):
    pass


class Database:
    """SQLite 数据访问层。

    每个操作使用独立连接，避免后续从工作线程记录统计时共享连接。
    """

    STATION_EXPORT_FORMAT = "intdemo-station-data"
    STATION_EXPORT_VERSION = 1
    MAX_IMPORT_BYTES = 100 * 1024 * 1024

    DEFAULT_METRICS = (
        (WORKFLOW_TOTAL_METRIC, "总计数", "条", 10),
        (WORKFLOW_EMPTY_METRIC, "空", "条", 20),
        (WORKFLOW_NO_TRANSPORT_METRIC, "无运输证号", "条", 30),
        (WORKFLOW_NO_OPERATION_METRIC, "无营运信息", "条", 40),
        (WORKFLOW_INDIVIDUAL_METRIC, "个体经营", "条", 50),
        (WORKFLOW_NO_PHONE_METRIC, "无电话（有公司名）", "条", 60),
        (WORKFLOW_HAS_PHONE_METRIC, "有电话（有公司名）", "条", 70),
    )
    LEGACY_METRICS = (
        "transport_query_completed",
        "business_backfill_completed",
        "aiqicha_query_completed",
        "workflow_total_completed",
        "workflow_no_transport",
        "workflow_no_phone_with_transport",
        "workflow_has_phone",
    )

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path or get_database_path())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._schema_lock = threading.Lock()
        self.initialize()

    @staticmethod
    def _now() -> str:
        return datetime.now().astimezone().isoformat(timespec="seconds")

    @staticmethod
    def _coerce_date(value, label: str):
        if value is None or value == "":
            return None
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        try:
            return date.fromisoformat(str(value).strip())
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label}格式无效") from exc

    @classmethod
    def _normalize_date_range(cls, start_date=None, end_date=None):
        start_day = cls._coerce_date(start_date, "开始日期")
        end_day = cls._coerce_date(end_date, "结束日期")
        if start_day and end_day and start_day > end_day:
            raise ValueError("开始日期不能晚于结束日期")
        return start_day, end_day

    @classmethod
    def _date_range_clause(
        cls,
        column: str,
        start_date=None,
        end_date=None,
    ):
        start_day, end_day = cls._normalize_date_range(start_date, end_date)
        clauses = []
        params = []
        if start_day:
            clauses.append(f"{column}>=?")
            params.append(f"{start_day.isoformat()}T00:00:00")
        if end_day:
            try:
                end_exclusive = end_day + timedelta(days=1)
                clauses.append(f"{column}<?")
                params.append(f"{end_exclusive.isoformat()}T00:00:00")
            except OverflowError:
                clauses.append(f"{column}<=?")
                params.append(f"{end_day.isoformat()}T23:59:59.999999")
        sql = "".join(f" AND {clause}" for clause in clauses)
        return sql, params, start_day, end_day

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(str(self.path), timeout=20)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 20000")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self) -> None:
        with self._schema_lock, self._connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    display_name TEXT NOT NULL DEFAULT '',
                    password_salt TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL CHECK (role IN ('admin', 'user')),
                    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
                    must_change_password INTEGER NOT NULL DEFAULT 0 CHECK (must_change_password IN (0, 1)),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_login TEXT,
                    created_by INTEGER REFERENCES accounts(id),
                    server_account_id TEXT,
                    stats_scope TEXT NOT NULL DEFAULT 'own',
                    is_archived INTEGER NOT NULL DEFAULT 0,
                    entitlement_revision INTEGER NOT NULL DEFAULT 0,
                    is_test INTEGER NOT NULL DEFAULT 0,
                    account_type TEXT NOT NULL DEFAULT 'station',
                    data_scope TEXT NOT NULL DEFAULT 'own',
                    road_id TEXT,
                    road_name TEXT
                );

                CREATE TABLE IF NOT EXISTS metric_definitions (
                    metric_key TEXT PRIMARY KEY,
                    label TEXT NOT NULL,
                    unit TEXT NOT NULL DEFAULT '条',
                    sort_order INTEGER NOT NULL DEFAULT 100,
                    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1))
                );

                CREATE TABLE IF NOT EXISTS activity_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES accounts(id),
                    metric_key TEXT NOT NULL REFERENCES metric_definitions(metric_key),
                    amount INTEGER NOT NULL DEFAULT 1,
                    yellow_amount INTEGER,
                    source TEXT NOT NULL DEFAULT '',
                    task_id TEXT,
                    event_uid TEXT,
                    details_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS deleted_default_accounts (
                    username TEXT PRIMARY KEY COLLATE NOCASE,
                    deleted_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS workflow_batches (
                    batch_id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                    source_path TEXT NOT NULL,
                    source_signature TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'incomplete'
                        CHECK (status IN ('incomplete', 'running', 'succeeded', 'cancelled')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT
                );

                CREATE TABLE IF NOT EXISTS workflow_runs (
                    run_id TEXT PRIMARY KEY,
                    batch_id TEXT NOT NULL REFERENCES workflow_batches(batch_id) ON DELETE CASCADE,
                    user_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                    process_session_id TEXT NOT NULL,
                    process_id INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL
                        CHECK (status IN (
                            'running', 'paused', 'stopping', 'stopped',
                            'succeeded', 'failed', 'interrupted'
                        )),
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    heartbeat_at TEXT NOT NULL,
                    heartbeat_ts REAL NOT NULL,
                    active_ms INTEGER NOT NULL DEFAULT 0,
                    paused_ms INTEGER NOT NULL DEFAULT 0,
                    current_step INTEGER NOT NULL DEFAULT 0,
                    stop_reason TEXT NOT NULL DEFAULT '',
                    error_summary TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS workflow_step_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES workflow_runs(run_id) ON DELETE CASCADE,
                    step_no INTEGER NOT NULL,
                    attempt_no INTEGER NOT NULL,
                    status TEXT NOT NULL
                        CHECK (status IN (
                            'running', 'paused', 'stopping', 'stopped',
                            'succeeded', 'failed', 'interrupted'
                        )),
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    active_ms INTEGER NOT NULL DEFAULT 0,
                    paused_ms INTEGER NOT NULL DEFAULT 0,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    UNIQUE(run_id, step_no, attempt_no)
                );

                CREATE TABLE IF NOT EXISTS workflow_timer_events (
                    event_uid TEXT PRIMARY KEY,
                    batch_id TEXT NOT NULL REFERENCES workflow_batches(batch_id) ON DELETE CASCADE,
                    run_id TEXT NOT NULL REFERENCES workflow_runs(run_id) ON DELETE CASCADE,
                    attempt_id TEXT REFERENCES workflow_step_attempts(attempt_id) ON DELETE SET NULL,
                    event_type TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    details_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS online_client_state (
                    id INTEGER PRIMARY KEY CHECK (id=1),
                    device_uid TEXT NOT NULL,
                    secure_profile BLOB,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sync_state (
                    id INTEGER PRIMARY KEY CHECK (id=1),
                    last_revision INTEGER NOT NULL DEFAULT 0,
                    needs_snapshot INTEGER NOT NULL DEFAULT 1
                        CHECK (needs_snapshot IN (0, 1)),
                    stats_scope TEXT NOT NULL DEFAULT 'own'
                        CHECK (stats_scope IN ('own', 'all')),
                    data_scope TEXT NOT NULL DEFAULT 'own'
                        CHECK (data_scope IN ('own', 'road', 'all')),
                    entitlement_revision INTEGER NOT NULL DEFAULT 0,
                    current_server_account_id TEXT,
                    last_sync_at TEXT,
                    last_error TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sync_outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    server_account_id TEXT NOT NULL,
                    event_uid TEXT NOT NULL UNIQUE,
                    kind TEXT NOT NULL CHECK (kind IN (
                        'activity_event',
                        'workflow_batch_snapshot',
                        'workflow_run_snapshot'
                    )),
                    entity_id TEXT NOT NULL,
                    revision INTEGER NOT NULL CHECK (revision > 0),
                    occurred_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'retry', 'sent', 'quarantined')),
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at REAL NOT NULL DEFAULT 0,
                    last_error_code TEXT,
                    last_error_message TEXT,
                    server_revision INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS captcha_upload_outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    server_account_id TEXT NOT NULL,
                    event_uid TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sync_entity_revisions (
                    kind TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    PRIMARY KEY(kind, entity_id)
                );

                CREATE TABLE IF NOT EXISTS remote_workflow_batches (
                    server_account_id TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    event_uid TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    input_fingerprint TEXT NOT NULL,
                    business_date TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    elapsed_ms INTEGER NOT NULL DEFAULT 0,
                    active_ms INTEGER NOT NULL DEFAULT 0,
                    paused_ms INTEGER NOT NULL DEFAULT 0,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    counters_json TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY(server_account_id, entity_id)
                );

                CREATE TABLE IF NOT EXISTS remote_workflow_runs (
                    server_account_id TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    event_uid TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    batch_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    step_index INTEGER NOT NULL DEFAULT 0,
                    started_at TEXT,
                    ended_at TEXT,
                    elapsed_ms INTEGER NOT NULL DEFAULT 0,
                    active_ms INTEGER NOT NULL DEFAULT 0,
                    paused_ms INTEGER NOT NULL DEFAULT 0,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(server_account_id, entity_id)
                );

                CREATE INDEX IF NOT EXISTS idx_activity_user_time
                    ON activity_events(user_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_activity_metric_time
                    ON activity_events(metric_key, created_at);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_activity_task_metric
                    ON activity_events(user_id, metric_key, task_id)
                    WHERE task_id IS NOT NULL;
                CREATE INDEX IF NOT EXISTS idx_workflow_batch_lookup
                    ON workflow_batches(user_id, source_path, source_signature, status);
                CREATE INDEX IF NOT EXISTS idx_workflow_run_batch
                    ON workflow_runs(batch_id, started_at);
                CREATE INDEX IF NOT EXISTS idx_workflow_run_heartbeat
                    ON workflow_runs(status, heartbeat_ts);
                CREATE INDEX IF NOT EXISTS idx_workflow_step_run
                    ON workflow_step_attempts(run_id, step_no, attempt_no);
                CREATE INDEX IF NOT EXISTS idx_sync_outbox_due
                    ON sync_outbox(status, next_attempt_at, id);
                CREATE INDEX IF NOT EXISTS idx_captcha_upload_outbox_account
                    ON captcha_upload_outbox(server_account_id, id);
                """
            )
            account_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(accounts)").fetchall()
            }
            if "display_name" not in account_columns:
                conn.execute("ALTER TABLE accounts ADD COLUMN display_name TEXT NOT NULL DEFAULT ''")
            if "server_account_id" not in account_columns:
                conn.execute("ALTER TABLE accounts ADD COLUMN server_account_id TEXT")
            if "stats_scope" not in account_columns:
                conn.execute(
                    "ALTER TABLE accounts ADD COLUMN stats_scope TEXT NOT NULL DEFAULT 'own'"
                )
            if "is_archived" not in account_columns:
                conn.execute(
                    "ALTER TABLE accounts ADD COLUMN is_archived INTEGER NOT NULL DEFAULT 0"
                )
            if "entitlement_revision" not in account_columns:
                conn.execute(
                    "ALTER TABLE accounts ADD COLUMN entitlement_revision INTEGER NOT NULL DEFAULT 0"
                )
            if "is_test" not in account_columns:
                conn.execute(
                    "ALTER TABLE accounts ADD COLUMN is_test INTEGER NOT NULL DEFAULT 0"
                )
            account_type_added = "account_type" not in account_columns
            if account_type_added:
                conn.execute(
                    "ALTER TABLE accounts ADD COLUMN account_type TEXT NOT NULL DEFAULT 'station'"
                )
                conn.execute(
                    "UPDATE accounts SET account_type='admin' WHERE role='admin'"
                )
            data_scope_added = "data_scope" not in account_columns
            if data_scope_added:
                conn.execute(
                    "ALTER TABLE accounts ADD COLUMN data_scope TEXT NOT NULL DEFAULT 'own'"
                )
                conn.execute(
                    "UPDATE accounts SET data_scope=stats_scope"
                )
            if "road_id" not in account_columns:
                conn.execute("ALTER TABLE accounts ADD COLUMN road_id TEXT")
            if "road_name" not in account_columns:
                conn.execute("ALTER TABLE accounts ADD COLUMN road_name TEXT")
            sync_state_columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(sync_state)").fetchall()
            }
            if "data_scope" not in sync_state_columns:
                conn.execute(
                    "ALTER TABLE sync_state ADD COLUMN data_scope TEXT NOT NULL DEFAULT 'own'"
                )
                conn.execute("UPDATE sync_state SET data_scope=stats_scope")
            conn.execute(
                "UPDATE accounts SET display_name=username WHERE TRIM(display_name)=''"
            )
            activity_columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(activity_events)").fetchall()
            }
            if "event_uid" not in activity_columns:
                conn.execute("ALTER TABLE activity_events ADD COLUMN event_uid TEXT")
            if "remote_cached" not in activity_columns:
                conn.execute(
                    "ALTER TABLE activity_events ADD COLUMN remote_cached INTEGER NOT NULL DEFAULT 0"
                )
            if "yellow_amount" not in activity_columns:
                conn.execute(
                    "ALTER TABLE activity_events ADD COLUMN yellow_amount INTEGER"
                )
            workflow_run_columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(workflow_runs)").fetchall()
            }
            if "process_id" not in workflow_run_columns:
                conn.execute(
                    "ALTER TABLE workflow_runs ADD COLUMN process_id INTEGER NOT NULL DEFAULT 0"
                )
            missing_event_uids = conn.execute(
                "SELECT id FROM activity_events WHERE event_uid IS NULL OR TRIM(event_uid)=''"
            ).fetchall()
            conn.executemany(
                "UPDATE activity_events SET event_uid=? WHERE id=?",
                [(uuid.uuid4().hex, row["id"]) for row in missing_event_uids],
            )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_activity_user_event_uid
                ON activity_events(user_id, event_uid)
                WHERE event_uid IS NOT NULL
                """
            )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_accounts_server_account_id
                ON accounts(server_account_id)
                WHERE server_account_id IS NOT NULL
                """
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO sync_state(id, updated_at)
                VALUES (1, ?)
                """,
                (self._now(),),
            )
            sync_state_columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(sync_state)").fetchall()
            }
            if "current_server_account_id" not in sync_state_columns:
                conn.execute(
                    "ALTER TABLE sync_state ADD COLUMN current_server_account_id TEXT"
                )
            sync_outbox_columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(sync_outbox)").fetchall()
            }
            if "server_account_id" not in sync_outbox_columns:
                conn.execute(
                    "ALTER TABLE sync_outbox ADD COLUMN server_account_id TEXT"
                )
            conn.executemany(
                """
                INSERT INTO metric_definitions(metric_key, label, unit, sort_order)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(metric_key) DO UPDATE SET
                    label=excluded.label,
                    unit=excluded.unit,
                    sort_order=excluded.sort_order,
                    is_active=1
                """,
                self.DEFAULT_METRICS,
            )
            placeholders = ",".join("?" for _ in self.LEGACY_METRICS)
            conn.execute(
                f"UPDATE metric_definitions SET is_active=0 WHERE metric_key IN ({placeholders})",
                self.LEGACY_METRICS,
            )

    @staticmethod
    def _account_from_row(row: sqlite3.Row) -> Account:
        columns = set(row.keys())
        return Account(
            id=int(row["id"]),
            username=row["username"],
            display_name=row["display_name"] or row["username"],
            role=row["role"],
            is_active=bool(row["is_active"]),
            created_at=row["created_at"],
            last_login=row["last_login"],
            must_change_password=bool(row["must_change_password"]),
            server_account_id=(
                row["server_account_id"] if "server_account_id" in columns else None
            ),
            stats_scope=(
                row["stats_scope"] if "stats_scope" in columns else "own"
            ),
            is_archived=bool(
                row["is_archived"] if "is_archived" in columns else False
            ),
            entitlement_revision=int(
                row["entitlement_revision"]
                if "entitlement_revision" in columns
                else 0
            ),
            is_test=bool(row["is_test"] if "is_test" in columns else False),
            account_type=(
                row["account_type"]
                if "account_type" in columns
                else ("admin" if row["role"] == "admin" else "station")
            ),
            data_scope=(
                row["data_scope"]
                if "data_scope" in columns
                else (row["stats_scope"] if "stats_scope" in columns else "own")
            ),
            road_id=(row["road_id"] if "road_id" in columns else None),
            road_name=(row["road_name"] if "road_name" in columns else None),
        )

    @staticmethod
    def _validate_username(username: str) -> str:
        username = (username or "").strip()
        if not 3 <= len(username) <= 32:
            raise ValueError("账号长度需要在 3 到 32 个字符之间")
        if any(ch.isspace() for ch in username):
            raise ValueError("账号中不能包含空格")
        return username

    def ensure_default_admin(self) -> bool:
        """确保至少存在一个管理员；返回是否创建/恢复了默认管理员。"""
        with self._connect() as conn:
            active_admin = conn.execute(
                "SELECT id FROM accounts WHERE role='admin' AND is_active=1 LIMIT 1"
            ).fetchone()
            if active_admin:
                return False

            salt, digest = hash_password(DEFAULT_ADMIN_PASSWORD)
            now = self._now()
            existing = conn.execute(
                "SELECT id FROM accounts WHERE username=? COLLATE NOCASE",
                (DEFAULT_ADMIN_USERNAME,),
            ).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE accounts
                    SET password_salt=?, password_hash=?, role='admin', is_active=1,
                        must_change_password=1, updated_at=?
                    WHERE id=?
                    """,
                    (salt, digest, now, existing["id"]),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO accounts(
                        username, display_name, password_salt, password_hash, role, is_active,
                        must_change_password, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'admin', 1, 1, ?, ?)
                    """,
                    (DEFAULT_ADMIN_USERNAME, "系统管理员", salt, digest, now, now),
                )
            return True

    def ensure_default_station_users(self) -> int:
        """幂等创建预置中心站普通用户，返回本次新建数量。"""
        with self._connect() as conn:
            admin_row = conn.execute(
                "SELECT id FROM accounts WHERE role='admin' ORDER BY id LIMIT 1"
            ).fetchone()
            created_by = admin_row["id"] if admin_row else None
            created = 0
            for display_name, username in DEFAULT_STATION_USERS:
                deleted = conn.execute(
                    "SELECT 1 FROM deleted_default_accounts WHERE username=? COLLATE NOCASE",
                    (username,),
                ).fetchone()
                if deleted:
                    continue
                existing = conn.execute(
                    "SELECT id, display_name FROM accounts WHERE username=? COLLATE NOCASE",
                    (username,),
                ).fetchone()
                if existing:
                    current_name = (existing["display_name"] or "").strip()
                    if not current_name or current_name.casefold() == username.casefold():
                        conn.execute(
                            "UPDATE accounts SET display_name=?, updated_at=? WHERE id=?",
                            (display_name, self._now(), existing["id"]),
                        )
                    continue
                salt, digest = hash_password(DEFAULT_STATION_PASSWORD)
                now = self._now()
                conn.execute(
                    """
                    INSERT INTO accounts(
                        username, display_name, password_salt, password_hash, role,
                        is_active, must_change_password, created_at, updated_at, created_by
                    ) VALUES (?, ?, ?, ?, 'user', 1, 0, ?, ?, ?)
                    """,
                    (username, display_name, salt, digest, now, now, created_by),
                )
                created += 1
            return created

    def authenticate(self, username: str, password: str) -> Account:
        username = (username or "").strip()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM accounts WHERE username=? COLLATE NOCASE", (username,)
            ).fetchone()
            if not row or not verify_password(password or "", row["password_salt"], row["password_hash"]):
                raise AuthenticationError("账号或密码不正确")
            if not row["is_active"]:
                raise AuthenticationError("账号已停用，请联系管理员")
            now = self._now()
            conn.execute("UPDATE accounts SET last_login=? WHERE id=?", (now, row["id"]))
            refreshed = dict(row)
            refreshed["last_login"] = now
            return self._account_from_row(refreshed)

    def create_account(
        self, username: str, password: str, role: str, created_by: int,
        display_name: str = "", is_test: bool = False
    ) -> Account:
        username = self._validate_username(username)
        display_name = (display_name or username).strip()
        if not 1 <= len(display_name) <= 64:
            raise ValueError("用户名称长度需要在 1 到 64 个字符之间")
        validate_password(password)
        if role == "test":
            role = "user"
            is_test = True
        if role not in {"admin", "user"}:
            raise ValueError("无效的账号角色")
        if is_test and role != "user":
            raise ValueError("测试账号必须使用普通用户权限")
        salt, digest = hash_password(password)
        now = self._now()
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    """
                    INSERT INTO accounts(
                        username, display_name, password_salt, password_hash, role, is_active,
                        must_change_password, created_at, updated_at, created_by, is_test
                    ) VALUES (?, ?, ?, ?, ?, 1, 1, ?, ?, ?, ?)
                    """,
                    (
                        username,
                        display_name,
                        salt,
                        digest,
                        role,
                        now,
                        now,
                        created_by,
                        int(bool(is_test)),
                    ),
                )
                conn.execute(
                    "DELETE FROM deleted_default_accounts WHERE username=? COLLATE NOCASE",
                    (username,),
                )
                row = conn.execute("SELECT * FROM accounts WHERE id=?", (cur.lastrowid,)).fetchone()
                return self._account_from_row(row)
        except sqlite3.IntegrityError as exc:
            raise DatabaseError("该账号已存在") from exc

    def list_accounts(self) -> List[Account]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM accounts ORDER BY role, username COLLATE NOCASE"
            ).fetchall()
            return [self._account_from_row(row) for row in rows]

    def get_account(self, account_id: int) -> Optional[Account]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM accounts WHERE id=?",
                (int(account_id),),
            ).fetchone()
            return self._account_from_row(row) if row else None

    def account_statistics_enabled(self, account_id: int) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT is_test FROM accounts WHERE id=?",
                (int(account_id),),
            ).fetchone()
        if not row:
            raise DatabaseError("账号不存在")
        return not bool(row["is_test"])

    def update_account_display_name(
        self, account_id: int, display_name: str
    ) -> Account:
        display_name = (display_name or "").strip()
        if not 1 <= len(display_name) <= 64:
            raise ValueError("用户名称长度需要在 1 到 64 个字符之间")
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT id FROM accounts WHERE id=?",
                (int(account_id),),
            ).fetchone()
            if not existing:
                raise DatabaseError("账号不存在")
            conn.execute(
                "UPDATE accounts SET display_name=?, updated_at=? WHERE id=?",
                (display_name, self._now(), int(account_id)),
            )
            row = conn.execute(
                "SELECT * FROM accounts WHERE id=?",
                (int(account_id),),
            ).fetchone()
            return self._account_from_row(row)

    def update_account_role(self, account_id: int, role: str) -> Account:
        is_test = role == "test"
        if is_test:
            role = "user"
        if role not in {"admin", "user"}:
            raise ValueError("无效的账号权限")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM accounts WHERE id=?",
                (int(account_id),),
            ).fetchone()
            if not row:
                raise DatabaseError("账号不存在")
            if row["role"] == role and bool(row["is_test"] or 0) == is_test:
                return self._account_from_row(row)
            if row["role"] == "admin" and role == "user" and row["is_active"]:
                active_admins = conn.execute(
                    "SELECT COUNT(*) FROM accounts WHERE role='admin' AND is_active=1"
                ).fetchone()[0]
                if active_admins <= 1:
                    raise DatabaseError("不能取消最后一个可用管理员的权限")
            conn.execute(
                "UPDATE accounts SET role=?, is_test=?, updated_at=? WHERE id=?",
                (role, int(is_test), self._now(), int(account_id)),
            )
            updated = conn.execute(
                "SELECT * FROM accounts WHERE id=?",
                (int(account_id),),
            ).fetchone()
            return self._account_from_row(updated)

    def delete_account(self, account_id: int) -> Dict:
        """Permanently delete an account and its statistics in one transaction."""
        target_id = int(account_id)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM accounts WHERE id=?",
                (target_id,),
            ).fetchone()
            if not row:
                raise DatabaseError("账号不存在")
            if row["role"] == "admin":
                admin_count = conn.execute(
                    "SELECT COUNT(*) FROM accounts WHERE role='admin'"
                ).fetchone()[0]
                if admin_count <= 1:
                    raise DatabaseError("不能删除系统中的最后一个管理员账号")

            event_cursor = conn.execute(
                "DELETE FROM activity_events WHERE user_id=?",
                (target_id,),
            )
            conn.execute(
                "UPDATE accounts SET created_by=NULL WHERE created_by=?",
                (target_id,),
            )
            default_usernames = {username.casefold() for _, username in DEFAULT_STATION_USERS}
            if row["username"].casefold() in default_usernames:
                conn.execute(
                    """
                    INSERT INTO deleted_default_accounts(username, deleted_at)
                    VALUES (?, ?)
                    ON CONFLICT(username) DO UPDATE SET deleted_at=excluded.deleted_at
                    """,
                    (row["username"], self._now()),
                )
            conn.execute("DELETE FROM accounts WHERE id=?", (target_id,))
            return {
                "account": {
                    "id": target_id,
                    "username": row["username"],
                    "display_name": row["display_name"] or row["username"],
                    "role": row["role"],
                },
                "deleted_events": max(event_cursor.rowcount, 0),
            }

    @staticmethod
    def _station_account_row(
        conn, account_id: int, operation: str = "数据导入和导出"
    ):
        row = conn.execute(
            "SELECT * FROM accounts WHERE id=?",
            (int(account_id),),
        ).fetchone()
        if not row:
            raise DatabaseError("账号不存在")
        if row["role"] != "user":
            raise DatabaseError(f"仅普通用户站点支持{operation}")
        return row

    def export_station_data(
        self,
        account_id: int,
        file_path: Path,
        start_date=None,
        end_date=None,
    ) -> Dict:
        target = Path(file_path)
        date_sql, date_params, start_day, end_day = self._date_range_clause(
            "e.created_at",
            start_date,
            end_date,
        )
        with self._connect() as conn:
            account = self._station_account_row(conn, account_id)
            metric_rows = conn.execute(
                f"""
                SELECT DISTINCT m.metric_key, m.label, m.unit, m.sort_order, m.is_active
                FROM metric_definitions m
                JOIN activity_events e ON e.metric_key=m.metric_key
                WHERE e.user_id=?{date_sql}
                ORDER BY m.sort_order, m.metric_key
                """,
                (int(account_id), *date_params),
            ).fetchall()
            event_rows = conn.execute(
                f"""
                SELECT event_uid, metric_key, amount, yellow_amount, source, task_id,
                       details_json, created_at
                FROM activity_events e
                WHERE e.user_id=?{date_sql}
                ORDER BY e.id
                """,
                (int(account_id), *date_params),
            ).fetchall()

        events = []
        for row in event_rows:
            try:
                details = json.loads(row["details_json"] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                details = {}
            if not isinstance(details, dict):
                details = {}
            events.append(
                {
                    "event_uid": row["event_uid"],
                    "metric_key": row["metric_key"],
                    "amount": int(row["amount"]),
                    "yellow_amount": (
                        int(row["yellow_amount"])
                        if row["yellow_amount"] is not None
                        else None
                    ),
                    "source": row["source"] or "",
                    "task_id": row["task_id"],
                    "details": details,
                    "created_at": row["created_at"],
                }
            )
        payload = {
            "format": self.STATION_EXPORT_FORMAT,
            "version": self.STATION_EXPORT_VERSION,
            "exported_at": self._now(),
            "station": {
                "username": account["username"],
                "display_name": account["display_name"] or account["username"],
            },
            "date_range": {
                "start": start_day.isoformat() if start_day else None,
                "end": end_day.isoformat() if end_day else None,
            },
            "metrics": [
                {
                    "metric_key": row["metric_key"],
                    "label": row["label"],
                    "unit": row["unit"],
                    "sort_order": int(row["sort_order"]),
                    "is_active": bool(row["is_active"]),
                }
                for row in metric_rows
            ],
            "events": events,
        }
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f"{target.name}.tmp")
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary.replace(target)
        except OSError as exc:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise DatabaseError(f"导出文件写入失败：{exc}") from exc
        return {
            "station": payload["station"],
            "date_range": payload["date_range"],
            "event_count": len(events),
            "file_path": str(target),
        }

    def import_station_data(
        self,
        account_id: int,
        file_path: Path,
        start_date=None,
        end_date=None,
    ) -> Dict:
        start_day, end_day = self._normalize_date_range(start_date, end_date)
        source_path = Path(file_path)
        try:
            if source_path.stat().st_size > self.MAX_IMPORT_BYTES:
                raise DatabaseError("导入文件超过 100 MB 限制")
            payload = json.loads(source_path.read_text(encoding="utf-8-sig"))
        except DatabaseError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise DatabaseError(f"无法读取导入文件：{exc}") from exc
        if not isinstance(payload, dict):
            raise DatabaseError("导入文件格式无效")
        if payload.get("format") != self.STATION_EXPORT_FORMAT:
            raise DatabaseError("不是有效的站点数据文件")
        if payload.get("version") != self.STATION_EXPORT_VERSION:
            raise DatabaseError("站点数据文件版本不受支持")
        metrics = payload.get("metrics")
        events = payload.get("events")
        station = payload.get("station")
        if not isinstance(metrics, list) or not isinstance(events, list):
            raise DatabaseError("站点数据文件内容不完整")
        if not isinstance(station, dict):
            station = {}

        normalized_metrics = {}
        for metric in metrics:
            if not isinstance(metric, dict):
                raise DatabaseError("统计指标格式无效")
            metric_key = str(metric.get("metric_key") or "").strip()
            if not metric_key:
                raise DatabaseError("统计指标标识不能为空")
            try:
                sort_order = int(metric.get("sort_order", 100))
            except (TypeError, ValueError, OverflowError) as exc:
                raise DatabaseError("统计指标排序值无效") from exc
            normalized_metrics[metric_key] = {
                "label": str(metric.get("label") or metric_key).strip() or metric_key,
                "unit": str(metric.get("unit") or "条").strip() or "条",
                "sort_order": sort_order,
                "is_active": int(bool(metric.get("is_active", True))),
            }

        normalized_events = []
        filtered_out = 0
        for event in events:
            if not isinstance(event, dict):
                raise DatabaseError("统计事件格式无效")
            event_uid = str(event.get("event_uid") or "").strip()
            metric_key = str(event.get("metric_key") or "").strip()
            created_at = str(event.get("created_at") or "").strip()
            details = event.get("details", {})
            if not event_uid or len(event_uid) > 128:
                raise DatabaseError("统计事件唯一标识无效")
            if not metric_key or not created_at:
                raise DatabaseError("统计事件缺少必要字段")
            if not isinstance(details, dict):
                raise DatabaseError("统计事件明细格式无效")
            try:
                event_day = date.fromisoformat(created_at[:10])
            except (TypeError, ValueError) as exc:
                raise DatabaseError("统计事件日期格式无效") from exc
            try:
                amount = int(event.get("amount", 0))
            except (TypeError, ValueError, OverflowError) as exc:
                raise DatabaseError("统计事件数量无效") from exc
            raw_yellow_amount = event.get("yellow_amount")
            try:
                yellow_amount = (
                    int(raw_yellow_amount)
                    if raw_yellow_amount is not None
                    else None
                )
            except (TypeError, ValueError, OverflowError) as exc:
                raise DatabaseError("黄牌统计数量无效") from exc
            if yellow_amount is not None and not 0 <= yellow_amount <= amount:
                raise DatabaseError("黄牌统计数量不能超过总数量")
            if (
                (start_day and event_day < start_day)
                or (end_day and event_day > end_day)
            ):
                filtered_out += 1
                continue
            normalized_events.append(
                {
                    "event_uid": event_uid,
                    "metric_key": metric_key,
                    "amount": amount,
                    "yellow_amount": yellow_amount,
                    "source": str(event.get("source") or ""),
                    "task_id": (
                        str(event["task_id"]) if event.get("task_id") is not None else None
                    ),
                    "details_json": json.dumps(
                        details,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    "created_at": created_at,
                }
            )

        imported = 0
        with self._connect() as conn:
            target_account = self._station_account_row(conn, account_id)
            for metric_key, metric in normalized_metrics.items():
                conn.execute(
                    """
                    INSERT INTO metric_definitions(
                        metric_key, label, unit, sort_order, is_active
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(metric_key) DO UPDATE SET
                        label=excluded.label,
                        unit=excluded.unit,
                        sort_order=excluded.sort_order,
                        is_active=CASE
                            WHEN metric_definitions.is_active=1 THEN 1
                            ELSE excluded.is_active
                        END
                    """,
                    (
                        metric_key,
                        metric["label"],
                        metric["unit"],
                        metric["sort_order"],
                        metric["is_active"],
                    ),
                )
            for event in normalized_events:
                if event["metric_key"] not in normalized_metrics:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO metric_definitions(
                            metric_key, label, unit, sort_order, is_active
                        ) VALUES (?, ?, '条', 100, 1)
                        """,
                        (event["metric_key"], event["metric_key"]),
                    )
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO activity_events(
                        user_id, metric_key, amount, yellow_amount, source, task_id, event_uid,
                        details_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        int(account_id),
                        event["metric_key"],
                        event["amount"],
                        event["yellow_amount"],
                        event["source"],
                        event["task_id"],
                        event["event_uid"],
                        event["details_json"],
                        event["created_at"],
                    ),
                )
                imported += max(cursor.rowcount, 0)

        total = len(normalized_events)
        return {
            "source_station": {
                "username": str(station.get("username") or ""),
                "display_name": str(station.get("display_name") or ""),
            },
            "target_station": {
                "username": target_account["username"],
                "display_name": target_account["display_name"] or target_account["username"],
            },
            "date_range": {
                "start": start_day.isoformat() if start_day else None,
                "end": end_day.isoformat() if end_day else None,
            },
            "file_total": len(events),
            "filtered_out": filtered_out,
            "total": total,
            "imported": imported,
            "skipped": total - imported,
        }

    def reset_station_statistics(
        self,
        account_id: int,
        start_date=None,
        end_date=None,
    ) -> Dict:
        """Delete one ordinary user's statistical events while preserving the account."""
        date_sql, date_params, start_day, end_day = self._date_range_clause(
            "created_at",
            start_date,
            end_date,
        )
        with self._connect() as conn:
            account = self._station_account_row(
                conn,
                account_id,
                operation="重置统计数据",
            )
            cursor = conn.execute(
                f"DELETE FROM activity_events WHERE user_id=?{date_sql}",
                (int(account_id), *date_params),
            )
            deleted = max(cursor.rowcount, 0)
            return {
                "station": {
                    "username": account["username"],
                    "display_name": account["display_name"] or account["username"],
                },
                "date_range": {
                    "start": start_day.isoformat() if start_day else None,
                    "end": end_day.isoformat() if end_day else None,
                },
                "deleted": deleted,
            }

    def change_password(self, account_id: int, new_password: str, must_change: bool = False) -> None:
        validate_password(new_password)
        salt, digest = hash_password(new_password)
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE accounts
                SET password_salt=?, password_hash=?, must_change_password=?, updated_at=?
                WHERE id=?
                """,
                (salt, digest, int(must_change), self._now(), account_id),
            )

    def change_own_password(
        self, account_id: int, current_password: str, new_password: str
    ) -> None:
        """校验当前密码后修改本人密码。"""
        validate_password(new_password)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT password_salt, password_hash FROM accounts WHERE id=?",
                (account_id,),
            ).fetchone()
            if not row:
                raise DatabaseError("账号不存在")
            if not verify_password(
                current_password or "",
                row["password_salt"],
                row["password_hash"],
            ):
                raise AuthenticationError("原密码不正确")
            salt, digest = hash_password(new_password)
            conn.execute(
                """
                UPDATE accounts
                SET password_salt=?, password_hash=?, must_change_password=0, updated_at=?
                WHERE id=?
                """,
                (salt, digest, self._now(), account_id),
            )

    def set_account_active(self, account_id: int, active: bool) -> None:
        with self._connect() as conn:
            row = conn.execute("SELECT role, is_active FROM accounts WHERE id=?", (account_id,)).fetchone()
            if not row:
                raise DatabaseError("账号不存在")
            if row["role"] == "admin" and row["is_active"] and not active:
                count = conn.execute(
                    "SELECT COUNT(*) FROM accounts WHERE role='admin' AND is_active=1"
                ).fetchone()[0]
                if count <= 1:
                    raise DatabaseError("不能停用最后一个可用管理员账号")
            conn.execute(
                "UPDATE accounts SET is_active=?, updated_at=? WHERE id=?",
                (int(active), self._now(), account_id),
            )

    def record_activity(
        self,
        user_id: int,
        metric_key: str,
        amount: int = 1,
        source: str = "",
        details: Optional[Dict] = None,
        task_id: Optional[str] = None,
    ) -> None:
        self.record_activity_batch(
            user_id,
            {metric_key: amount},
            source=source,
            details=details,
            task_id=task_id,
        )

    @staticmethod
    def _sync_safe_details(details: Optional[Dict]) -> Dict:
        """Return only aggregate fields accepted by the central service."""
        source = details if isinstance(details, dict) else {}
        safe = {}
        row_count = source.get("row_count")
        if isinstance(row_count, int) and not isinstance(row_count, bool):
            safe["row_count"] = max(0, row_count)
        for field_name in ("violation_counts", "yellow_violation_counts"):
            violation_counts = source.get(field_name)
            if not isinstance(violation_counts, dict):
                continue
            safe_counts = {}
            for raw_reason, raw_values in list(violation_counts.items())[:200]:
                reason = str(raw_reason or "").strip()[:120]
                if not reason or not isinstance(raw_values, dict):
                    continue
                values = {}
                for key in ("total", "has_phone", "other"):
                    value = raw_values.get(key, 0)
                    if isinstance(value, int) and not isinstance(value, bool):
                        values[key] = max(0, value)
                safe_counts[reason] = values
            safe[field_name] = safe_counts
        return safe

    @staticmethod
    def _server_account_id(conn, user_id: int) -> Optional[str]:
        row = conn.execute(
            "SELECT server_account_id, is_test FROM accounts WHERE id=?",
            (int(user_id),),
        ).fetchone()
        if not row:
            raise DatabaseError("账号不存在")
        if row["is_test"]:
            return None
        return row["server_account_id"]

    @staticmethod
    def _next_sync_revision(conn, kind: str, entity_id: str) -> int:
        row = conn.execute(
            """
            SELECT revision FROM sync_entity_revisions
            WHERE kind=? AND entity_id=?
            """,
            (kind, entity_id),
        ).fetchone()
        revision = int(row["revision"]) + 1 if row else 1
        conn.execute(
            """
            INSERT INTO sync_entity_revisions(kind, entity_id, revision)
            VALUES (?, ?, ?)
            ON CONFLICT(kind, entity_id) DO UPDATE SET revision=excluded.revision
            """,
            (kind, entity_id, revision),
        )
        return revision

    def _enqueue_sync_item(
        self,
        conn,
        *,
        server_account_id: str,
        kind: str,
        entity_id: str,
        revision: int,
        occurred_at: str,
        payload: Dict,
        event_uid: Optional[str] = None,
    ) -> str:
        event_uid = str(event_uid or uuid.uuid4())
        now = self._now()
        conn.execute(
            """
            INSERT INTO sync_outbox(
                server_account_id, event_uid, kind, entity_id, revision, occurred_at,
                payload_json, status, next_attempt_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?)
            """,
            (
                str(server_account_id),
                event_uid,
                kind,
                str(entity_id),
                int(revision),
                occurred_at,
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                now,
                now,
            ),
        )
        return event_uid

    def record_activity_batch(
        self,
        user_id: int,
        activities: Mapping[str, int],
        source: str = "",
        details: Optional[Dict] = None,
        task_id: Optional[str] = None,
    ) -> None:
        """在一个事务内记录一次完整流程的多个统计指标。"""
        normalized = []
        for metric_key, amount in activities.items():
            key = (metric_key or "").strip()
            if not key:
                raise ValueError("统计指标不能为空")
            if key in self.LEGACY_METRICS:
                continue
            value = int(amount)
            if value:
                normalized.append((key, value))
        if not normalized:
            return
        if not self.account_statistics_enabled(user_id):
            return

        detail_source = dict(details) if isinstance(details, dict) else {}
        raw_yellow_counts = detail_source.pop("yellow_counts", None)
        yellow_counts = None
        if raw_yellow_counts is not None:
            if not isinstance(raw_yellow_counts, Mapping):
                raise ValueError("黄牌统计明细格式无效")
            yellow_counts = {}
            for metric_key, amount in normalized:
                if metric_key not in raw_yellow_counts:
                    raise ValueError(f"黄牌统计缺少指标：{metric_key}")
                yellow_amount = int(raw_yellow_counts[metric_key])
                if not 0 <= yellow_amount <= amount:
                    raise ValueError("黄牌统计数量不能超过总数量")
                yellow_counts[metric_key] = yellow_amount
        created_at = self._now()
        with self._connect() as conn:
            server_account_id = self._server_account_id(conn, user_id)
            for metric_key, amount in normalized:
                event_uid = uuid.uuid4().hex
                yellow_amount = (
                    yellow_counts[metric_key]
                    if yellow_counts is not None
                    else None
                )
                event_details = dict(detail_source)
                if metric_key != WORKFLOW_TOTAL_METRIC:
                    event_details.pop("violation_counts", None)
                    event_details.pop("yellow_violation_counts", None)
                payload = json.dumps(
                    event_details,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                sync_summary = self._sync_safe_details(event_details)
                conn.execute(
                    """
                    INSERT OR IGNORE INTO metric_definitions(metric_key, label, unit, sort_order)
                    VALUES (?, ?, '条', 100)
                    """,
                    (metric_key, metric_key),
                )
                conn.execute(
                    """
                    INSERT OR IGNORE INTO activity_events(
                        user_id, metric_key, amount, yellow_amount, source, task_id, event_uid,
                        details_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        metric_key,
                        amount,
                        yellow_amount,
                        source,
                        task_id,
                        event_uid,
                        payload,
                        created_at,
                    ),
                )
                if server_account_id:
                    sync_payload = {
                        "metric_key": metric_key,
                        "amount": amount,
                        "business_date": created_at[:10],
                        "source": source or "client",
                        "task_id": str(task_id) if task_id is not None else None,
                        "summary": sync_summary,
                    }
                    if yellow_amount is not None:
                        sync_payload["yellow_amount"] = yellow_amount
                    self._enqueue_sync_item(
                        conn,
                        server_account_id=server_account_id,
                        kind="activity_event",
                        entity_id=event_uid,
                        revision=1,
                        occurred_at=created_at,
                        event_uid=event_uid,
                        payload=sync_payload,
                    )
            if server_account_id and task_id:
                batch = conn.execute(
                    """
                    SELECT b.batch_id, b.source_signature, b.status, b.created_at,
                           b.completed_at,
                           COALESCE((
                               SELECT SUM(r.active_ms) FROM workflow_runs r
                               WHERE r.batch_id=b.batch_id
                           ), 0) AS active_ms,
                           COALESCE((
                               SELECT SUM(r.paused_ms) FROM workflow_runs r
                               WHERE r.batch_id=b.batch_id
                           ), 0) AS paused_ms,
                           COALESCE((
                               SELECT SUM(s.retry_count)
                               FROM workflow_step_attempts s
                               JOIN workflow_runs r ON r.run_id=s.run_id
                               WHERE r.batch_id=b.batch_id
                           ), 0) AS retry_count
                    FROM workflow_runs final_run
                    JOIN workflow_batches b ON b.batch_id=final_run.batch_id
                    WHERE final_run.run_id=? AND final_run.user_id=?
                    """,
                    (str(task_id), int(user_id)),
                ).fetchone()
                if batch:
                    signature = str(batch["source_signature"])
                    fingerprint = (
                        signature.lower()
                        if len(signature) == 64
                        and all(
                            ch in "0123456789abcdefABCDEF" for ch in signature
                        )
                        else hashlib.sha256(signature.encode("utf-8")).hexdigest()
                    )
                    revision = self._next_sync_revision(
                        conn,
                        "workflow_batch_snapshot",
                        batch["batch_id"],
                    )
                    active_ms = int(batch["active_ms"])
                    paused_ms = int(batch["paused_ms"])
                    self._enqueue_sync_item(
                        conn,
                        server_account_id=server_account_id,
                        kind="workflow_batch_snapshot",
                        entity_id=batch["batch_id"],
                        revision=revision,
                        occurred_at=created_at,
                        payload={
                            "status": batch["status"],
                            "input_fingerprint": fingerprint,
                            "business_date": str(batch["created_at"])[:10],
                            "started_at": batch["created_at"],
                            "completed_at": batch["completed_at"],
                            "elapsed_ms": active_ms + paused_ms,
                            "active_ms": active_ms,
                            "paused_ms": paused_ms,
                            "retry_count": int(batch["retry_count"]),
                            "counters": dict(normalized),
                        },
                    )

    def get_metric_definitions(self) -> List[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                """
                SELECT metric_key, label, unit, sort_order
                FROM metric_definitions WHERE is_active=1
                ORDER BY sort_order, metric_key
                """
            ).fetchall()

    @staticmethod
    def _activity_amount_sql(yellow_only: bool, alias: str = "e") -> str:
        if yellow_only:
            return f"COALESCE({alias}.yellow_amount, {alias}.amount)"
        return f"{alias}.amount"

    def get_user_totals(
        self,
        user_id: int,
        start_date=None,
        end_date=None,
        yellow_only: bool = False,
    ) -> List[sqlite3.Row]:
        date_sql, date_params, _, _ = self._date_range_clause(
            "e.created_at",
            start_date,
            end_date,
        )
        effective_user_id = (
            int(user_id) if self.account_statistics_enabled(user_id) else -1
        )
        amount_sql = self._activity_amount_sql(yellow_only)
        with self._connect() as conn:
            return conn.execute(
                f"""
                SELECT m.metric_key, m.label, m.unit,
                       COALESCE(SUM({amount_sql}), 0) AS total
                FROM metric_definitions m
                LEFT JOIN activity_events e
                  ON e.metric_key=m.metric_key AND e.user_id=?{date_sql}
                WHERE m.is_active=1
                GROUP BY m.metric_key, m.label, m.unit, m.sort_order
                ORDER BY m.sort_order, m.metric_key
                """,
                (effective_user_id, *date_params),
            ).fetchall()

    def get_all_account_totals(
        self,
        start_date=None,
        end_date=None,
        yellow_only: bool = False,
    ) -> List[sqlite3.Row]:
        date_sql, date_params, _, _ = self._date_range_clause(
            "e.created_at",
            start_date,
            end_date,
        )
        amount_sql = self._activity_amount_sql(yellow_only)
        with self._connect() as conn:
            return conn.execute(
                f"""
                SELECT a.id AS user_id, a.username, a.display_name, a.role, a.is_active,
                       m.metric_key, m.label, m.unit,
                       COALESCE(SUM({amount_sql}), 0) AS total
                FROM accounts a
                CROSS JOIN metric_definitions m
                LEFT JOIN activity_events e
                  ON e.user_id=a.id AND e.metric_key=m.metric_key{date_sql}
                WHERE m.is_active=1 AND a.is_test=0
                GROUP BY a.id, a.username, a.display_name, a.role, a.is_active,
                         m.metric_key, m.label, m.unit, m.sort_order
                ORDER BY a.username COLLATE NOCASE, m.sort_order, m.metric_key
                """,
                date_params,
            ).fetchall()

    def get_daily_metric_totals(
        self,
        metric_key: str,
        user_id: Optional[int] = None,
        users_only: bool = False,
        start_date=None,
        end_date=None,
        yellow_only: bool = False,
    ) -> List[Dict]:
        """按本地自然日汇总一个指标，缺少数据的日期由调用方补零。"""
        amount_sql = self._activity_amount_sql(yellow_only)
        sql = f"""
            SELECT SUBSTR(e.created_at, 1, 10) AS activity_date,
                   COALESCE(SUM({amount_sql}), 0) AS total
            FROM activity_events e
            JOIN accounts a ON a.id=e.user_id
            WHERE e.metric_key=? AND a.is_test=0
        """
        params = [str(metric_key)]
        if user_id is not None:
            sql += " AND e.user_id=?"
            params.append(int(user_id))
        elif users_only:
            sql += " AND a.role='user'"
        date_sql, date_params, _, _ = self._date_range_clause(
            "e.created_at",
            start_date,
            end_date,
        )
        sql += date_sql
        params.extend(date_params)
        sql += " GROUP BY activity_date ORDER BY activity_date"

        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [
            {
                "date": row["activity_date"],
                "total": int(row["total"] or 0),
            }
            for row in rows
        ]

    def get_violation_totals(
        self,
        user_id: Optional[int] = None,
        users_only: bool = False,
        start_date=None,
        end_date=None,
        yellow_only: bool = False,
    ) -> List[Dict]:
        """汇总完整流程“原因”列拆分后的违规类型及电话分类。"""
        sql = """
            SELECT e.details_json
            FROM activity_events e
            JOIN accounts a ON a.id=e.user_id
            WHERE e.metric_key=? AND e.source='unified_workflow'
              AND a.is_test=0
        """
        params = [WORKFLOW_TOTAL_METRIC]
        if user_id is not None:
            sql += " AND e.user_id=?"
            params.append(int(user_id))
        elif users_only:
            sql += " AND a.role='user'"
        date_sql, date_params, _, _ = self._date_range_clause(
            "e.created_at",
            start_date,
            end_date,
        )
        sql += date_sql
        params.extend(date_params)

        totals = {}
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        for row in rows:
            try:
                payload = json.loads(row["details_json"] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if yellow_only and "yellow_violation_counts" in payload:
                violation_counts = payload.get("yellow_violation_counts") or {}
            else:
                violation_counts = payload.get("violation_counts") or {}
            if not isinstance(violation_counts, dict):
                continue
            event_totals = {}
            for raw_reason, values in violation_counts.items():
                if not isinstance(values, dict):
                    continue
                for reason in split_violation_reasons(raw_reason):
                    target = event_totals.setdefault(
                        reason,
                        {"total": 0, "has_phone": 0, "other": 0},
                    )
                    target["total"] += int(values.get("total", 0) or 0)
                    target["has_phone"] += int(values.get("has_phone", 0) or 0)
                    target["other"] += int(values.get("other", 0) or 0)

            # 旧版本先按斜线拆分、但尚未按竖线拆分，真实历史键可能是
            # “其它改变缴费路径逃费|恶意U”和“J形行驶”。先展开竖线后，
            # 再把同一事件内数值完全相同的两个碎片合并，且只计数一次。
            legacy_u = event_totals.get("恶意U")
            legacy_j = event_totals.get("J形行驶")
            if legacy_u is not None and legacy_u == legacy_j:
                event_totals.pop("恶意U", None)
                event_totals.pop("J形行驶", None)
                event_totals.setdefault("恶意U/J形行驶", legacy_u)

            for reason, values in event_totals.items():
                target = totals.setdefault(
                    reason,
                    {"reason": reason, "total": 0, "has_phone": 0, "other": 0},
                )
                target["total"] += int(values.get("total", 0) or 0)
                target["has_phone"] += int(values.get("has_phone", 0) or 0)
                target["other"] += int(values.get("other", 0) or 0)
        return sorted(totals.values(), key=lambda item: (-item["total"], item["reason"]))

    def get_recent_activity(
        self, user_id: Optional[int] = None, limit: int = 50,
        users_only: bool = False, start_date=None, end_date=None
    ) -> List[sqlite3.Row]:
        sql = """
            SELECT e.created_at, a.username, a.display_name, m.label, m.unit,
                   e.amount, e.source, e.details_json
            FROM activity_events e
            JOIN accounts a ON a.id=e.user_id
            JOIN metric_definitions m ON m.metric_key=e.metric_key
            WHERE m.is_active=1 AND a.is_test=0
        """
        params: Iterable = ()
        if user_id is not None:
            sql += " AND e.user_id=?"
            params = (user_id,)
        elif users_only:
            sql += " AND a.role='user'"
        date_sql, date_params, _, _ = self._date_range_clause(
            "e.created_at",
            start_date,
            end_date,
        )
        sql += date_sql
        sql += " ORDER BY e.id DESC LIMIT ?"
        params = tuple(params) + tuple(date_params) + (int(limit),)
        with self._connect() as conn:
            return conn.execute(sql, params).fetchall()

    @staticmethod
    def _insert_workflow_timer_event(
        conn,
        batch_id: str,
        run_id: str,
        event_type: str,
        created_at: str,
        attempt_id: Optional[str] = None,
        reason: str = "",
        details: Optional[Dict] = None,
    ) -> None:
        conn.execute(
            """
            INSERT INTO workflow_timer_events(
                event_uid, batch_id, run_id, attempt_id, event_type,
                reason, details_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                uuid.uuid4().hex,
                batch_id,
                run_id,
                attempt_id,
                event_type,
                reason or "",
                json.dumps(details or {}, ensure_ascii=False, separators=(",", ":")),
                created_at,
            ),
        )

    @staticmethod
    def _process_is_running(process_id: int) -> bool:
        process_id = int(process_id or 0)
        if process_id <= 0:
            return False
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = (
                wintypes.DWORD,
                wintypes.BOOL,
                wintypes.DWORD,
            )
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.GetExitCodeProcess.argtypes = (
                wintypes.HANDLE,
                ctypes.POINTER(wintypes.DWORD),
            )
            kernel32.GetExitCodeProcess.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
            kernel32.CloseHandle.restype = wintypes.BOOL
            process_query_limited_information = 0x1000
            still_active = 259
            handle = kernel32.OpenProcess(
                process_query_limited_information,
                False,
                process_id,
            )
            if not handle:
                return ctypes.get_last_error() == 5
            try:
                exit_code = wintypes.DWORD()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return False
                return exit_code.value == still_active
            finally:
                kernel32.CloseHandle(handle)
        try:
            os.kill(process_id, 0)
            return True
        except PermissionError:
            return True
        except (ProcessLookupError, OSError):
            return False

    def recover_stale_workflow_runs(
        self,
        stale_after_seconds: int = 15,
        now_ts: Optional[float] = None,
    ) -> int:
        """把失去心跳的运行标记为异常中断，保留最后一次累计时间。"""
        current_ts = float(time.time() if now_ts is None else now_ts)
        cutoff = current_ts - max(1, int(stale_after_seconds))
        detected_at = self._now()
        recovered = 0
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT run_id, batch_id, user_id, started_at, heartbeat_at,
                       active_ms, paused_ms, current_step, status,
                       heartbeat_ts, process_id
                FROM workflow_runs
                WHERE status IN ('running', 'paused', 'stopping')
                """
            ).fetchall()
            for row in rows:
                process_id = int(row["process_id"] or 0)
                process_dead = process_id > 0 and not self._process_is_running(process_id)
                heartbeat_stale = float(row["heartbeat_ts"] or 0) < cutoff
                legacy_without_process = process_id <= 0 and heartbeat_stale
                if not process_dead and not legacy_without_process:
                    continue
                ended_at = row["heartbeat_at"] or detected_at
                interruption_reason = (
                    "程序进程已退出"
                    if process_dead
                    else "程序异常退出或心跳中断"
                )
                conn.execute(
                    """
                    UPDATE workflow_runs
                    SET status='interrupted', ended_at=?, stop_reason=?,
                        heartbeat_at=?, heartbeat_ts=?
                    WHERE run_id=? AND status IN ('running', 'paused', 'stopping')
                    """,
                    (
                        ended_at,
                        interruption_reason,
                        ended_at,
                        min(current_ts, cutoff),
                        row["run_id"],
                    ),
                )
                conn.execute(
                    """
                    UPDATE workflow_step_attempts
                    SET status='interrupted', ended_at=?,
                        last_error=CASE WHEN last_error='' THEN ? ELSE last_error END
                    WHERE run_id=? AND status IN ('running', 'paused', 'stopping')
                    """,
                    (ended_at, interruption_reason, row["run_id"]),
                )
                conn.execute(
                    """
                    UPDATE workflow_batches
                    SET status='incomplete', updated_at=?
                    WHERE batch_id=?
                    """,
                    (detected_at, row["batch_id"]),
                )
                self._insert_workflow_timer_event(
                    conn,
                    row["batch_id"],
                    row["run_id"],
                    "run_interrupted",
                    detected_at,
                    reason=interruption_reason,
                    details={
                        "last_heartbeat_at": row["heartbeat_at"],
                        "active_ms": int(row["active_ms"]),
                        "paused_ms": int(row["paused_ms"]),
                        "current_step": int(row["current_step"]),
                        "previous_status": row["status"],
                        "process_id": process_id,
                    },
                )
                server_account_id = self._server_account_id(
                    conn,
                    int(row["user_id"]),
                )
                if server_account_id:
                    retry_row = conn.execute(
                        """
                        SELECT COALESCE(SUM(retry_count), 0) AS retry_count
                        FROM workflow_step_attempts WHERE run_id=?
                        """,
                        (row["run_id"],),
                    ).fetchone()
                    run_revision = self._next_sync_revision(
                        conn,
                        "workflow_run_snapshot",
                        row["run_id"],
                    )
                    self._enqueue_sync_item(
                        conn,
                        server_account_id=server_account_id,
                        kind="workflow_run_snapshot",
                        entity_id=row["run_id"],
                        revision=run_revision,
                        occurred_at=detected_at,
                        payload={
                            "batch_id": row["batch_id"],
                            "status": "interrupted",
                            "step_index": int(row["current_step"]),
                            "started_at": row["started_at"],
                            "ended_at": ended_at,
                            "elapsed_ms": int(row["active_ms"])
                            + int(row["paused_ms"]),
                            "active_ms": int(row["active_ms"]),
                            "paused_ms": int(row["paused_ms"]),
                            "retry_count": int(retry_row["retry_count"]),
                        },
                    )
                    batch = conn.execute(
                        """
                        SELECT source_signature, created_at,
                               COALESCE((
                                   SELECT SUM(active_ms) FROM workflow_runs r
                                   WHERE r.batch_id=b.batch_id
                               ), 0) AS active_ms,
                               COALESCE((
                                   SELECT SUM(paused_ms) FROM workflow_runs r
                                   WHERE r.batch_id=b.batch_id
                               ), 0) AS paused_ms
                        FROM workflow_batches b WHERE batch_id=?
                        """,
                        (row["batch_id"],),
                    ).fetchone()
                    signature = str(batch["source_signature"])
                    fingerprint = (
                        signature.lower()
                        if len(signature) == 64
                        and all(
                            ch in "0123456789abcdefABCDEF" for ch in signature
                        )
                        else hashlib.sha256(signature.encode("utf-8")).hexdigest()
                    )
                    batch_revision = self._next_sync_revision(
                        conn,
                        "workflow_batch_snapshot",
                        row["batch_id"],
                    )
                    active_total = int(batch["active_ms"])
                    paused_total = int(batch["paused_ms"])
                    self._enqueue_sync_item(
                        conn,
                        server_account_id=server_account_id,
                        kind="workflow_batch_snapshot",
                        entity_id=row["batch_id"],
                        revision=batch_revision,
                        occurred_at=detected_at,
                        payload={
                            "status": "incomplete",
                            "input_fingerprint": fingerprint,
                            "business_date": str(batch["created_at"])[:10],
                            "started_at": batch["created_at"],
                            "completed_at": None,
                            "elapsed_ms": active_total + paused_total,
                            "active_ms": active_total,
                            "paused_ms": paused_total,
                            "retry_count": int(retry_row["retry_count"]),
                            "counters": {},
                        },
                    )
                recovered += 1
        return recovered

    def start_workflow_run(
        self,
        user_id: int,
        source_path: str,
        source_signature: str,
        run_id: str,
        process_session_id: str,
        process_id: int,
        stale_after_seconds: int = 15,
        source_signature_aliases: Optional[Iterable[str]] = None,
    ) -> Dict:
        """创建一次物理运行，并复用相同文件输入的未完成逻辑批次。"""
        source_path = str(source_path or "").strip()
        source_signature = str(source_signature or "").strip()
        run_id = str(run_id or "").strip()
        process_session_id = str(process_session_id or "").strip()
        process_id = int(process_id or 0)
        if (
            not source_path
            or not source_signature
            or not run_id
            or not process_session_id
            or process_id <= 0
        ):
            raise ValueError("计时任务缺少必要标识")

        self.recover_stale_workflow_runs(stale_after_seconds)
        signature_aliases = [source_signature]
        for alias in source_signature_aliases or ():
            alias = str(alias or "").strip()
            if alias and alias not in signature_aliases:
                signature_aliases.append(alias)
        now = self._now()
        heartbeat_ts = time.time()
        with self._connect() as conn:
            account = conn.execute(
                "SELECT id, is_active, is_test FROM accounts WHERE id=?",
                (int(user_id),),
            ).fetchone()
            if not account or not account["is_active"]:
                raise DatabaseError("当前账号不可用于创建计时任务")
            if account["is_test"]:
                raise DatabaseError("测试账号不记录业务计时")

            signature_placeholders = ",".join("?" for _ in signature_aliases)
            batches = conn.execute(
                f"""
                SELECT * FROM workflow_batches
                WHERE user_id=? AND source_path=?
                  AND source_signature IN ({signature_placeholders})
                  AND status IN ('incomplete', 'running')
                ORDER BY updated_at DESC
                """,
                (int(user_id), source_path, *signature_aliases),
            ).fetchall()
            if batches:
                batch_ids = [row["batch_id"] for row in batches]
                batch_placeholders = ",".join("?" for _ in batch_ids)
                active_run = conn.execute(
                    """
                    SELECT run_id, heartbeat_ts FROM workflow_runs
                    WHERE batch_id IN ({})
                      AND status IN ('running', 'paused', 'stopping')
                    ORDER BY started_at DESC LIMIT 1
                    """.format(batch_placeholders),
                    batch_ids,
                ).fetchone()
                if active_run:
                    raise DatabaseError("同一批次仍在另一个客户端实例中运行")
                batch_id = batches[0]["batch_id"]
                for duplicate in batches[1:]:
                    duplicate_id = duplicate["batch_id"]
                    conn.execute(
                        "UPDATE workflow_timer_events SET batch_id=? WHERE batch_id=?",
                        (batch_id, duplicate_id),
                    )
                    conn.execute(
                        "UPDATE workflow_runs SET batch_id=? WHERE batch_id=?",
                        (batch_id, duplicate_id),
                    )
                    conn.execute(
                        "DELETE FROM workflow_batches WHERE batch_id=?",
                        (duplicate_id,),
                    )
                created_at = min(row["created_at"] for row in batches)
                conn.execute(
                    """
                    UPDATE workflow_batches
                    SET source_signature=?, created_at=?, status='incomplete',
                        updated_at=?
                    WHERE batch_id=?
                    """,
                    (source_signature, created_at, now, batch_id),
                )
                resumed = True
            else:
                batch_id = uuid.uuid4().hex
                resumed = False
                conn.execute(
                    """
                    INSERT INTO workflow_batches(
                        batch_id, user_id, source_path, source_signature, status,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'incomplete', ?, ?)
                    """,
                    (
                        batch_id,
                        int(user_id),
                        source_path,
                        source_signature,
                        now,
                        now,
                    ),
                )

            totals = conn.execute(
                """
                SELECT COALESCE(SUM(active_ms), 0) AS active_ms,
                       COALESCE(SUM(paused_ms), 0) AS paused_ms,
                       COUNT(*) AS run_count
                FROM workflow_runs WHERE batch_id=?
                """,
                (batch_id,),
            ).fetchone()
            try:
                conn.execute(
                    """
                    INSERT INTO workflow_runs(
                        run_id, batch_id, user_id, process_session_id, process_id, status,
                        started_at, heartbeat_at, heartbeat_ts
                    ) VALUES (?, ?, ?, ?, ?, 'running', ?, ?, ?)
                    """,
                    (
                        run_id,
                        batch_id,
                        int(user_id),
                        process_session_id,
                        process_id,
                        now,
                        now,
                        heartbeat_ts,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise DatabaseError("计时运行标识已存在") from exc
            conn.execute(
                """
                UPDATE workflow_batches
                SET status='running', updated_at=?, completed_at=NULL
                WHERE batch_id=?
                """,
                (now, batch_id),
            )
            self._insert_workflow_timer_event(
                conn,
                batch_id,
                run_id,
                "run_started",
                now,
                details={
                    "resumed_batch": resumed,
                    "previous_run_count": int(totals["run_count"]),
                },
            )
            server_account_id = self._server_account_id(conn, user_id)
            if server_account_id:
                fingerprint = (
                    source_signature.lower()
                    if len(source_signature) == 64
                    and all(ch in "0123456789abcdefABCDEF" for ch in source_signature)
                    else hashlib.sha256(source_signature.encode("utf-8")).hexdigest()
                )
                batch_revision = self._next_sync_revision(
                    conn, "workflow_batch_snapshot", batch_id
                )
                self._enqueue_sync_item(
                    conn,
                    server_account_id=server_account_id,
                    kind="workflow_batch_snapshot",
                    entity_id=batch_id,
                    revision=batch_revision,
                    occurred_at=now,
                    payload={
                        "status": "running",
                        "input_fingerprint": fingerprint,
                        "business_date": now[:10],
                        "started_at": now,
                        "completed_at": None,
                        "elapsed_ms": int(totals["active_ms"])
                        + int(totals["paused_ms"]),
                        "active_ms": int(totals["active_ms"]),
                        "paused_ms": int(totals["paused_ms"]),
                        "retry_count": 0,
                        "counters": {},
                    },
                )
                run_revision = self._next_sync_revision(
                    conn, "workflow_run_snapshot", run_id
                )
                self._enqueue_sync_item(
                    conn,
                    server_account_id=server_account_id,
                    kind="workflow_run_snapshot",
                    entity_id=run_id,
                    revision=run_revision,
                    occurred_at=now,
                    payload={
                        "batch_id": batch_id,
                        "status": "running",
                        "step_index": 0,
                        "started_at": now,
                        "ended_at": None,
                        "elapsed_ms": 0,
                        "active_ms": 0,
                        "paused_ms": 0,
                        "retry_count": 0,
                    },
                )
            return {
                "batch_id": batch_id,
                "run_id": run_id,
                "resumed": resumed,
                "previous_active_ms": int(totals["active_ms"]),
                "previous_paused_ms": int(totals["paused_ms"]),
                "run_number": int(totals["run_count"]) + 1,
            }

    def start_workflow_step(self, run_id: str, step_no: int) -> Dict:
        now = self._now()
        with self._connect() as conn:
            run = conn.execute(
                "SELECT batch_id, status FROM workflow_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if not run:
                raise DatabaseError("计时运行不存在")
            if run["status"] not in {"running", "paused", "stopping"}:
                raise DatabaseError("计时运行已经结束")
            attempt_no = conn.execute(
                """
                SELECT COALESCE(MAX(attempt_no), 0) + 1
                FROM workflow_step_attempts WHERE run_id=? AND step_no=?
                """,
                (run_id, int(step_no)),
            ).fetchone()[0]
            attempt_id = uuid.uuid4().hex
            conn.execute(
                """
                INSERT INTO workflow_step_attempts(
                    attempt_id, run_id, step_no, attempt_no, status, started_at
                ) VALUES (?, ?, ?, ?, 'running', ?)
                """,
                (attempt_id, run_id, int(step_no), int(attempt_no), now),
            )
            conn.execute(
                "UPDATE workflow_runs SET current_step=? WHERE run_id=?",
                (int(step_no), run_id),
            )
            self._insert_workflow_timer_event(
                conn,
                run["batch_id"],
                run_id,
                "step_started",
                now,
                attempt_id=attempt_id,
                details={"step_no": int(step_no), "attempt_no": int(attempt_no)},
            )
            return {
                "attempt_id": attempt_id,
                "step_no": int(step_no),
                "attempt_no": int(attempt_no),
            }

    def checkpoint_workflow_timing(
        self,
        run_id: str,
        status: str,
        active_ms: int,
        paused_ms: int,
        current_step: int = 0,
        attempt_id: Optional[str] = None,
        step_status: Optional[str] = None,
        step_active_ms: int = 0,
        step_paused_ms: int = 0,
        event_type: Optional[str] = None,
        reason: str = "",
        details: Optional[Dict] = None,
    ) -> None:
        active_statuses = {"running", "paused", "stopping"}
        if status not in active_statuses:
            raise ValueError("无效的运行中计时状态")
        if step_status is not None and step_status not in active_statuses:
            raise ValueError("无效的步骤计时状态")
        now = self._now()
        now_ts = time.time()
        with self._connect() as conn:
            run = conn.execute(
                "SELECT batch_id, status FROM workflow_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if not run or run["status"] not in active_statuses:
                return
            conn.execute(
                """
                UPDATE workflow_runs
                SET status=?, active_ms=?, paused_ms=?, current_step=?,
                    heartbeat_at=?, heartbeat_ts=?,
                    stop_reason=CASE WHEN ?<>'' THEN ? ELSE stop_reason END
                WHERE run_id=?
                """,
                (
                    status,
                    max(0, int(active_ms)),
                    max(0, int(paused_ms)),
                    max(0, int(current_step)),
                    now,
                    now_ts,
                    reason,
                    reason,
                    run_id,
                ),
            )
            if attempt_id and step_status:
                conn.execute(
                    """
                    UPDATE workflow_step_attempts
                    SET status=?, active_ms=?, paused_ms=?
                    WHERE attempt_id=? AND status IN ('running', 'paused', 'stopping')
                    """,
                    (
                        step_status,
                        max(0, int(step_active_ms)),
                        max(0, int(step_paused_ms)),
                        attempt_id,
                    ),
                )
            conn.execute(
                "UPDATE workflow_batches SET updated_at=? WHERE batch_id=?",
                (now, run["batch_id"]),
            )
            if event_type:
                self._insert_workflow_timer_event(
                    conn,
                    run["batch_id"],
                    run_id,
                    event_type,
                    now,
                    attempt_id=attempt_id,
                    reason=reason,
                    details=details,
                )

    def record_workflow_retry(
        self,
        run_id: str,
        attempt_id: Optional[str],
        retry_type: str,
        reason: str = "",
    ) -> None:
        if not attempt_id:
            return
        now = self._now()
        with self._connect() as conn:
            run = conn.execute(
                "SELECT batch_id, status FROM workflow_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if not run or run["status"] not in {"running", "paused", "stopping"}:
                return
            cursor = conn.execute(
                """
                UPDATE workflow_step_attempts
                SET retry_count=retry_count+1,
                    last_error=CASE WHEN ?<>'' THEN ? ELSE last_error END
                WHERE attempt_id=? AND status IN ('running', 'paused', 'stopping')
                """,
                (reason, reason, attempt_id),
            )
            if cursor.rowcount <= 0:
                return
            self._insert_workflow_timer_event(
                conn,
                run["batch_id"],
                run_id,
                "retry",
                now,
                attempt_id=attempt_id,
                reason=reason,
                details={"retry_type": retry_type},
            )

    def finish_workflow_step(
        self,
        attempt_id: str,
        status: str,
        active_ms: int,
        paused_ms: int,
        error_summary: str = "",
    ) -> None:
        terminal_statuses = {"stopped", "succeeded", "failed", "interrupted"}
        if status not in terminal_statuses:
            raise ValueError("无效的步骤结束状态")
        now = self._now()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT s.run_id, r.batch_id, s.step_no, s.status
                FROM workflow_step_attempts s
                JOIN workflow_runs r ON r.run_id=s.run_id
                WHERE s.attempt_id=?
                """,
                (attempt_id,),
            ).fetchone()
            if not row or row["status"] not in {"running", "paused", "stopping"}:
                return
            conn.execute(
                """
                UPDATE workflow_step_attempts
                SET status=?, ended_at=?, active_ms=?, paused_ms=?,
                    last_error=CASE WHEN ?<>'' THEN ? ELSE last_error END
                WHERE attempt_id=?
                """,
                (
                    status,
                    now,
                    max(0, int(active_ms)),
                    max(0, int(paused_ms)),
                    error_summary,
                    error_summary,
                    attempt_id,
                ),
            )
            self._insert_workflow_timer_event(
                conn,
                row["batch_id"],
                row["run_id"],
                "step_finished",
                now,
                attempt_id=attempt_id,
                reason=error_summary,
                details={"step_no": int(row["step_no"]), "status": status},
            )

    def finish_workflow_run(
        self,
        run_id: str,
        status: str,
        active_ms: int,
        paused_ms: int,
        reason: str = "",
        error_summary: str = "",
    ) -> Dict:
        terminal_statuses = {"stopped", "succeeded", "failed", "interrupted"}
        if status not in terminal_statuses:
            raise ValueError("无效的计时结束状态")
        now = self._now()
        now_ts = time.time()
        with self._connect() as conn:
            run = conn.execute(
                "SELECT batch_id, status FROM workflow_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if not run:
                raise DatabaseError("计时运行不存在")
            if run["status"] in terminal_statuses:
                batch_id = run["batch_id"]
            else:
                batch_id = run["batch_id"]
                conn.execute(
                    """
                    UPDATE workflow_runs
                    SET status=?, ended_at=?, heartbeat_at=?, heartbeat_ts=?,
                        active_ms=?, paused_ms=?, stop_reason=?, error_summary=?
                    WHERE run_id=?
                    """,
                    (
                        status,
                        now,
                        now,
                        now_ts,
                        max(0, int(active_ms)),
                        max(0, int(paused_ms)),
                        reason or "",
                        error_summary or "",
                        run_id,
                    ),
                )
                step_status = "succeeded" if status == "succeeded" else status
                conn.execute(
                    """
                    UPDATE workflow_step_attempts
                    SET status=?, ended_at=?,
                        last_error=CASE WHEN last_error='' THEN ? ELSE last_error END
                    WHERE run_id=? AND status IN ('running', 'paused', 'stopping')
                    """,
                    (step_status, now, error_summary or reason or "", run_id),
                )
                batch_status = "succeeded" if status == "succeeded" else "incomplete"
                conn.execute(
                    """
                    UPDATE workflow_batches
                    SET status=?, updated_at=?, completed_at=?
                    WHERE batch_id=?
                    """,
                    (
                        batch_status,
                        now,
                        now if batch_status == "succeeded" else None,
                        batch_id,
                    ),
                )
                self._insert_workflow_timer_event(
                    conn,
                    batch_id,
                    run_id,
                    "run_finished",
                    now,
                    reason=reason or error_summary,
                    details={"status": status},
                )

            totals = conn.execute(
                """
                SELECT COALESCE(SUM(active_ms), 0) AS active_ms,
                       COALESCE(SUM(paused_ms), 0) AS paused_ms,
                       COUNT(*) AS run_count
                FROM workflow_runs WHERE batch_id=?
                """,
                (batch_id,),
            ).fetchone()
            run_snapshot = conn.execute(
                """
                SELECT r.user_id, r.started_at, r.ended_at, r.current_step,
                       r.status, r.active_ms, r.paused_ms,
                       COALESCE(SUM(s.retry_count), 0) AS retry_count
                FROM workflow_runs r
                LEFT JOIN workflow_step_attempts s ON s.run_id=r.run_id
                WHERE r.run_id=?
                GROUP BY r.run_id
                """,
                (run_id,),
            ).fetchone()
            batch_snapshot = conn.execute(
                """
                SELECT b.source_signature, b.status, b.created_at, b.completed_at,
                       COALESCE(SUM(s.retry_count), 0) AS retry_count
                FROM workflow_batches b
                LEFT JOIN workflow_runs r ON r.batch_id=b.batch_id
                LEFT JOIN workflow_step_attempts s ON s.run_id=r.run_id
                WHERE b.batch_id=?
                GROUP BY b.batch_id
                """,
                (batch_id,),
            ).fetchone()
            server_account_id = (
                self._server_account_id(conn, int(run_snapshot["user_id"]))
                if run_snapshot
                else None
            )
            if run_snapshot and batch_snapshot and server_account_id:
                run_revision = self._next_sync_revision(
                    conn, "workflow_run_snapshot", run_id
                )
                self._enqueue_sync_item(
                    conn,
                    server_account_id=server_account_id,
                    kind="workflow_run_snapshot",
                    entity_id=run_id,
                    revision=run_revision,
                    occurred_at=now,
                    payload={
                        "batch_id": batch_id,
                        "status": run_snapshot["status"],
                        "step_index": int(run_snapshot["current_step"]),
                        "started_at": run_snapshot["started_at"],
                        "ended_at": run_snapshot["ended_at"],
                        "elapsed_ms": int(run_snapshot["active_ms"])
                        + int(run_snapshot["paused_ms"]),
                        "active_ms": int(run_snapshot["active_ms"]),
                        "paused_ms": int(run_snapshot["paused_ms"]),
                        "retry_count": int(run_snapshot["retry_count"]),
                    },
                )
                signature = str(batch_snapshot["source_signature"])
                fingerprint = (
                    signature.lower()
                    if len(signature) == 64
                    and all(ch in "0123456789abcdefABCDEF" for ch in signature)
                    else hashlib.sha256(signature.encode("utf-8")).hexdigest()
                )
                batch_revision = self._next_sync_revision(
                    conn, "workflow_batch_snapshot", batch_id
                )
                self._enqueue_sync_item(
                    conn,
                    server_account_id=server_account_id,
                    kind="workflow_batch_snapshot",
                    entity_id=batch_id,
                    revision=batch_revision,
                    occurred_at=now,
                    payload={
                        "status": batch_snapshot["status"],
                        "input_fingerprint": fingerprint,
                        "business_date": str(batch_snapshot["created_at"])[:10],
                        "started_at": batch_snapshot["created_at"],
                        "completed_at": batch_snapshot["completed_at"],
                        "elapsed_ms": int(totals["active_ms"])
                        + int(totals["paused_ms"]),
                        "active_ms": int(totals["active_ms"]),
                        "paused_ms": int(totals["paused_ms"]),
                        "retry_count": int(batch_snapshot["retry_count"]),
                        "counters": {},
                    },
                )
            return {
                "batch_id": batch_id,
                "active_ms": int(totals["active_ms"]),
                "paused_ms": int(totals["paused_ms"]),
                "run_count": int(totals["run_count"]),
            }

    def get_workflow_batch_summary(self, batch_id: str) -> Dict:
        with self._connect() as conn:
            batch = conn.execute(
                "SELECT * FROM workflow_batches WHERE batch_id=?",
                (batch_id,),
            ).fetchone()
            if not batch:
                raise DatabaseError("计时批次不存在")
            totals = conn.execute(
                """
                SELECT COALESCE(SUM(active_ms), 0) AS active_ms,
                       COALESCE(SUM(paused_ms), 0) AS paused_ms,
                       COUNT(*) AS run_count,
                       SUM(CASE WHEN status='stopped' THEN 1 ELSE 0 END) AS stopped_count,
                       SUM(CASE WHEN status='interrupted' THEN 1 ELSE 0 END) AS interrupted_count
                FROM workflow_runs WHERE batch_id=?
                """,
                (batch_id,),
            ).fetchone()
            return {
                "batch_id": batch_id,
                "status": batch["status"],
                "source_path": batch["source_path"],
                "source_signature": batch["source_signature"],
                "active_ms": int(totals["active_ms"]),
                "paused_ms": int(totals["paused_ms"]),
                "run_count": int(totals["run_count"]),
                "stopped_count": int(totals["stopped_count"] or 0),
                "interrupted_count": int(totals["interrupted_count"] or 0),
            }

    def get_workflow_timing_totals(
        self,
        user_id: Optional[int] = None,
        users_only: bool = False,
        start_date=None,
        end_date=None,
        yellow_only: bool = False,
    ) -> Dict:
        """只汇总已经成功完成完整流程的批次及其全部运行时间。"""
        amount_sql = self._activity_amount_sql(yellow_only)
        completed_batches_sql = f"""
            SELECT b.batch_id, COALESCE(SUM({amount_sql}), 0) AS completed_items
            FROM activity_events e
            JOIN workflow_runs final_run
              ON final_run.run_id=e.task_id AND final_run.user_id=e.user_id
            JOIN workflow_batches b ON b.batch_id=final_run.batch_id
            JOIN accounts a ON a.id=e.user_id
            WHERE e.metric_key=? AND e.source='unified_workflow'
              AND final_run.status='succeeded' AND b.status='succeeded'
              AND a.is_test=0
        """
        params = [WORKFLOW_TOTAL_METRIC]

        if user_id is not None:
            completed_batches_sql += " AND e.user_id=?"
            params.append(int(user_id))
        elif users_only:
            completed_batches_sql += " AND a.role='user'"

        date_sql, date_params, _, _ = self._date_range_clause(
            "e.created_at",
            start_date,
            end_date,
        )
        completed_batches_sql += date_sql
        completed_batches_sql += " GROUP BY b.batch_id"
        params.extend(date_params)

        summary_sql = f"""
            WITH completed_batches AS (
                {completed_batches_sql}
            ), timing AS (
                SELECT COALESCE(SUM(r.active_ms), 0) AS active_ms,
                       COALESCE(SUM(r.paused_ms), 0) AS paused_ms,
                       COUNT(*) AS run_count
                FROM workflow_runs r
                JOIN completed_batches completed
                  ON completed.batch_id=r.batch_id
            )
            SELECT timing.active_ms,
                   timing.paused_ms,
                   timing.run_count,
                   COALESCE((
                       SELECT SUM(completed_items) FROM completed_batches
                   ), 0) AS completed_items
            FROM timing
        """

        with self._connect() as conn:
            summary = conn.execute(summary_sql, params).fetchone()
            remote_completed_sql = f"""
                SELECT rb.server_account_id, rb.entity_id AS batch_id,
                       COALESCE(SUM({amount_sql}), 0) AS completed_items
                FROM activity_events e
                JOIN accounts a ON a.id=e.user_id
                JOIN remote_workflow_runs final_run
                  ON final_run.entity_id=e.task_id
                 AND final_run.server_account_id=a.server_account_id
                JOIN remote_workflow_batches rb
                  ON rb.entity_id=final_run.batch_id
                 AND rb.server_account_id=final_run.server_account_id
                WHERE e.metric_key=? AND e.source='unified_workflow'
                  AND final_run.status='succeeded' AND rb.status='succeeded'
                  AND a.is_test=0
                  AND NOT EXISTS (
                      SELECT 1 FROM workflow_runs local_run
                      WHERE local_run.run_id=final_run.entity_id
                  )
            """
            remote_params = [WORKFLOW_TOTAL_METRIC]
            if user_id is not None:
                remote_completed_sql += " AND e.user_id=?"
                remote_params.append(int(user_id))
            elif users_only:
                remote_completed_sql += " AND a.role='user'"
            remote_completed_sql += date_sql
            remote_params.extend(date_params)
            remote_completed_sql += (
                " GROUP BY rb.server_account_id, rb.entity_id"
            )
            remote_summary = conn.execute(
                f"""
                WITH completed_batches AS (
                    {remote_completed_sql}
                ), timing AS (
                    SELECT COALESCE(SUM(r.active_ms), 0) AS active_ms,
                           COALESCE(SUM(r.paused_ms), 0) AS paused_ms,
                           COUNT(*) AS run_count
                    FROM remote_workflow_runs r
                    JOIN completed_batches completed
                      ON completed.batch_id=r.batch_id
                     AND completed.server_account_id=r.server_account_id
                    WHERE NOT EXISTS (
                        SELECT 1 FROM workflow_runs local_run
                        WHERE local_run.run_id=r.entity_id
                    )
                )
                SELECT timing.active_ms, timing.paused_ms, timing.run_count,
                       COALESCE((
                           SELECT SUM(completed_items) FROM completed_batches
                       ), 0) AS completed_items
                FROM timing
                """,
                remote_params,
            ).fetchone()

        active_ms = int(summary["active_ms"] or 0) + int(
            remote_summary["active_ms"] or 0
        )
        paused_ms = int(summary["paused_ms"] or 0) + int(
            remote_summary["paused_ms"] or 0
        )
        return {
            "active_ms": active_ms,
            "paused_ms": paused_ms,
            "total_ms": active_ms + paused_ms,
            "run_count": int(summary["run_count"] or 0)
            + int(remote_summary["run_count"] or 0),
            "completed_items": int(summary["completed_items"] or 0)
            + int(remote_summary["completed_items"] or 0),
        }

    def get_workflow_run(self, run_id: str) -> Optional[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM workflow_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()

    def get_or_create_online_device_uid(self) -> str:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT device_uid FROM online_client_state WHERE id=1"
            ).fetchone()
            if row:
                return row["device_uid"]
            device_uid = str(uuid.uuid4())
            now = self._now()
            conn.execute(
                """
                INSERT INTO online_client_state(
                    id, device_uid, secure_profile, created_at, updated_at
                ) VALUES (1, ?, NULL, ?, ?)
                """,
                (device_uid, now, now),
            )
            return device_uid

    def save_secure_online_profile(self, encrypted_profile: bytes) -> None:
        if not isinstance(encrypted_profile, bytes) or not encrypted_profile:
            raise ValueError("加密登录资料不能为空")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT device_uid FROM online_client_state WHERE id=1"
            ).fetchone()
            device_uid = row["device_uid"] if row else str(uuid.uuid4())
            conn.execute(
                """
                INSERT INTO online_client_state(
                    id, device_uid, secure_profile, created_at, updated_at
                ) VALUES (1, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    secure_profile=excluded.secure_profile,
                    updated_at=excluded.updated_at
                """,
                (
                    device_uid,
                    sqlite3.Binary(encrypted_profile),
                    self._now(),
                    self._now(),
                ),
            )

    def load_secure_online_profile(self) -> Optional[bytes]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT secure_profile FROM online_client_state WHERE id=1"
            ).fetchone()
            if not row or row["secure_profile"] is None:
                return None
            return bytes(row["secure_profile"])

    def clear_secure_online_profile(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE online_client_state
                SET secure_profile=NULL, updated_at=?
                WHERE id=1
                """,
                (self._now(),),
            )

    @staticmethod
    def _upsert_remote_account_conn(conn, payload: Dict) -> int:
        server_account_id = str(payload.get("id") or payload.get("account_id") or "")
        username = str(payload.get("username") or "").strip().lower()
        if not server_account_id or not username:
            raise DatabaseError("服务器账号资料不完整")
        last_login_provided = (
            "last_login_at" in payload or "last_login" in payload
        )
        last_login = payload.get("last_login_at", payload.get("last_login"))
        if last_login is not None:
            last_login = str(last_login)
        is_test_provided = "is_test" in payload
        is_test = int(bool(payload.get("is_test", False)))
        account_type_value = str(
            payload.get("account_type")
            or ("admin" if payload.get("role") == "admin" else "station")
        )
        if account_type_value not in {"admin", "road_admin", "station"}:
            account_type_value = "station"
        data_scope_value = str(
            payload.get("data_scope") or payload.get("stats_scope") or "own"
        )
        if data_scope_value not in {"own", "road", "all"}:
            data_scope_value = "own"
        road_fields_provided = "road_id" in payload or "road_name" in payload
        road_id = payload.get("road_id")
        road_name = payload.get("road_name")
        if road_id is not None:
            road_id = str(road_id)
        if road_name is not None:
            road_name = str(road_name)
        row = conn.execute(
            """
            SELECT id FROM accounts
            WHERE server_account_id=? OR username=? COLLATE NOCASE
            ORDER BY server_account_id IS NOT NULL DESC
            LIMIT 1
            """,
            (server_account_id, username),
        ).fetchone()
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        if row:
            account_id = int(row["id"])
            conn.execute(
                """
                UPDATE accounts
                SET server_account_id=?, username=?, display_name=?, role=?,
                    stats_scope=?, is_active=?, is_archived=?,
                    must_change_password=?, entitlement_revision=?,
                    is_test=CASE WHEN ? THEN ? ELSE is_test END,
                    account_type=?, data_scope=?,
                    road_id=CASE WHEN ? THEN ? ELSE road_id END,
                    road_name=CASE WHEN ? THEN ? ELSE road_name END,
                    last_login=CASE WHEN ? THEN ? ELSE last_login END,
                    updated_at=?
                WHERE id=?
                """,
                (
                    server_account_id,
                    username,
                    str(payload.get("display_name") or username),
                    str(payload.get("role") or "user"),
                    str(payload.get("stats_scope") or "own"),
                    int(bool(payload.get("is_active", True))),
                    int(bool(payload.get("is_archived", False))),
                    int(bool(payload.get("must_change_password", False))),
                    int(payload.get("entitlement_revision") or 0),
                    int(is_test_provided),
                    is_test,
                    account_type_value,
                    data_scope_value,
                    int(road_fields_provided),
                    road_id,
                    int(road_fields_provided),
                    road_name,
                    int(last_login_provided),
                    last_login,
                    now,
                    account_id,
                ),
            )
            if is_test_provided and is_test:
                conn.execute(
                    "DELETE FROM sync_outbox WHERE server_account_id=?",
                    (server_account_id,),
                )
                conn.execute(
                    "DELETE FROM captcha_upload_outbox WHERE server_account_id=?",
                    (server_account_id,),
                )
            return account_id
        salt, digest = hash_password(uuid.uuid4().hex + uuid.uuid4().hex)
        cursor = conn.execute(
            """
            INSERT INTO accounts(
                username, display_name, password_salt, password_hash, role,
                is_active, must_change_password, last_login, created_at, updated_at,
                server_account_id, stats_scope, is_archived,
                entitlement_revision, is_test, account_type, data_scope,
                road_id, road_name
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                username,
                str(payload.get("display_name") or username),
                salt,
                digest,
                str(payload.get("role") or "user"),
                int(bool(payload.get("is_active", True))),
                int(bool(payload.get("must_change_password", False))),
                last_login,
                str(payload.get("created_at") or now),
                str(payload.get("updated_at") or now),
                server_account_id,
                str(payload.get("stats_scope") or "own"),
                int(bool(payload.get("is_archived", False))),
                int(payload.get("entitlement_revision") or 0),
                is_test,
                account_type_value,
                data_scope_value,
                road_id,
                road_name,
            ),
        )
        if is_test_provided and is_test:
            conn.execute(
                "DELETE FROM sync_outbox WHERE server_account_id=?",
                (server_account_id,),
            )
            conn.execute(
                "DELETE FROM captcha_upload_outbox WHERE server_account_id=?",
                (server_account_id,),
            )
        return int(cursor.lastrowid)

    def upsert_remote_account(self, payload: Dict) -> Account:
        with self._connect() as conn:
            account_id = self._upsert_remote_account_conn(conn, payload)
            row = conn.execute(
                "SELECT * FROM accounts WHERE id=?",
                (account_id,),
            ).fetchone()
            return self._account_from_row(row)

    def reconcile_remote_accounts(
        self,
        payloads: Iterable[Dict],
        *,
        prune_missing: bool = True,
    ) -> List[Account]:
        """Mirror one complete server account list into the local cache.

        An account delete change can be missed when a client was offline or its
        local sync cursor predates a server-side cleanup.  The administration
        endpoint returns a complete list, so a successful response is also an
        authoritative opportunity to remove remote accounts that no longer
        exist.  Local-only accounts and the currently signed-in account are
        deliberately preserved.
        """
        rows = list(payloads)
        with self._connect() as conn:
            account_ids = []
            server_account_ids = set()
            for payload in rows:
                account_id = self._upsert_remote_account_conn(conn, payload)
                account_ids.append(account_id)
                server_account_id = str(
                    payload.get("id") or payload.get("account_id") or ""
                ).strip()
                if server_account_id:
                    server_account_ids.add(server_account_id)

            state = conn.execute(
                "SELECT current_server_account_id FROM sync_state WHERE id=1"
            ).fetchone()
            current_server_account_id = (
                str(state["current_server_account_id"] or "").strip()
                if state
                else ""
            )
            if prune_missing:
                cached_ids = conn.execute(
                    """
                    SELECT server_account_id FROM accounts
                    WHERE server_account_id IS NOT NULL
                    """
                ).fetchall()
                for cached in cached_ids:
                    server_account_id = str(cached["server_account_id"] or "").strip()
                    if (
                        server_account_id
                        and server_account_id not in server_account_ids
                        and server_account_id != current_server_account_id
                    ):
                        self._purge_remote_account_cache_conn(
                            conn,
                            server_account_id,
                        )

            accounts = []
            for account_id in account_ids:
                row = conn.execute(
                    "SELECT * FROM accounts WHERE id=?",
                    (account_id,),
                ).fetchone()
                if row:
                    accounts.append(self._account_from_row(row))
            return accounts

    @staticmethod
    def _purge_remote_account_cache_conn(conn, server_account_id: str) -> bool:
        """Remove one remote account and every local row that can identify it."""
        normalized = str(server_account_id or "").strip()
        if not normalized:
            return False
        row = conn.execute(
            "SELECT id FROM accounts WHERE server_account_id=?",
            (normalized,),
        ).fetchone()
        conn.execute(
            "DELETE FROM sync_outbox WHERE server_account_id=?",
            (normalized,),
        )
        conn.execute(
            "DELETE FROM captcha_upload_outbox WHERE server_account_id=?",
            (normalized,),
        )
        conn.execute(
            "DELETE FROM remote_workflow_runs WHERE server_account_id=?",
            (normalized,),
        )
        conn.execute(
            "DELETE FROM remote_workflow_batches WHERE server_account_id=?",
            (normalized,),
        )
        if not row:
            return False
        local_account_id = int(row["id"])
        # The oldest local schema did not declare ON DELETE CASCADE on every
        # business table. Remove timer/run children explicitly before deleting
        # the account so no historical rows can survive a permanent purge.
        conn.execute(
            """
            DELETE FROM workflow_timer_events
            WHERE batch_id IN (
                SELECT batch_id FROM workflow_batches WHERE user_id=?
            )
            OR run_id IN (
                SELECT run_id FROM workflow_runs WHERE user_id=?
            )
            """,
            (local_account_id, local_account_id),
        )
        conn.execute(
            """
            DELETE FROM workflow_step_attempts
            WHERE run_id IN (
                SELECT run_id FROM workflow_runs WHERE user_id=?
            )
            """,
            (local_account_id,),
        )
        conn.execute(
            "DELETE FROM workflow_runs WHERE user_id=?",
            (local_account_id,),
        )
        conn.execute(
            "DELETE FROM workflow_batches WHERE user_id=?",
            (local_account_id,),
        )
        conn.execute(
            "DELETE FROM activity_events WHERE user_id=?",
            (local_account_id,),
        )
        conn.execute(
            "UPDATE accounts SET created_by=NULL WHERE created_by=?",
            (local_account_id,),
        )
        conn.execute("DELETE FROM accounts WHERE id=?", (local_account_id,))
        return True

    def purge_remote_account_cache(self, server_account_id: str) -> bool:
        """Remove a permanently deleted remote account and all local mirrors."""
        normalized = str(server_account_id or "").strip()
        if not normalized:
            return False
        with self._connect() as conn:
            current = conn.execute(
                "SELECT current_server_account_id FROM sync_state WHERE id=1"
            ).fetchone()
            if current and current["current_server_account_id"] == normalized:
                raise DatabaseError("不能删除当前登录账号的本机缓存")
            return self._purge_remote_account_cache_conn(conn, normalized)

    def set_current_online_account(
        self,
        server_account_id: str,
        stats_scope: Optional[str] = None,
        data_scope: Optional[str] = None,
    ) -> None:
        server_account_id = str(server_account_id)
        normalized_scope = str(data_scope or stats_scope or "own")
        if normalized_scope not in {"own", "road", "all"}:
            normalized_scope = "own"
        legacy_scope = "all" if normalized_scope == "all" else "own"
        with self._connect() as conn:
            state = conn.execute("SELECT * FROM sync_state WHERE id=1").fetchone()
            previous_account_id = state["current_server_account_id"]
            previous_scope = str(state["data_scope"] or state["stats_scope"] or "own")
            account_changed = previous_account_id != server_account_id
            scope_changed = (
                previous_account_id == server_account_id
                and previous_scope != normalized_scope
            )
            if account_changed or scope_changed:
                self._purge_other_station_cache_conn(conn, server_account_id)
            conn.execute(
                """
                UPDATE sync_state
                SET current_server_account_id=?,
                    stats_scope=?,
                    data_scope=?,
                    last_revision=CASE WHEN ? THEN 0 ELSE last_revision END,
                    needs_snapshot=CASE WHEN ? THEN 1 ELSE needs_snapshot END,
                    updated_at=?
                WHERE id=1
                """,
                (
                    server_account_id,
                    legacy_scope,
                    normalized_scope,
                    int(account_changed or scope_changed),
                    int(account_changed or scope_changed),
                    self._now(),
                ),
            )

    def enqueue_captcha_upload(
        self,
        server_account_id: str,
        payload: Mapping,
    ) -> int:
        """Persist one CAPTCHA sample upload until the server accepts it."""
        account_id = str(server_account_id or "").strip()
        if not account_id:
            raise ValueError("验证码待同步数据缺少服务器账号")
        if not isinstance(payload, Mapping):
            raise ValueError("验证码待同步数据格式无效")
        now = self._now()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO captcha_upload_outbox(
                    server_account_id, event_uid, payload_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    account_id,
                    uuid.uuid4().hex,
                    json.dumps(
                        dict(payload),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    now,
                    now,
                ),
            )
            return int(cursor.lastrowid)

    def get_captcha_upload_pending_count(self, server_account_id: str) -> int:
        account_id = str(server_account_id or "").strip()
        if not account_id:
            return 0
        with self._connect() as conn:
            return int(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM captcha_upload_outbox
                    WHERE server_account_id=?
                    """,
                    (account_id,),
                ).fetchone()[0]
            )

    def get_pending_captcha_uploads(
        self,
        server_account_id: str,
        limit: int = 100,
    ) -> List[Dict]:
        account_id = str(server_account_id or "").strip()
        if not account_id:
            return []
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, event_uid, payload_json, attempt_count,
                       last_error, created_at, updated_at
                FROM captcha_upload_outbox
                WHERE server_account_id=?
                ORDER BY id
                LIMIT ?
                """,
                (account_id, max(1, min(int(limit), 500))),
            ).fetchall()
        pending = []
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
            except (TypeError, ValueError, json.JSONDecodeError):
                payload = {}
            pending.append(
                {
                    "id": int(row["id"]),
                    "event_uid": row["event_uid"],
                    "payload": payload,
                    "attempt_count": int(row["attempt_count"]),
                    "last_error": row["last_error"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                }
            )
        return pending

    def mark_captcha_upload_sent(self, outbox_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM captcha_upload_outbox WHERE id=?",
                (int(outbox_id),),
            )

    def mark_captcha_upload_failed(self, outbox_id: int, error: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE captcha_upload_outbox
                SET attempt_count=attempt_count+1,
                    last_error=?, updated_at=?
                WHERE id=?
                """,
                (str(error)[:500], self._now(), int(outbox_id)),
            )

    def get_sync_state(self) -> Dict:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM sync_state WHERE id=1").fetchone()
            return {
                "last_revision": int(row["last_revision"]),
                "needs_snapshot": bool(row["needs_snapshot"]),
                "stats_scope": row["stats_scope"],
                "data_scope": row["data_scope"],
                "entitlement_revision": int(row["entitlement_revision"]),
                "current_server_account_id": row["current_server_account_id"],
                "last_sync_at": row["last_sync_at"],
                "last_error": row["last_error"],
            }

    def get_sync_pending_count(
        self,
        server_account_id: Optional[str] = None,
    ) -> int:
        with self._connect() as conn:
            if server_account_id is None:
                state = conn.execute(
                    "SELECT current_server_account_id FROM sync_state WHERE id=1"
                ).fetchone()
                server_account_id = state["current_server_account_id"]
            account_sql = (
                " AND server_account_id=?"
                if server_account_id
                else ""
            )
            params = (server_account_id,) if server_account_id else ()
            return int(
                conn.execute(
                    f"""
                    SELECT COUNT(*) FROM sync_outbox
                    WHERE status IN ('pending', 'retry'){account_sql}
                    """,
                    params,
                ).fetchone()[0]
            )

    def get_sync_quarantined_items(
        self,
        limit: int = 100,
        server_account_id: Optional[str] = None,
    ) -> List[Dict]:
        with self._connect() as conn:
            if server_account_id is None:
                state = conn.execute(
                    "SELECT current_server_account_id FROM sync_state WHERE id=1"
                ).fetchone()
                server_account_id = state["current_server_account_id"]
            account_sql = (
                " AND server_account_id=?"
                if server_account_id
                else ""
            )
            params = []
            if server_account_id:
                params.append(server_account_id)
            params.append(max(1, min(int(limit), 500)))
            rows = conn.execute(
                f"""
                SELECT id, event_uid, kind, entity_id, revision,
                       last_error_code, last_error_message, updated_at
                FROM sync_outbox
                WHERE status='quarantined'{account_sql}
                ORDER BY id DESC LIMIT ?
                """,
                params,
            ).fetchall()
            return [dict(row) for row in rows]

    def get_sync_quarantined_count(
        self,
        server_account_id: Optional[str] = None,
    ) -> int:
        with self._connect() as conn:
            if server_account_id is None:
                state = conn.execute(
                    "SELECT current_server_account_id FROM sync_state WHERE id=1"
                ).fetchone()
                server_account_id = state["current_server_account_id"]
            if server_account_id:
                row = conn.execute(
                    """
                    SELECT COUNT(*) FROM sync_outbox
                    WHERE status='quarantined' AND server_account_id=?
                    """,
                    (server_account_id,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*) FROM sync_outbox WHERE status='quarantined'"
                ).fetchone()
            return int(row[0])

    def enforce_own_cache_for_current_account(self) -> None:
        with self._connect() as conn:
            state = conn.execute("SELECT * FROM sync_state WHERE id=1").fetchone()
            self._purge_other_station_cache_conn(
                conn,
                state["current_server_account_id"],
            )
            conn.execute(
                """
                UPDATE sync_state
                SET stats_scope='own', data_scope='own', updated_at=?
                WHERE id=1
                """,
                (self._now(),),
            )

    def get_due_sync_items(
        self,
        limit: int = 100,
        now_ts: Optional[float] = None,
        server_account_id: Optional[str] = None,
    ) -> List[Dict]:
        due = time.time() if now_ts is None else float(now_ts)
        with self._connect() as conn:
            if server_account_id is None:
                state = conn.execute(
                    "SELECT current_server_account_id FROM sync_state WHERE id=1"
                ).fetchone()
                server_account_id = state["current_server_account_id"]
            account_sql = (
                " AND server_account_id=?"
                if server_account_id
                else ""
            )
            params = [due]
            if server_account_id:
                params.append(server_account_id)
            params.append(max(1, min(int(limit), 100)))
            rows = conn.execute(
                f"""
                SELECT * FROM sync_outbox
                WHERE status IN ('pending', 'retry') AND next_attempt_at<=?
                {account_sql}
                ORDER BY id LIMIT ?
                """,
                params,
            ).fetchall()
        result = []
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
            except (TypeError, ValueError, json.JSONDecodeError):
                payload = {}
            result.append(
                {
                    "id": int(row["id"]),
                    "server_account_id": row["server_account_id"],
                    "event_uid": row["event_uid"],
                    "attempt_count": int(row["attempt_count"]),
                    "item": {
                        "schema_version": 1,
                        "event_uid": row["event_uid"],
                        "kind": row["kind"],
                        "entity_id": row["entity_id"],
                        "revision": int(row["revision"]),
                        "occurred_at": row["occurred_at"],
                        "payload": payload,
                    },
                }
            )
        return result

    def mark_sync_item_sent(
        self,
        outbox_id: int,
        server_revision: Optional[int],
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE sync_outbox
                SET status='sent', server_revision=?, last_error_code=NULL,
                    last_error_message=NULL, updated_at=?
                WHERE id=?
                """,
                (server_revision, self._now(), int(outbox_id)),
            )

    def schedule_sync_retry(
        self,
        outbox_id: int,
        next_attempt_at: float,
        error_code: str,
        error_message: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE sync_outbox
                SET status='retry', attempt_count=attempt_count+1,
                    next_attempt_at=?, last_error_code=?,
                    last_error_message=?, updated_at=?
                WHERE id=?
                """,
                (
                    float(next_attempt_at),
                    str(error_code)[:120],
                    str(error_message)[:500],
                    self._now(),
                    int(outbox_id),
                ),
            )

    def make_sync_retries_due(
        self,
        server_account_id: Optional[str] = None,
    ) -> int:
        """Make delayed retry rows immediately eligible for a manual sync."""
        with self._connect() as conn:
            if server_account_id is None:
                state = conn.execute(
                    "SELECT current_server_account_id FROM sync_state WHERE id=1"
                ).fetchone()
                server_account_id = state["current_server_account_id"]
            account_sql = (
                " AND server_account_id=?"
                if server_account_id
                else ""
            )
            params = [0.0, self._now()]
            if server_account_id:
                params.append(server_account_id)
            cursor = conn.execute(
                f"""
                UPDATE sync_outbox
                SET next_attempt_at=?, updated_at=?
                WHERE status='retry'{account_sql}
                """,
                params,
            )
            return max(cursor.rowcount, 0)

    def quarantine_sync_item(
        self,
        outbox_id: int,
        error_code: str,
        error_message: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE sync_outbox
                SET status='quarantined', attempt_count=attempt_count+1,
                    last_error_code=?, last_error_message=?, updated_at=?
                WHERE id=?
                """,
                (
                    str(error_code)[:120],
                    str(error_message)[:500],
                    self._now(),
                    int(outbox_id),
                ),
            )

    @staticmethod
    def _apply_remote_activity_conn(conn, payload: Dict) -> None:
        server_account_id = str(payload.get("account_id") or "")
        account_row = conn.execute(
            "SELECT id FROM accounts WHERE server_account_id=?",
            (server_account_id,),
        ).fetchone()
        if not account_row:
            return
        metric_key = str(payload.get("metric_key") or "")
        event_uid = str(payload.get("event_uid") or "")
        if not metric_key or not event_uid:
            return
        conn.execute(
            """
            INSERT OR IGNORE INTO metric_definitions(
                metric_key, label, unit, sort_order, is_active
            ) VALUES (?, ?, '条', 1000, 1)
            """,
            (metric_key, metric_key),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO activity_events(
                user_id, metric_key, amount, yellow_amount, source, task_id, event_uid,
                details_json, created_at, remote_cached
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (
                int(account_row["id"]),
                metric_key,
                int(payload.get("amount") or 0),
                (
                    int(payload["yellow_amount"])
                    if payload.get("yellow_amount") is not None
                    else None
                ),
                str(payload.get("source") or "client"),
                payload.get("task_id"),
                event_uid,
                json.dumps(
                    payload.get("summary") or {},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                str(payload.get("occurred_at") or Database._now()),
            ),
        )

    @staticmethod
    def _apply_remote_batch_conn(
        conn,
        server_account_id: str,
        entity_id: str,
        entity_revision: int,
        payload: Dict,
    ) -> None:
        conn.execute(
            """
            INSERT INTO remote_workflow_batches(
                server_account_id, entity_id, event_uid, revision, status,
                input_fingerprint, business_date, started_at, completed_at,
                elapsed_ms, active_ms, paused_ms, retry_count, counters_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(server_account_id, entity_id) DO UPDATE SET
                event_uid=excluded.event_uid,
                revision=excluded.revision,
                status=excluded.status,
                input_fingerprint=excluded.input_fingerprint,
                business_date=excluded.business_date,
                started_at=excluded.started_at,
                completed_at=excluded.completed_at,
                elapsed_ms=excluded.elapsed_ms,
                active_ms=excluded.active_ms,
                paused_ms=excluded.paused_ms,
                retry_count=excluded.retry_count,
                counters_json=excluded.counters_json
            WHERE excluded.revision > remote_workflow_batches.revision
            """,
            (
                server_account_id,
                entity_id,
                str(payload.get("event_uid") or ""),
                int(payload.get("revision") or entity_revision),
                str(payload.get("status") or "incomplete"),
                str(payload.get("input_fingerprint") or ""),
                str(payload.get("business_date") or ""),
                payload.get("started_at"),
                payload.get("completed_at"),
                int(payload.get("elapsed_ms") or 0),
                int(payload.get("active_ms") or 0),
                int(payload.get("paused_ms") or 0),
                int(payload.get("retry_count") or 0),
                json.dumps(
                    payload.get("counters") or {},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            ),
        )

    @staticmethod
    def _apply_remote_run_conn(
        conn,
        server_account_id: str,
        entity_id: str,
        entity_revision: int,
        payload: Dict,
    ) -> None:
        conn.execute(
            """
            INSERT INTO remote_workflow_runs(
                server_account_id, entity_id, event_uid, revision, batch_id,
                status, step_index, started_at, ended_at, elapsed_ms,
                active_ms, paused_ms, retry_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(server_account_id, entity_id) DO UPDATE SET
                event_uid=excluded.event_uid,
                revision=excluded.revision,
                batch_id=excluded.batch_id,
                status=excluded.status,
                step_index=excluded.step_index,
                started_at=excluded.started_at,
                ended_at=excluded.ended_at,
                elapsed_ms=excluded.elapsed_ms,
                active_ms=excluded.active_ms,
                paused_ms=excluded.paused_ms,
                retry_count=excluded.retry_count
            WHERE excluded.revision > remote_workflow_runs.revision
            """,
            (
                server_account_id,
                entity_id,
                str(payload.get("event_uid") or ""),
                int(payload.get("revision") or entity_revision),
                str(payload.get("batch_id") or ""),
                str(payload.get("status") or "running"),
                int(payload.get("step_index") or 0),
                payload.get("started_at"),
                payload.get("ended_at"),
                int(payload.get("elapsed_ms") or 0),
                int(payload.get("active_ms") or 0),
                int(payload.get("paused_ms") or 0),
                int(payload.get("retry_count") or 0),
            ),
        )

    @staticmethod
    def _purge_other_station_cache_conn(
        conn,
        current_server_account_id: Optional[str],
    ) -> None:
        if not current_server_account_id:
            return
        other_ids = [
            int(row["id"])
            for row in conn.execute(
                """
                SELECT id FROM accounts
                WHERE server_account_id IS NOT NULL AND server_account_id<>?
                """,
                (current_server_account_id,),
            ).fetchall()
        ]
        if other_ids:
            placeholders = ",".join("?" for _ in other_ids)
            conn.execute(
                f"""
                DELETE FROM activity_events
                WHERE remote_cached=1 AND user_id IN ({placeholders})
                """,
                other_ids,
            )
        conn.execute(
            "DELETE FROM remote_workflow_batches WHERE server_account_id<>?",
            (current_server_account_id,),
        )
        conn.execute(
            "DELETE FROM remote_workflow_runs WHERE server_account_id<>?",
            (current_server_account_id,),
        )

    @staticmethod
    def _apply_data_reset_conn(
        conn,
        server_account_id: str,
        reset_at: Optional[str],
    ) -> None:
        account = conn.execute(
            "SELECT id FROM accounts WHERE server_account_id=?",
            (server_account_id,),
        ).fetchone()
        if account:
            conn.execute(
                "DELETE FROM activity_events WHERE user_id=?",
                (int(account["id"]),),
            )
        conn.execute(
            "DELETE FROM remote_workflow_batches WHERE server_account_id=?",
            (server_account_id,),
        )
        conn.execute(
            "DELETE FROM remote_workflow_runs WHERE server_account_id=?",
            (server_account_id,),
        )
        if reset_at:
            conn.execute(
                """
                UPDATE sync_outbox
                SET status='quarantined',
                    last_error_code='data_reset_tombstone',
                    last_error_message='该数据早于管理员重置时间',
                    updated_at=?
                WHERE server_account_id=?
                  AND status IN ('pending', 'retry') AND occurred_at<=?
                """,
                (Database._now(), server_account_id, reset_at),
            )

    def apply_sync_changes(self, response: Dict) -> None:
        changes = response.get("changes") or []
        data_scope = str(
            response.get("data_scope") or response.get("stats_scope") or "own"
        )
        if data_scope not in {"own", "road", "all"}:
            data_scope = "own"
        stats_scope = "all" if data_scope == "all" else "own"
        entitlement_revision = int(response.get("entitlement_revision") or 0)
        with self._connect() as conn:
            state = conn.execute("SELECT * FROM sync_state WHERE id=1").fetchone()
            previous_scope = state["data_scope"] or state["stats_scope"]
            current_server_account_id = state["current_server_account_id"]
            max_revision = int(state["last_revision"])
            scope_snapshot_required = False
            for change in changes:
                revision = int(change.get("revision") or 0)
                max_revision = max(max_revision, revision)
                kind = str(change.get("kind") or "")
                entity_id = str(change.get("entity_id") or "")
                account_id = str(change.get("account_id") or "")
                payload = change.get("payload") or {}
                if kind == "account":
                    if str(change.get("operation") or "upsert") == "delete":
                        if entity_id == current_server_account_id:
                            raise DatabaseError("当前登录账号已被服务器永久删除")
                        self._purge_remote_account_cache_conn(conn, entity_id)
                    else:
                        merged = {"id": entity_id, **payload}
                        self._upsert_remote_account_conn(conn, merged)
                elif kind == "activity_event":
                    self._apply_remote_activity_conn(conn, payload)
                elif kind == "workflow_batch_snapshot":
                    self._apply_remote_batch_conn(
                        conn,
                        account_id,
                        entity_id,
                        int(change.get("entity_revision") or 1),
                        payload,
                    )
                elif kind == "workflow_run_snapshot":
                    self._apply_remote_run_conn(
                        conn,
                        account_id,
                        entity_id,
                        int(change.get("entity_revision") or 1),
                        payload,
                    )
                elif kind == "account_data_reset":
                    self._apply_data_reset_conn(
                        conn,
                        account_id or entity_id,
                        payload.get("reset_at"),
                    )
                elif kind == "road_membership_changed" and payload.get(
                    "refresh_scope"
                ):
                    scope_snapshot_required = True
            if not response.get("has_more"):
                max_revision = max(
                    max_revision,
                    int(response.get("latest_revision") or 0),
                )
            if previous_scope != "own" and data_scope == "own":
                self._purge_other_station_cache_conn(
                    conn,
                    current_server_account_id,
                )
            conn.execute(
                """
                UPDATE sync_state
                SET last_revision=?, stats_scope=?, data_scope=?, entitlement_revision=?,
                    needs_snapshot=CASE WHEN ? THEN 1 ELSE needs_snapshot END,
                    last_error=NULL, updated_at=?
                WHERE id=1
                """,
                (
                    max_revision,
                    stats_scope,
                    data_scope,
                    entitlement_revision,
                    int(scope_snapshot_required),
                    self._now(),
                ),
            )

    def apply_sync_snapshot(self, snapshot: Dict) -> None:
        data_scope = str(
            snapshot.get("data_scope") or snapshot.get("stats_scope") or "own"
        )
        if data_scope not in {"own", "road", "all"}:
            data_scope = "own"
        stats_scope = "all" if data_scope == "all" else "own"
        with self._connect() as conn:
            state = conn.execute("SELECT * FROM sync_state WHERE id=1").fetchone()
            current_server_account_id = state["current_server_account_id"]
            conn.execute("DELETE FROM activity_events WHERE remote_cached=1")
            conn.execute("DELETE FROM remote_workflow_batches")
            conn.execute("DELETE FROM remote_workflow_runs")
            snapshot_account_ids = set()
            for account in snapshot.get("accounts") or []:
                server_account_id = str(
                    account.get("id") or account.get("account_id") or ""
                ).strip()
                if server_account_id:
                    snapshot_account_ids.add(server_account_id)
                self._upsert_remote_account_conn(conn, account)
            for metric in snapshot.get("metrics") or []:
                conn.execute(
                    """
                    INSERT INTO metric_definitions(
                        metric_key, label, unit, sort_order, is_active
                    ) VALUES (?, ?, ?, ?, 1)
                    ON CONFLICT(metric_key) DO UPDATE SET
                        label=excluded.label,
                        unit=excluded.unit,
                        sort_order=excluded.sort_order,
                        is_active=1
                    """,
                    (
                        str(metric.get("metric_key") or ""),
                        str(metric.get("label") or metric.get("metric_key") or ""),
                        str(metric.get("unit") or "条"),
                        int(metric.get("sort_order") or 100),
                    ),
                )
            for event in snapshot.get("activity_events") or []:
                self._apply_remote_activity_conn(conn, event)
            for batch in snapshot.get("workflow_batches") or []:
                self._apply_remote_batch_conn(
                    conn,
                    str(batch.get("account_id") or ""),
                    str(batch.get("entity_id") or ""),
                    int(batch.get("revision") or 1),
                    batch,
                )
            for run in snapshot.get("workflow_runs") or []:
                self._apply_remote_run_conn(
                    conn,
                    str(run.get("account_id") or ""),
                    str(run.get("entity_id") or ""),
                    int(run.get("revision") or 1),
                    run,
                )
            if data_scope == "own":
                self._purge_other_station_cache_conn(
                    conn,
                    current_server_account_id,
                )
            else:
                stale_account_ids = [
                    str(row["server_account_id"])
                    for row in conn.execute(
                        """
                        SELECT server_account_id FROM accounts
                        WHERE server_account_id IS NOT NULL
                          AND server_account_id<>?
                        """,
                        (current_server_account_id or "",),
                    ).fetchall()
                    if str(row["server_account_id"]) not in snapshot_account_ids
                ]
                for server_account_id in stale_account_ids:
                    self._purge_remote_account_cache_conn(
                        conn,
                        server_account_id,
                    )
            conn.execute(
                """
                UPDATE sync_state
                SET last_revision=?, needs_snapshot=0, stats_scope=?, data_scope=?,
                    entitlement_revision=?, last_error=NULL, updated_at=?
                WHERE id=1
                """,
                (
                    int(snapshot.get("revision") or 0),
                    stats_scope,
                    data_scope,
                    int(snapshot.get("entitlement_revision") or 0),
                    self._now(),
                ),
            )

    def mark_sync_success(self, synced_at: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE sync_state
                SET last_sync_at=?, last_error=NULL, updated_at=?
                WHERE id=1
                """,
                (synced_at, self._now()),
            )

    def mark_sync_error(self, message: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE sync_state SET last_error=?, updated_at=? WHERE id=1
                """,
                (str(message)[:500], self._now()),
            )

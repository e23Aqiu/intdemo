import base64
import hashlib
import json
import os
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from integrated_client.config import APP_VERSION
from integrated_client.database import AuthenticationError, Database
from integrated_client.online.api import NetworkUnavailable
from integrated_client.online.config import OnlineConfig, OnlineConfigurationError
from integrated_client.online.secure import DpapiProtector, Protector
from integrated_client.online.session import OnlineSessionManager
from integrated_client.online.sync import SyncEngine
from integrated_client.online.update import (
    UpdateCancelled,
    UpdateClient,
    UpdateError,
    UpdateInfo,
    version_key,
)
from integrated_client.preferences import ClientPreferences, LoginCredentialStore


def _b64url(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _next_patch_version(value):
    major, minor, patch = (int(part) for part in value.split("."))
    return f"{major}.{minor}.{patch + 1}"


class MemoryProtector(Protector):
    def protect(self, value):
        return b"test:" + value[::-1]

    def unprotect(self, value):
        if not value.startswith(b"test:"):
            raise ValueError("invalid protected value")
        return value[5:][::-1]


class FakeApi:
    def __init__(self):
        self.online = True
        self.private_key = Ed25519PrivateKey.generate()
        self.password = "Online!234"
        self.account_id = str(uuid.uuid4())
        self.push_calls = []
        self.server_revision = 0

    def _entitlement(self, username, device_uid, expired=False):
        now = datetime.now(timezone.utc)
        expires = now - timedelta(seconds=1) if expired else now + timedelta(days=7)
        payload = {
            "v": 1,
            "iss": "test",
            "account_id": self.account_id,
            "device_id": str(uuid.uuid4()),
            "device_uid": device_uid,
            "username": username,
            "display_name": "在线测试站",
            "role": "user",
            "stats_scope": "own",
            "entitlement_revision": 1,
            "issued_at": int(now.timestamp()),
            "expires_at": int(expires.timestamp()),
        }
        body = _b64url(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        signature = _b64url(self.private_key.sign(body.encode("ascii")))
        public_key = self.private_key.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
        return f"{body}.{signature}", base64.b64encode(public_key).decode("ascii")

    def _bundle(self, username, device_uid, must_change=False, expired=False):
        now = datetime.now(timezone.utc)
        entitlement, public_key = self._entitlement(
            username,
            device_uid,
            expired=expired,
        )
        return {
            "token_type": "bearer",
            "access_token": "access-token",
            "access_expires_at": (now + timedelta(minutes=15)).isoformat(),
            "refresh_token": "refresh-token-value-that-is-long",
            "refresh_expires_at": (now + timedelta(days=30)).isoformat(),
            "offline_entitlement": entitlement,
            "offline_expires_at": (
                now + (-timedelta(seconds=1) if expired else timedelta(days=7))
            ).isoformat(),
            "offline_public_key": public_key,
            "account": {
                "id": self.account_id,
                "username": username,
                "display_name": "在线测试站",
                "role": "user",
                "stats_scope": "own",
                "device_limit": 1,
                "is_active": True,
                "is_archived": False,
                "must_change_password": must_change,
                "entitlement_revision": 1,
                "created_at": now.isoformat(),
                "updated_at": now.isoformat(),
            },
            "device": {
                "id": str(uuid.uuid4()),
                "device_uid": device_uid,
                "name": "test",
                "client_version": "0.2.0",
                "created_at": now.isoformat(),
                "last_seen_at": now.isoformat(),
                "revoked_at": None,
                "revoked_reason": None,
            },
            "server_revision": self.server_revision,
        }

    def login(self, username, password, device_uid, device_name, client_version):
        if not self.online:
            raise NetworkUnavailable("offline")
        if password != self.password:
            raise AssertionError("unexpected test password")
        return self._bundle(username, device_uid)

    def change_password(self, access_token, current_password, new_password):
        self.password = new_password
        return self._bundle("station", self.device_uid)

    def refresh(self, refresh_token, device_uid):
        if not self.online:
            raise NetworkUnavailable("offline")
        return self._bundle("station", device_uid)

    def logout(self, access_token, refresh_token):
        return None

    def push(self, access_token, items):
        if not self.online:
            raise NetworkUnavailable("offline")
        self.push_calls.append(items)
        results = []
        for item in items:
            self.server_revision += 1
            results.append(
                {
                    "event_uid": item["event_uid"],
                    "status": "accepted",
                    "server_revision": self.server_revision,
                    "retryable": False,
                }
            )
        return {"items": results, "latest_revision": self.server_revision}

    def snapshot(self, access_token):
        return {
            "revision": self.server_revision,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "accounts": [],
            "metrics": [],
            "activity_events": [],
            "workflow_batches": [],
            "workflow_runs": [],
            "entitlement_revision": 1,
            "stats_scope": "own",
        }

    def pull(self, access_token, after_revision, limit=500):
        return {
            "changes": [],
            "latest_revision": self.server_revision,
            "has_more": False,
            "entitlement_revision": 1,
            "stats_scope": "own",
        }


class OnlineClientTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "online.db")
        self.api = FakeApi()
        self.protector = MemoryProtector()
        self.session = OnlineSessionManager(
            self.database,
            self.api,
            protector=self.protector,
        )
        self.api.device_uid = self.session.device_uid

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_online_login_then_password_checked_offline_login(self):
        account = self.session.login("station", "Online!234")
        self.assertEqual(account.server_account_id, self.api.account_id)
        self.assertTrue(self.session.state.is_online)
        encrypted = self.database.load_secure_online_profile()
        self.assertNotIn(b"Online!234", encrypted)

        self.api.online = False
        offline_session = OnlineSessionManager(
            self.database,
            self.api,
            protector=self.protector,
        )
        offline_account = offline_session.login("station", "Online!234")
        self.assertEqual(offline_account.id, account.id)
        self.assertFalse(offline_session.state.is_online)
        with self.assertRaises(AuthenticationError):
            offline_session.login("station", "wrong-password")
        self.api.online = True
        self.assertEqual(offline_session.access_token(), "access-token")
        self.assertTrue(offline_session.state.is_online)

    def test_first_login_cannot_start_offline(self):
        self.api.online = False
        with self.assertRaisesRegex(AuthenticationError, "首次登录"):
            self.session.login("station", "Online!234")

    def test_explicit_offline_login_never_contacts_server(self):
        account = self.session.login("station", "Online!234")
        offline_session = OnlineSessionManager(
            self.database,
            self.api,
            protector=self.protector,
        )
        original_login = self.api.login
        self.api.login = Mock(side_effect=AssertionError("network was used"))
        try:
            offline_account = offline_session.offline_login(
                "station",
                "Online!234",
            )
        finally:
            self.api.login = original_login

        self.assertEqual(offline_account.id, account.id)
        self.assertEqual(offline_session.state.mode, "offline_untracked")
        with self.assertRaisesRegex(NetworkUnavailable, "手动离线"):
            offline_session.access_token()
        encrypted = self.database.load_secure_online_profile()
        offline_session.end_offline_session()
        self.assertIsNone(offline_session.state)
        self.assertEqual(self.database.load_secure_online_profile(), encrypted)

    def test_online_config_requires_https_and_private_ca_for_ip(self):
        with self.assertRaises(OnlineConfigurationError):
            OnlineConfig(base_url="http://api.example.com").validate()
        with self.assertRaisesRegex(OnlineConfigurationError, "私有 CA"):
            OnlineConfig(base_url="https://203.0.113.10").validate()
        config = OnlineConfig(
            base_url="https://203.0.113.10",
            ca_bundle=str(self.database.path),
        ).validate()
        self.assertEqual(config.ca_bundle, str(self.database.path))

    def test_update_manifest_accepts_only_newer_same_server_package(self):
        installer = b"signed-by-manifest-hash"
        target_version = _next_patch_version(APP_VERSION)
        installer_name = f"IntDemoOnline-Setup-{target_version}.exe"
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "schema_version": 1,
            "channel": "test",
            "version": target_version,
            "installer_path": f"/updates/files/{installer_name}",
            "sha256": hashlib.sha256(installer).hexdigest(),
            "size": len(installer),
            "notes": "修复启动问题",
            "mandatory": True,
        }
        session = Mock()
        session.headers = {}
        session.get.return_value = response
        config = OnlineConfig(
            base_url="https://203.0.113.10",
            ca_bundle=str(self.database.path),
        )

        update = UpdateClient(config, session=session).check()

        self.assertEqual(update.version, target_version)
        self.assertEqual(
            update.installer_url,
            f"https://203.0.113.10/updates/files/{installer_name}",
        )
        self.assertGreater(version_key(update.version), version_key(APP_VERSION))
        self.assertTrue(update.mandatory)
        session.get.assert_called_once()

        response.json.return_value["installer_path"] = "https://evil.example/x.exe"
        with self.assertRaisesRegex(UpdateError, "路径无效"):
            UpdateClient(config, session=session).check()

    def test_update_manifest_prefers_matching_delta(self):
        full = b"full"
        delta = b"delta"
        target_version = _next_patch_version(APP_VERSION)
        full_name = f"IntDemoOnline-Setup-{target_version}.exe"
        delta_name = (
            f"IntDemoOnline-Patch-{APP_VERSION}-to-{target_version}.exe"
        )
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "schema_version": 1,
            "channel": "test",
            "version": target_version,
            "installer_path": f"/updates/files/{delta_name}",
            "sha256": hashlib.sha256(delta).hexdigest(),
            "size": len(delta),
            "notes": "增量更新测试",
            "primary_kind": "delta",
            "primary_from_version": APP_VERSION,
            "full": {
                "installer_path": f"/updates/files/{full_name}",
                "sha256": hashlib.sha256(full).hexdigest(),
                "size": len(full),
            },
            "deltas": [
                {
                    "from_version": APP_VERSION,
                    "installer_path": f"/updates/files/{delta_name}",
                    "sha256": hashlib.sha256(delta).hexdigest(),
                    "size": len(delta),
                }
            ],
        }
        session = Mock()
        session.headers = {}
        session.get.return_value = response
        config = OnlineConfig(
            base_url="https://203.0.113.10",
            ca_bundle=str(self.database.path),
        )

        update = UpdateClient(config, session=session).check()

        self.assertTrue(update.is_delta)
        self.assertEqual(update.from_version, APP_VERSION)
        self.assertEqual(update.size, len(delta))
        self.assertEqual(update.installer_name, delta_name)

    def test_paused_update_distribution_is_treated_as_no_update(self):
        response = Mock(status_code=200)
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "paused": True,
            "paused_version": _next_patch_version(APP_VERSION),
        }
        session = Mock()
        session.headers = {}
        session.get.return_value = response
        config = OnlineConfig(
            base_url="https://203.0.113.10",
            ca_bundle=str(self.database.path),
        )

        self.assertIsNone(UpdateClient(config, session=session).check())

        no_content = Mock(status_code=204)
        no_content.raise_for_status.return_value = None
        session.get.return_value = no_content

        self.assertIsNone(UpdateClient(config, session=session).check())
        no_content.json.assert_not_called()

    def test_update_manifest_uses_delta_only_for_exact_current_version(self):
        full = b"full-package"
        delta = b"delta-package"
        manifest = {
            "schema_version": 1,
            "channel": "test",
            "version": "0.2.6",
            "installer_path": "/updates/files/IntDemoOnline-Setup-0.2.6.exe",
            "sha256": hashlib.sha256(full).hexdigest(),
            "size": len(full),
            "full": {
                "installer_path": "/updates/files/IntDemoOnline-Setup-0.2.6.exe",
                "sha256": hashlib.sha256(full).hexdigest(),
                "size": len(full),
            },
            "deltas": [
                {
                    "from_version": "0.2.4",
                    "installer_path": (
                        "/updates/files/IntDemoOnline-Patch-0.2.4-to-0.2.6.exe"
                    ),
                    "sha256": hashlib.sha256(delta).hexdigest(),
                    "size": len(delta),
                }
            ],
        }
        config = OnlineConfig(
            base_url="https://203.0.113.10",
            ca_bundle=str(self.database.path),
        )

        for current_version, expected_kind, expected_name in (
            ("0.2.3", "full", "IntDemoOnline-Setup-0.2.6.exe"),
            ("0.2.4", "delta", "IntDemoOnline-Patch-0.2.4-to-0.2.6.exe"),
            ("0.2.5", "full", "IntDemoOnline-Setup-0.2.6.exe"),
        ):
            with self.subTest(current_version=current_version):
                response = Mock()
                response.raise_for_status.return_value = None
                response.json.return_value = manifest
                session = Mock()
                session.headers = {}
                session.get.return_value = response

                update = UpdateClient(
                    config,
                    session=session,
                    current_version=current_version,
                ).check()

                self.assertEqual(update.package_kind, expected_kind)
                self.assertEqual(update.installer_name, expected_name)
                self.assertEqual(
                    session.headers["User-Agent"],
                    f"IntDemoUpdater/{current_version}",
                )
                self.assertEqual(
                    session.headers["X-IntDemo-Version"],
                    current_version,
                )

    def test_update_download_verifies_size_and_sha256(self):
        content = b"verified-installer-content"

        class DownloadResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            @staticmethod
            def raise_for_status():
                return None

            @staticmethod
            def iter_content(chunk_size):
                self.assertGreater(chunk_size, 0)
                yield content

        session = Mock()
        session.headers = {}
        session.get.return_value = DownloadResponse()
        config = OnlineConfig(
            base_url="https://203.0.113.10",
            ca_bundle=str(self.database.path),
        )
        update = UpdateInfo(
            version="0.2.4",
            installer_url=(
                "https://203.0.113.10/updates/files/"
                "IntDemoOnline-Setup-0.2.4.exe"
            ),
            installer_name="IntDemoOnline-Setup-0.2.4.exe",
            sha256=hashlib.sha256(content).hexdigest(),
            size=len(content),
            notes="",
        )
        previous = os.environ.get("INTDEMO_DATA_DIR")
        os.environ["INTDEMO_DATA_DIR"] = self.temp_dir.name
        progress = []
        try:
            path = UpdateClient(config, session=session).download(
                update,
                progress_callback=lambda received, total: progress.append(
                    (received, total)
                ),
            )
        finally:
            if previous is None:
                os.environ.pop("INTDEMO_DATA_DIR", None)
            else:
                os.environ["INTDEMO_DATA_DIR"] = previous
        self.assertEqual(path.read_bytes(), content)
        self.assertEqual(progress[0], (0, len(content)))
        self.assertEqual(progress[-1], (len(content), len(content)))

    def test_update_download_can_be_cancelled_and_removes_partial_file(self):
        content = b"x" * (512 * 1024)

        class DownloadResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            @staticmethod
            def raise_for_status():
                return None

            @staticmethod
            def iter_content(chunk_size):
                yield content[:chunk_size]
                yield content[chunk_size:]

        session = Mock()
        session.headers = {}
        session.get.return_value = DownloadResponse()
        config = OnlineConfig(
            base_url="https://203.0.113.10",
            ca_bundle=str(self.database.path),
        )
        update = UpdateInfo(
            version="0.2.6",
            installer_url="https://203.0.113.10/updates/files/update.exe",
            installer_name="cancel-update.exe",
            sha256=hashlib.sha256(content).hexdigest(),
            size=len(content),
            notes="",
        )
        previous = os.environ.get("INTDEMO_DATA_DIR")
        os.environ["INTDEMO_DATA_DIR"] = self.temp_dir.name
        cancelled = [False]
        try:
            with self.assertRaises(UpdateCancelled):
                UpdateClient(config, session=session).download(
                    update,
                    progress_callback=lambda received, _total: (
                        cancelled.__setitem__(0, True)
                        if received
                        else None
                    ),
                    cancelled_callback=lambda: cancelled[0],
                )
            update_dir = Path(self.temp_dir.name) / "updates"
            self.assertFalse((update_dir / "cancel-update.exe").exists())
            self.assertFalse((update_dir / "cancel-update.exe.part").exists())
        finally:
            if previous is None:
                os.environ.pop("INTDEMO_DATA_DIR", None)
            else:
                os.environ["INTDEMO_DATA_DIR"] = previous

    def test_remembered_credentials_are_encrypted_and_can_disable_auto_login(self):
        store = LoginCredentialStore(
            self.temp_dir.name,
            protector=self.protector,
        )
        store.save("Station", "Secret!234", auto_login=True)

        encrypted = store.path.read_bytes()
        self.assertNotIn(b"Station", encrypted)
        self.assertNotIn(b"Secret!234", encrypted)
        remembered = store.load()
        self.assertEqual(remembered.username, "station")
        self.assertEqual(remembered.password, "Secret!234")
        self.assertTrue(remembered.auto_login)

        store.disable_auto_login()
        self.assertFalse(store.load().auto_login)
        store.update_password("station", "Changed!234")
        self.assertEqual(store.load().password, "Changed!234")
        store.clear()
        self.assertIsNone(store.load())

    def test_ignored_update_version_is_persisted_per_version(self):
        preferences = ClientPreferences(self.temp_dir.name)
        self.assertEqual(preferences.ignored_update_version, "")
        preferences.ignore_update("0.2.3")
        self.assertEqual(
            ClientPreferences(self.temp_dir.name).ignored_update_version,
            "0.2.3",
        )
        preferences.clear_ignored_update()
        self.assertEqual(preferences.ignored_update_version, "")

    def test_expired_offline_entitlement_blocks_login(self):
        expired_bundle = self.api._bundle(
            "station",
            self.session.device_uid,
            expired=True,
        )
        self.session._save_bundle(expired_bundle, "Online!234")
        self.api.online = False
        offline_session = OnlineSessionManager(
            self.database,
            self.api,
            protector=self.protector,
        )
        with self.assertRaisesRegex(AuthenticationError, "超过 7 天"):
            offline_session.login("station", "Online!234")

    def test_account_switch_and_scope_expansion_require_fresh_snapshot(self):
        self.session.login("station", "Online!234")
        self.database.apply_sync_snapshot(
            {
                "revision": 50,
                "accounts": [],
                "metrics": [],
                "activity_events": [],
                "workflow_batches": [],
                "workflow_runs": [],
                "entitlement_revision": 1,
                "stats_scope": "own",
            }
        )
        self.assertEqual(self.database.get_sync_state()["last_revision"], 50)

        self.database.set_current_online_account(self.api.account_id, "all")
        expanded = self.database.get_sync_state()
        self.assertEqual(expanded["last_revision"], 0)
        self.assertTrue(expanded["needs_snapshot"])

        self.database.apply_sync_snapshot(
            {
                "revision": 75,
                "accounts": [],
                "metrics": [],
                "activity_events": [],
                "workflow_batches": [],
                "workflow_runs": [],
                "entitlement_revision": 2,
                "stats_scope": "all",
            }
        )
        self.database.set_current_online_account(str(uuid.uuid4()), "own")
        switched = self.database.get_sync_state()
        self.assertEqual(switched["last_revision"], 0)
        self.assertTrue(switched["needs_snapshot"])

    def test_activity_and_outbox_share_transaction_and_strip_local_fields(self):
        account = self.session.login("station", "Online!234")
        self.database.record_activity_batch(
            account.id,
            {"workflow_detail_total": 3},
            source="unified_workflow",
            details={
                "file_name": "绝不能上传.xlsx",
                "row_count": 3,
                "violation_counts": {
                    "证件异常": {"total": 2, "has_phone": 1, "other": 1}
                },
            },
            task_id="run-safe-id",
        )
        due = self.database.get_due_sync_items()
        self.assertEqual(len(due), 1)
        encoded = json.dumps(due[0]["item"], ensure_ascii=False)
        self.assertNotIn("绝不能上传", encoded)
        self.assertNotIn("file_name", encoded)
        self.assertEqual(
            due[0]["item"]["payload"]["summary"]["violation_counts"]["证件异常"][
                "total"
            ],
            2,
        )

        original = self.database._enqueue_sync_item

        def fail_enqueue(*args, **kwargs):
            raise RuntimeError("outbox failure")

        self.database._enqueue_sync_item = fail_enqueue
        with self.assertRaisesRegex(RuntimeError, "outbox failure"):
            self.database.record_activity(
                account.id,
                "future_metric",
                1,
            )
        self.database._enqueue_sync_item = original
        totals = {
            row["metric_key"]: row["total"]
            for row in self.database.get_user_totals(account.id)
        }
        self.assertEqual(totals.get("future_metric", 0), 0)

    def test_sync_engine_pushes_once_and_updates_status(self):
        account = self.session.login("station", "Online!234")
        self.database.record_activity(
            account.id,
            "workflow_detail_total",
            1,
            source="unified_workflow",
        )
        engine = SyncEngine(self.database, self.session)
        status = engine.run_once()
        self.assertEqual(status.state, "online")
        self.assertEqual(status.pending_count, 0)
        self.assertEqual(len(self.api.push_calls), 1)
        engine.run_once()
        self.assertEqual(len(self.api.push_calls), 1)

    def test_sync_receipt_matches_hyphenated_server_uuid(self):
        account = self.session.login("station", "Online!234")
        self.database.record_activity(
            account.id,
            "workflow_detail_total",
            1,
            source="unified_workflow",
        )
        local_uid = self.database.get_due_sync_items()[0]["event_uid"]
        self.assertNotIn("-", local_uid)

        def canonical_uuid_response(_access_token, items):
            return {
                "items": [
                    {
                        "event_uid": str(uuid.UUID(items[0]["event_uid"])),
                        "status": "duplicate",
                        "server_revision": 42,
                        "retryable": False,
                    }
                ],
                "latest_revision": 42,
            }

        self.api.push = canonical_uuid_response
        status = SyncEngine(self.database, self.session).run_once()
        self.assertEqual(status.state, "online")
        self.assertEqual(status.pending_count, 0)

    def test_manual_retry_can_make_delayed_items_due(self):
        account = self.session.login("station", "Online!234")
        self.database.record_activity(
            account.id,
            "workflow_detail_total",
            1,
            source="unified_workflow",
        )
        row = self.database.get_due_sync_items()[0]
        self.database.schedule_sync_retry(
            row["id"],
            datetime.now().timestamp() + 300,
            "network_unavailable",
            "offline",
        )
        self.assertEqual(self.database.get_due_sync_items(), [])
        changed = self.database.make_sync_retries_due(
            account.server_account_id
        )
        self.assertEqual(changed, 1)
        self.assertEqual(len(self.database.get_due_sync_items()), 1)

    def test_network_failure_schedules_exponential_retry(self):
        account = self.session.login("station", "Online!234")
        self.database.record_activity(
            account.id,
            "workflow_detail_total",
            1,
            source="unified_workflow",
        )
        self.api.online = False
        status = SyncEngine(self.database, self.session).run_once()
        self.assertEqual(status.state, "offline")
        self.assertEqual(self.database.get_due_sync_items(), [])
        with self.database._connect() as connection:
            row = connection.execute(
                """
                SELECT status, attempt_count, next_attempt_at
                FROM sync_outbox WHERE status='retry'
                """
            ).fetchone()
        self.assertEqual(row["attempt_count"], 1)
        self.assertGreater(row["next_attempt_at"], datetime.now().timestamp())

    def test_permanently_rejected_item_moves_to_quarantine(self):
        account = self.session.login("station", "Online!234")
        self.database.record_activity(
            account.id,
            "workflow_detail_total",
            1,
            source="unified_workflow",
        )

        def reject(access_token, items):
            return {
                "items": [
                    {
                        "event_uid": items[0]["event_uid"],
                        "status": "rejected",
                        "code": "invalid_sync_payload",
                        "message": "invalid test item",
                        "retryable": False,
                    }
                ],
                "latest_revision": 0,
            }

        self.api.push = reject
        status = SyncEngine(self.database, self.session).run_once()
        self.assertEqual(status.state, "online")
        self.assertEqual(status.pending_count, 0)
        self.assertEqual(status.quarantined_count, 1)
        quarantined = self.database.get_sync_quarantined_items()
        self.assertEqual(quarantined[0]["last_error_code"], "invalid_sync_payload")

    def test_workflow_outbox_contains_only_summaries_and_fingerprint(self):
        account = self.session.login("station", "Online!234")
        run_id = uuid.uuid4().hex
        source_signature = hashlib.sha256(b"input-rows").hexdigest()
        started = self.database.start_workflow_run(
            account.id,
            r"C:\private\车辆明细.xlsx",
            source_signature,
            run_id,
            uuid.uuid4().hex,
            os.getpid(),
        )
        self.database.finish_workflow_run(
            run_id,
            "succeeded",
            active_ms=1250,
            paused_ms=250,
        )
        self.database.record_activity_batch(
            account.id,
            {"workflow_detail_total": 2},
            source="unified_workflow",
            details={"file_name": "车辆明细.xlsx", "row_count": 2},
            task_id=run_id,
        )
        items = [row["item"] for row in self.database.get_due_sync_items()]
        self.assertEqual(
            {item["kind"] for item in items},
            {
                "activity_event",
                "workflow_batch_snapshot",
                "workflow_run_snapshot",
            },
        )
        encoded = json.dumps(items, ensure_ascii=False)
        self.assertNotIn("车辆明细", encoded)
        self.assertNotIn(r"C:\private", encoded)
        batch_items = [
            item for item in items if item["kind"] == "workflow_batch_snapshot"
        ]
        self.assertTrue(
            all(
                item["payload"]["input_fingerprint"] == source_signature
                for item in batch_items
            )
        )
        self.assertEqual(started["run_id"], run_id)

    def test_permission_shrink_purges_other_station_cache(self):
        own = self.session.login("station", "Online!234")
        other_id = str(uuid.uuid4())
        snapshot = {
            "revision": 10,
            "accounts": [
                {
                    "id": self.api.account_id,
                    "username": "station",
                    "display_name": "本站",
                    "role": "user",
                    "stats_scope": "all",
                    "is_active": True,
                    "is_archived": False,
                    "entitlement_revision": 1,
                },
                {
                    "id": other_id,
                    "username": "other",
                    "display_name": "其他站",
                    "role": "user",
                    "stats_scope": "own",
                    "is_active": True,
                    "is_archived": False,
                    "entitlement_revision": 1,
                },
            ],
            "metrics": [],
            "activity_events": [
                {
                    "account_id": other_id,
                    "event_uid": str(uuid.uuid4()),
                    "metric_key": "workflow_detail_total",
                    "amount": 5,
                    "business_date": "2026-07-27",
                    "source": "unified_workflow",
                    "task_id": None,
                    "summary": {},
                    "occurred_at": datetime.now(timezone.utc).isoformat(),
                }
            ],
            "workflow_batches": [],
            "workflow_runs": [],
            "entitlement_revision": 1,
            "stats_scope": "all",
        }
        self.database.apply_sync_snapshot(snapshot)
        other = next(
            account
            for account in self.database.list_accounts()
            if account.server_account_id == other_id
        )
        self.assertEqual(
            sum(row["total"] for row in self.database.get_user_totals(other.id)),
            5,
        )
        self.database.apply_sync_changes(
            {
                "changes": [],
                "latest_revision": 11,
                "has_more": False,
                "entitlement_revision": 2,
                "stats_scope": "own",
            }
        )
        self.assertEqual(
            sum(row["total"] for row in self.database.get_user_totals(other.id)),
            0,
        )
        self.assertEqual(own.id, self.session.state.account.id)

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI test")
    def test_dpapi_round_trip_and_ciphertext(self):
        protector = DpapiProtector()
        plaintext = b"refresh-token-and-offline-entitlement"
        ciphertext = protector.protect(plaintext)
        self.assertNotEqual(ciphertext, plaintext)
        self.assertEqual(protector.unprotect(ciphertext), plaintext)


if __name__ == "__main__":
    unittest.main()

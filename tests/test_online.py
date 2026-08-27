import base64
import hashlib
import json
import os
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from integrated_client.config import APP_VERSION
from integrated_client.database import AuthenticationError, Database, DatabaseError
from integrated_client.online.api import ApiResponseError, NetworkUnavailable
from integrated_client.online.config import OnlineConfig, OnlineConfigurationError
from integrated_client.online.coordinator import load_websocket_ca_certificates
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
from integrated_client.platform_support import (
    WINDOWS_UPDATE_PLATFORM,
    login_system_label,
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
        self.last_login_at = "2026-08-19T00:32:47+00:00"
        self.last_login_system = None
        self.refresh_calls = 0
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
                "last_login_at": self.last_login_at,
                "last_login_system": self.last_login_system,
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

    def login(
        self,
        username,
        password,
        device_uid,
        device_name,
        client_version,
        login_system=None,
    ):
        if not self.online:
            raise NetworkUnavailable("offline")
        if password != self.password:
            raise AssertionError("unexpected test password")
        self.last_login_system = login_system
        return self._bundle(username, device_uid)

    def change_password(self, access_token, current_password, new_password):
        self.password = new_password
        return self._bundle("station", self.device_uid)

    def refresh(self, refresh_token, device_uid):
        if not self.online:
            raise NetworkUnavailable("offline")
        self.refresh_calls += 1
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
        self.assertEqual(account.last_login, self.api.last_login_at)
        self.assertEqual(self.api.last_login_system, login_system_label())
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

    def test_concurrent_access_token_refresh_uses_rotating_token_once(self):
        self.session.login("station", "Online!234")
        self.session._bundle["access_expires_at"] = (
            datetime.now(timezone.utc) + timedelta(seconds=1)
        ).isoformat()
        original_refresh = self.api.refresh

        def slow_refresh(*args, **kwargs):
            import time

            time.sleep(0.05)
            return original_refresh(*args, **kwargs)

        self.api.refresh = slow_refresh
        with ThreadPoolExecutor(max_workers=2) as pool:
            tokens = list(pool.map(lambda _index: self.session.access_token(), range(2)))

        self.assertEqual(tokens, ["access-token", "access-token"])
        self.assertEqual(self.api.refresh_calls, 1)

    def test_remote_account_without_last_login_field_preserves_cached_value(self):
        account = self.session.login("station", "Online!234")
        payload = self.api._bundle("station", self.session.device_uid)["account"]
        payload.pop("last_login_at")

        refreshed = self.database.upsert_remote_account(payload)

        self.assertEqual(refreshed.id, account.id)
        self.assertEqual(refreshed.last_login, self.api.last_login_at)

    def test_road_manager_fields_and_scope_are_cached_additively(self):
        road_id = str(uuid.uuid4())
        account = self.database.upsert_remote_account(
            {
                "id": str(uuid.uuid4()),
                "username": "road_manager",
                "display_name": "广深高速",
                # Legacy values remain safe for v1.1.x SQLite constraints.
                "role": "user",
                "stats_scope": "own",
                "account_type": "road_admin",
                "data_scope": "road",
                "road_id": road_id,
                "road_name": "广深高速",
                "is_active": True,
                "is_archived": False,
                "entitlement_revision": 1,
            }
        )

        self.assertEqual(account.role, "user")
        self.assertEqual(account.stats_scope, "own")
        self.assertTrue(account.is_road_admin)
        self.assertTrue(account.is_account_manager)
        self.assertFalse(account.is_admin)
        self.assertFalse(account.statistics_enabled)
        self.assertFalse(self.database.account_statistics_enabled(account.id))
        self.assertEqual(account.role_label, "路段管理员")
        self.assertEqual(account.effective_data_scope, "road")
        self.assertTrue(account.can_view_shared_stats)
        self.assertFalse(account.can_view_all_stats)
        self.assertEqual(account.road_id, road_id)
        self.assertEqual(account.road_name, "广深高速")

        self.database.set_current_online_account(
            account.server_account_id,
            account.stats_scope,
            account.effective_data_scope,
        )
        state = self.database.get_sync_state()
        self.assertEqual(state["stats_scope"], "own")
        self.assertEqual(state["data_scope"], "road")
        self.assertTrue(state["needs_snapshot"])

    def test_road_manager_cannot_record_and_cached_history_is_not_counted(self):
        account_id = str(uuid.uuid4())
        road_id = str(uuid.uuid4())
        payload = {
            "id": account_id,
            "username": "promoted_manager",
            "display_name": "晋升前中心站",
            "role": "user",
            "stats_scope": "own",
            "account_type": "station",
            "data_scope": "own",
            "road_id": road_id,
            "road_name": "晋升测试路段",
            "is_active": True,
            "is_archived": False,
            "entitlement_revision": 1,
        }
        station = self.database.upsert_remote_account(payload)
        self.database.start_workflow_run(
            station.id,
            "历史数据.xlsx",
            "history-signature",
            "history-run",
            "history-process-session",
            os.getpid(),
        )
        self.database.finish_workflow_run(
            "history-run",
            "succeeded",
            active_ms=1000,
            paused_ms=200,
        )
        self.database.record_activity(
            station.id,
            "workflow_detail_total",
            7,
            source="unified_workflow",
            details={
                "violation_counts": {
                    "历史违规": {"total": 7, "has_phone": 3, "other": 4}
                }
            },
            task_id="history-run",
        )
        self.assertEqual(
            next(
                row["total"]
                for row in self.database.get_user_totals(station.id)
                if row["metric_key"] == "workflow_detail_total"
            ),
            7,
        )
        self.assertEqual(
            self.database.get_workflow_timing_totals(station.id)["completed_items"],
            7,
        )

        payload.update(
            {
                "display_name": "晋升测试路段",
                "account_type": "road_admin",
                "data_scope": "road",
                "entitlement_revision": 2,
            }
        )
        manager = self.database.upsert_remote_account(payload)
        self.database.record_activity(
            manager.id,
            "workflow_detail_total",
            9,
            source="unified_workflow",
        )

        self.assertFalse(manager.statistics_enabled)
        self.assertFalse(self.database.account_statistics_enabled(manager.id))
        self.assertTrue(
            all(row["total"] == 0 for row in self.database.get_user_totals(manager.id))
        )
        self.assertNotIn(
            manager.username,
            {row["username"] for row in self.database.get_all_account_totals()},
        )
        self.assertEqual(
            self.database.get_daily_metric_totals(
                "workflow_detail_total",
                user_id=manager.id,
            ),
            [],
        )
        self.assertEqual(
            self.database.get_violation_totals(user_id=manager.id),
            [],
        )
        self.assertEqual(self.database.get_recent_activity(manager.id), [])
        self.assertEqual(
            self.database.get_workflow_timing_totals(manager.id),
            {
                "active_ms": 0,
                "paused_ms": 0,
                "total_ms": 0,
                "run_count": 0,
                "completed_items": 0,
            },
        )
        with self.assertRaisesRegex(DatabaseError, "路段管理员账号不记录业务计时"):
            self.database.start_workflow_run(
                manager.id,
                "管理账号.xlsx",
                "manager-signature",
                "manager-run",
                "manager-process-session",
                os.getpid(),
            )
        with self.database._connect() as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM activity_events WHERE user_id=?",
                    (manager.id,),
                ).fetchone()[0],
                1,
            )
    def test_road_membership_change_requests_a_fresh_scope_snapshot(self):
        road_id = str(uuid.uuid4())
        manager_id = str(uuid.uuid4())
        account_payload = {
            "id": manager_id,
            "username": "snapshot_manager",
            "display_name": "广深高速",
            "role": "user",
            "stats_scope": "own",
            "account_type": "road_admin",
            "data_scope": "road",
            "road_id": road_id,
            "road_name": "广深高速",
            "is_active": True,
            "is_archived": False,
            "entitlement_revision": 1,
        }
        self.database.upsert_remote_account(account_payload)
        self.database.set_current_online_account(manager_id, "own", "road")
        self.database.apply_sync_snapshot(
            {
                "revision": 5,
                "accounts": [account_payload],
                "metrics": [],
                "activity_events": [],
                "workflow_batches": [],
                "workflow_runs": [],
                "entitlement_revision": 1,
                "stats_scope": "own",
                "data_scope": "road",
            }
        )
        self.assertFalse(self.database.get_sync_state()["needs_snapshot"])

        self.database.apply_sync_changes(
            {
                "changes": [
                    {
                        "revision": 6,
                        "account_id": None,
                        "kind": "road_membership_changed",
                        "entity_id": str(uuid.uuid4()),
                        "entity_revision": 1,
                        "operation": "upsert",
                        "payload": {"refresh_scope": True},
                        "occurred_at": datetime.now(timezone.utc).isoformat(),
                    }
                ],
                "latest_revision": 6,
                "has_more": False,
                "entitlement_revision": 1,
                "stats_scope": "own",
                "data_scope": "road",
            }
        )
        self.assertTrue(self.database.get_sync_state()["needs_snapshot"])

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

    def test_websocket_ca_loads_from_unicode_windows_path(self):
        source = (
            Path(__file__).resolve().parents[1]
            / "packaging"
            / "uos-arm64"
            / "certs"
            / "intdemo-caddy-root.crt"
        )
        target = Path(self.temp_dir.name) / "中文证书目录" / "根证书.crt"
        target.parent.mkdir()
        target.write_bytes(source.read_bytes())

        certificates = load_websocket_ca_certificates(target)

        self.assertTrue(certificates)
        self.assertTrue(all(not certificate.isNull() for certificate in certificates))

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

        update = UpdateClient(
            config,
            session=session,
            platform_key=WINDOWS_UPDATE_PLATFORM,
        ).check()

        self.assertEqual(update.version, target_version)
        self.assertEqual(
            update.installer_url,
            f"https://203.0.113.10/updates/files/{installer_name}",
        )
        self.assertGreater(version_key(update.version), version_key(APP_VERSION))
        self.assertTrue(update.mandatory)
        self.assertEqual(
            session.headers["X-IntDemo-Platform"],
            "windows-x86_64",
        )
        session.get.assert_called_once()

        response.json.return_value["installer_path"] = "https://evil.example/x.exe"
        with self.assertRaisesRegex(UpdateError, "路径无效"):
            UpdateClient(
                config,
                session=session,
                platform_key=WINDOWS_UPDATE_PLATFORM,
            ).check()

    def test_update_manifest_selects_uos_arm64_deb(self):
        target_version = _next_patch_version(APP_VERSION)
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "schema_version": 1,
            "channel": "test",
            "version": target_version,
            "notes": "双端更新测试",
            "platforms": {
                "windows-x86_64": {
                    "full": {
                        "installer_path": (
                            f"/updates/files/IntDemoOnline-Setup-{target_version}.exe"
                        ),
                        "sha256": "a" * 64,
                        "size": 123,
                    }
                },
                "linux-aarch64": {
                    "full": {
                        "installer_path": (
                            f"/updates/files/IntDemo-UOS-arm64-{target_version}.deb"
                        ),
                        "sha256": "b" * 64,
                        "size": 456,
                    }
                },
            },
        }
        session = Mock()
        session.headers = {}
        session.get.return_value = response
        config = OnlineConfig(
            base_url="https://203.0.113.10",
            ca_bundle=str(self.database.path),
        )

        update = UpdateClient(
            config,
            session=session,
            platform_key="linux-aarch64",
        ).check()

        self.assertEqual(update.platform_key, "linux-aarch64")
        self.assertTrue(update.installer_name.endswith(".deb"))
        self.assertEqual(update.size, 456)
        self.assertEqual(
            session.headers["X-IntDemo-Platform"],
            "linux-aarch64",
        )
        self.assertEqual(
            session.headers["X-IntDemo-Update-Capabilities"],
            "uos-file-update-v2, uos-layered-v1, uos-deb-xdelta-v1",
        )

        response.json.return_value["platforms"]["linux-aarch64"]["full"][
            "installer_path"
        ] = f"/updates/files/IntDemoOnline-Setup-{target_version}.exe"
        with self.assertRaisesRegex(UpdateError, "当前平台不匹配"):
            UpdateClient(
                config,
                session=session,
                platform_key="linux-aarch64",
            ).check()

    def test_uos_manifest_uses_delta_only_with_verified_local_base(self):
        target_version = _next_patch_version(APP_VERSION)
        base = b"released-uos-base-deb"
        target = b"target-uos-deb"
        patch_content = b"xdelta-patch"
        full_name = f"IntDemo-UOS-arm64-{target_version}.deb"
        patch_name = (
            f"IntDemo-UOS-arm64-Patch-{APP_VERSION}-to-"
            f"{target_version}.intdelta"
        )
        manifest = {
            "schema_version": 1,
            "channel": "test",
            "version": target_version,
            "notes": "UOS 增量更新测试",
            "platforms": {
                "linux-aarch64": {
                    "full": {
                        "installer_path": f"/updates/files/{full_name}",
                        "sha256": hashlib.sha256(target).hexdigest(),
                        "size": len(target),
                    },
                    "deltas": [
                        {
                            "format": "uos-deb-xdelta-v1",
                            "algorithm": "xdelta3",
                            "from_version": APP_VERSION,
                            "base_sha256": hashlib.sha256(base).hexdigest(),
                            "base_size": len(base),
                            "installer_path": f"/updates/files/{patch_name}",
                            "sha256": hashlib.sha256(patch_content).hexdigest(),
                            "size": len(patch_content),
                            "target_sha256": hashlib.sha256(target).hexdigest(),
                            "target_size": len(target),
                        }
                    ],
                }
            },
        }
        response = Mock(status_code=200)
        response.raise_for_status.return_value = None
        response.json.return_value = manifest
        session = Mock()
        session.headers = {}
        session.get.return_value = response
        config = OnlineConfig(
            base_url="https://203.0.113.10",
            ca_bundle=str(self.database.path),
        )
        previous = os.environ.get("INTDEMO_DATA_DIR")
        os.environ["INTDEMO_DATA_DIR"] = self.temp_dir.name
        try:
            updates = Path(self.temp_dir.name) / "updates"
            updates.mkdir()
            (updates / f"IntDemo-UOS-arm64-{APP_VERSION}.deb").write_bytes(base)

            update = UpdateClient(
                config,
                session=session,
                platform_key="linux-aarch64",
            ).check()

            self.assertTrue(update.is_uos_delta)
            self.assertEqual(update.installer_name, patch_name)
            self.assertEqual(update.full_installer_name, full_name)
            self.assertEqual(update.base_size, len(base))
            self.assertTrue(
                (
                    updates
                    / "base"
                    / f"IntDemo-UOS-arm64-{APP_VERSION}.deb"
                ).is_file()
            )

            # Removing the verified base must silently restore the full path.
            (
                updates / "base" / f"IntDemo-UOS-arm64-{APP_VERSION}.deb"
            ).unlink()
            update = UpdateClient(
                config,
                session=session,
                platform_key="linux-aarch64",
            ).check()
            self.assertFalse(update.is_delta)
            self.assertEqual(update.installer_name, full_name)
        finally:
            if previous is None:
                os.environ.pop("INTDEMO_DATA_DIR", None)
            else:
                os.environ["INTDEMO_DATA_DIR"] = previous

    def test_uos_delta_failure_falls_back_to_verified_full_deb(self):
        target_version = _next_patch_version(APP_VERSION)
        base = b"base-deb"
        patch_content = b"invalid-patch"
        target = b"complete-target-deb"
        base_hash = hashlib.sha256(base).hexdigest()
        target_hash = hashlib.sha256(target).hexdigest()
        patch_hash = hashlib.sha256(patch_content).hexdigest()
        full_name = f"IntDemo-UOS-arm64-{target_version}.deb"
        patch_name = (
            f"IntDemo-UOS-arm64-Patch-{APP_VERSION}-to-"
            f"{target_version}.intdelta"
        )

        class DownloadResponse:
            def __init__(self, content):
                self.content = content

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            @staticmethod
            def raise_for_status():
                return None

            def iter_content(self, chunk_size):
                assert chunk_size > 0
                yield self.content

        session = Mock()
        session.headers = {}
        session.get.side_effect = [
            DownloadResponse(patch_content),
            DownloadResponse(target),
        ]
        config = OnlineConfig(
            base_url="https://203.0.113.10",
            ca_bundle=str(self.database.path),
        )
        update = UpdateInfo(
            version=target_version,
            installer_url=f"https://203.0.113.10/updates/files/{patch_name}",
            installer_name=patch_name,
            sha256=patch_hash,
            size=len(patch_content),
            notes="",
            package_kind="delta",
            from_version=APP_VERSION,
            platform_key="linux-aarch64",
            full_installer_url=(
                f"https://203.0.113.10/updates/files/{full_name}"
            ),
            full_installer_name=full_name,
            full_sha256=target_hash,
            full_size=len(target),
            delta_format="uos-deb-xdelta-v1",
            delta_algorithm="xdelta3",
            base_sha256=base_hash,
            base_size=len(base),
            target_sha256=target_hash,
            target_size=len(target),
        )
        previous_data = os.environ.get("INTDEMO_DATA_DIR")
        previous_engine = os.environ.get("INTDEMO_XDELTA3_PATH")
        os.environ["INTDEMO_DATA_DIR"] = self.temp_dir.name
        os.environ["INTDEMO_XDELTA3_PATH"] = str(
            Path(self.temp_dir.name) / "missing-xdelta3"
        )
        states = []
        try:
            updates = Path(self.temp_dir.name) / "updates"
            updates.mkdir()
            (updates / f"IntDemo-UOS-arm64-{APP_VERSION}.deb").write_bytes(base)
            client = UpdateClient(
                config,
                session=session,
                platform_key="linux-aarch64",
            )
            result = client.download(
                update,
                state_callback=lambda state, message: states.append(
                    (state, message)
                ),
            )
            self.assertEqual(result.read_bytes(), target)
            self.assertEqual(result.name, full_name)
            self.assertTrue(
                (updates / "pending" / "pending-install.json").is_file()
            )
            self.assertIn("fallback_full", {state for state, _ in states})
        finally:
            if previous_data is None:
                os.environ.pop("INTDEMO_DATA_DIR", None)
            else:
                os.environ["INTDEMO_DATA_DIR"] = previous_data
            if previous_engine is None:
                os.environ.pop("INTDEMO_XDELTA3_PATH", None)
            else:
                os.environ["INTDEMO_XDELTA3_PATH"] = previous_engine

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

        update = UpdateClient(
            config,
            session=session,
            platform_key=WINDOWS_UPDATE_PLATFORM,
        ).check()

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

        self.assertIsNone(
            UpdateClient(
                config,
                session=session,
                platform_key=WINDOWS_UPDATE_PLATFORM,
            ).check()
        )

        no_content = Mock(status_code=204)
        no_content.raise_for_status.return_value = None
        session.get.return_value = no_content

        self.assertIsNone(
            UpdateClient(
                config,
                session=session,
                platform_key=WINDOWS_UPDATE_PLATFORM,
            ).check()
        )
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
                    platform_key=WINDOWS_UPDATE_PLATFORM,
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
            path = UpdateClient(
                config,
                session=session,
                platform_key=WINDOWS_UPDATE_PLATFORM,
            ).download(
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
                UpdateClient(
                    config,
                    session=session,
                    platform_key=WINDOWS_UPDATE_PLATFORM,
                ).download(
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
                "yellow_counts": {"workflow_detail_total": 2},
                "violation_counts": {
                    "证件异常": {"total": 2, "has_phone": 1, "other": 1}
                },
                "yellow_violation_counts": {
                    "证件异常": {"total": 1, "has_phone": 1, "other": 0}
                },
            },
            task_id="run-safe-id",
        )
        due = self.database.get_due_sync_items()
        self.assertEqual(len(due), 1)
        encoded = json.dumps(due[0]["item"], ensure_ascii=False)
        self.assertNotIn("绝不能上传", encoded)
        self.assertNotIn("file_name", encoded)
        self.assertEqual(due[0]["item"]["payload"]["yellow_amount"], 2)
        self.assertEqual(
            due[0]["item"]["payload"]["summary"]["violation_counts"]["证件异常"][
                "total"
            ],
            2,
        )
        self.assertEqual(
            due[0]["item"]["payload"]["summary"]["yellow_violation_counts"][
                "证件异常"
            ]["total"],
            1,
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
        self.assertTrue(status.data_changed)
        self.assertEqual(status.pending_count, 0)
        self.assertEqual(len(self.api.push_calls), 1)
        unchanged = engine.run_once()
        self.assertFalse(unchanged.data_changed)
        self.assertEqual(len(self.api.push_calls), 1)

    def test_revoked_session_stays_offline_and_uploads_after_reauthentication(self):
        account = self.session.login("station", "Online!234")
        encrypted_profile = self.database.load_secure_online_profile()
        self.database.record_activity(
            account.id,
            "workflow_detail_total",
            2,
            source="unified_workflow",
        )
        original_push = self.api.push
        self.api.push = Mock(
            side_effect=ApiResponseError(
                "session_revoked",
                "登录会话已失效",
                status_code=401,
            )
        )

        offline = SyncEngine(self.database, self.session).run_once()

        self.assertEqual(offline.state, "reauth_required")
        self.assertEqual(offline.pending_count, 1)
        self.assertEqual(self.session.state.account.id, account.id)
        self.assertEqual(self.session.state.mode, "reauth_required")
        self.assertEqual(
            self.database.load_secure_online_profile(),
            encrypted_profile,
        )
        with self.assertRaisesRegex(NetworkUnavailable, "左下角"):
            self.session.access_token()

        self.api.push = original_push
        restored = self.session.reauthenticate("Online!234")
        online = SyncEngine(self.database, self.session).run_once()

        self.assertEqual(restored.id, account.id)
        self.assertTrue(self.session.state.is_online)
        self.assertEqual(online.state, "online")
        self.assertEqual(online.pending_count, 0)
        self.assertEqual(len(self.api.push_calls), 1)

    def test_test_account_never_records_or_queues_business_statistics(self):
        bundle = self.api._bundle("station", self.session.device_uid)
        bundle["account"]["stats_scope"] = "all"
        regular_account = self.session._save_bundle(bundle, "Online!234")
        self.database.record_activity(
            regular_account.id,
            "workflow_detail_total",
            3,
            source="unified_workflow",
        )
        self.assertEqual(self.database.get_sync_pending_count(self.api.account_id), 1)

        bundle["account"]["is_test"] = True
        account = self.session._save_bundle(bundle, "Online!234")

        self.database.record_activity(
            account.id,
            "workflow_detail_total",
            5,
            source="unified_workflow",
        )

        self.assertTrue(account.is_test)
        self.assertFalse(account.statistics_enabled)
        self.assertEqual(account.role, "user")
        self.assertTrue(account.can_view_all_stats)
        self.assertEqual(self.database.get_sync_pending_count(self.api.account_id), 0)
        self.assertTrue(
            all(row["total"] == 0 for row in self.database.get_user_totals(account.id))
        )

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

    def test_remote_account_delete_change_purges_station_and_statistics(self):
        self.session.login("station", "Online!234")
        other_id = str(uuid.uuid4())
        occurred_at = datetime.now(timezone.utc).isoformat()
        self.database.apply_sync_snapshot(
            {
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
                        "username": "deleted_station",
                        "display_name": "待删除站点",
                        "role": "user",
                        "stats_scope": "own",
                        "is_active": False,
                        "is_archived": True,
                        "entitlement_revision": 2,
                    },
                ],
                "metrics": [],
                "activity_events": [
                    {
                        "account_id": other_id,
                        "event_uid": str(uuid.uuid4()),
                        "metric_key": "workflow_detail_total",
                        "amount": 5,
                        "business_date": "2026-08-05",
                        "source": "unified_workflow",
                        "task_id": None,
                        "summary": {},
                        "occurred_at": occurred_at,
                    }
                ],
                "workflow_batches": [],
                "workflow_runs": [],
                "entitlement_revision": 1,
                "stats_scope": "all",
            }
        )
        self.assertIn(
            "deleted_station",
            {account.username for account in self.database.list_accounts()},
        )

        self.database.apply_sync_changes(
            {
                "changes": [
                    {
                        "revision": 11,
                        "account_id": None,
                        "kind": "account",
                        "entity_id": other_id,
                        "entity_revision": 3,
                        "operation": "delete",
                        "payload": {"username": "deleted_station"},
                        "occurred_at": occurred_at,
                    }
                ],
                "latest_revision": 11,
                "has_more": False,
                "entitlement_revision": 1,
                "stats_scope": "all",
            }
        )

        self.assertNotIn(
            "deleted_station",
            {account.username for account in self.database.list_accounts()},
        )
        self.assertNotIn(
            "deleted_station",
            {row["username"] for row in self.database.get_all_account_totals()},
        )

    def test_full_snapshot_removes_remote_accounts_missing_from_server(self):
        self.session.login("station", "Online!234")
        stale_id = str(uuid.uuid4())
        base_snapshot = {
            "revision": 20,
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
                    "id": stale_id,
                    "username": "stale_station",
                    "display_name": "已不存在站点",
                    "role": "user",
                    "stats_scope": "own",
                    "is_active": False,
                    "is_archived": True,
                    "entitlement_revision": 2,
                },
            ],
            "metrics": [],
            "activity_events": [],
            "workflow_batches": [],
            "workflow_runs": [],
            "entitlement_revision": 1,
            "stats_scope": "all",
        }
        self.database.apply_sync_snapshot(base_snapshot)
        base_snapshot["revision"] = 21
        base_snapshot["accounts"] = base_snapshot["accounts"][:1]
        self.database.apply_sync_snapshot(base_snapshot)

        self.assertNotIn(
            "stale_station",
            {account.username for account in self.database.list_accounts()},
        )

    def test_complete_admin_account_list_purges_missing_remote_cache(self):
        current = self.session.login("station", "Online!234")
        local_only = self.database.create_account(
            "local_only",
            "Local!23456",
            "user",
            current.id,
        )
        stale_id = str(uuid.uuid4())
        occurred_at = datetime.now(timezone.utc).isoformat()
        current_payload = {
            "id": self.api.account_id,
            "username": "station",
            "display_name": "本站",
            "role": "user",
            "stats_scope": "all",
            "is_active": True,
            "is_archived": False,
            "entitlement_revision": 1,
        }
        stale_payload = {
            "id": stale_id,
            "username": "stale_station",
            "display_name": "已永久删除站点",
            "role": "user",
            "stats_scope": "own",
            "is_active": False,
            "is_archived": True,
            "entitlement_revision": 2,
        }
        self.database.apply_sync_snapshot(
            {
                "revision": 30,
                "accounts": [current_payload, stale_payload],
                "metrics": [],
                "activity_events": [
                    {
                        "account_id": stale_id,
                        "event_uid": str(uuid.uuid4()),
                        "metric_key": "workflow_detail_total",
                        "amount": 9,
                        "business_date": "2026-08-06",
                        "source": "unified_workflow",
                        "task_id": None,
                        "summary": {},
                        "occurred_at": occurred_at,
                    }
                ],
                "workflow_batches": [
                    {
                        "account_id": stale_id,
                        "entity_id": "stale-batch",
                        "event_uid": str(uuid.uuid4()),
                        "revision": 1,
                        "status": "succeeded",
                        "input_fingerprint": "a" * 64,
                        "business_date": "2026-08-06",
                    }
                ],
                "workflow_runs": [
                    {
                        "account_id": stale_id,
                        "entity_id": "stale-run",
                        "event_uid": str(uuid.uuid4()),
                        "revision": 1,
                        "batch_id": "stale-batch",
                        "status": "succeeded",
                    }
                ],
                "entitlement_revision": 1,
                "stats_scope": "all",
            }
        )
        with self.database._connect() as conn:
            self.database._enqueue_sync_item(
                conn,
                server_account_id=stale_id,
                kind="activity_event",
                entity_id="stale-event",
                revision=1,
                occurred_at=occurred_at,
                payload={"metric_key": "workflow_detail_total", "amount": 9},
            )

        reconciled = self.database.reconcile_remote_accounts([current_payload])

        self.assertEqual(
            [account.server_account_id for account in reconciled],
            [self.api.account_id],
        )
        accounts = {account.username: account for account in self.database.list_accounts()}
        self.assertIn("station", accounts)
        self.assertEqual(accounts["local_only"].id, local_only.id)
        self.assertNotIn("stale_station", accounts)
        self.assertNotIn(
            "stale_station",
            {row["username"] for row in self.database.get_all_account_totals()},
        )
        with self.database._connect() as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM remote_workflow_batches WHERE server_account_id=?",
                    (stale_id,),
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM remote_workflow_runs WHERE server_account_id=?",
                    (stale_id,),
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM sync_outbox WHERE server_account_id=?",
                    (stale_id,),
                ).fetchone()[0],
                0,
            )

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI test")
    def test_dpapi_round_trip_and_ciphertext(self):
        protector = DpapiProtector()
        plaintext = b"refresh-token-and-offline-entitlement"
        ciphertext = protector.protect(plaintext)
        self.assertNotEqual(ciphertext, plaintext)
        self.assertEqual(protector.unprotect(ciphertext), plaintext)


if __name__ == "__main__":
    unittest.main()

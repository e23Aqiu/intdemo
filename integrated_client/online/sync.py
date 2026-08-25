from __future__ import annotations

import random
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime

from .api import ApiResponseError, NetworkUnavailable
from .session import OnlineSessionManager


@dataclass(frozen=True)
class SyncStatus:
    state: str
    pending_count: int
    quarantined_count: int
    last_sync_at: str | None
    error: str | None = None
    data_changed: bool = False


class SyncEngine:
    def __init__(self, database, session: OnlineSessionManager):
        self.database = database
        self.session = session
        self._lock = threading.Lock()

    @staticmethod
    def _retry_delay(attempt_count: int) -> float:
        base = min(300.0, 2.0 ** min(max(attempt_count, 1), 8))
        return base * random.uniform(0.75, 1.25)

    @staticmethod
    def _event_uid_key(value) -> str:
        """Normalize UUID spellings used by SQLite and the JSON API.

        Older local activity rows use ``uuid.hex`` while FastAPI serializes
        the same value with hyphens.  Treat both spellings as one receipt so
        an accepted event is removed from the pending queue.
        """
        text = str(value or "").strip()
        try:
            return uuid.UUID(text).hex
        except (AttributeError, TypeError, ValueError):
            return text.casefold()

    def status(
        self,
        state: str | None = None,
        error: str | None = None,
        *,
        data_changed: bool = False,
    ) -> SyncStatus:
        sync_state = self.database.get_sync_state()
        server_account_id = (
            self.session.state.account.server_account_id if self.session.state else None
        )
        return SyncStatus(
            state=state
            or (
                "reauth_required"
                if self.session.state
                and self.session.state.mode == "reauth_required"
                else (
                    "online"
                    if self.session.state and self.session.state.is_online
                    else "offline"
                )
            ),
            pending_count=self.database.get_sync_pending_count(server_account_id),
            quarantined_count=self.database.get_sync_quarantined_count(
                server_account_id
            ),
            last_sync_at=sync_state.get("last_sync_at"),
            error=error or sync_state.get("last_error"),
            data_changed=bool(data_changed),
        )

    def _push(self, access_token: str) -> None:
        server_account_id = self.session.state.account.server_account_id
        rows = self.database.get_due_sync_items(
            limit=100,
            server_account_id=server_account_id,
        )
        if not rows:
            return
        items = [row["item"] for row in rows]
        try:
            response = self.session.api.push(access_token, items)
        except (NetworkUnavailable, ApiResponseError) as exc:
            if isinstance(exc, ApiResponseError) and not exc.retryable:
                raise
            for row in rows:
                attempt = int(row["attempt_count"]) + 1
                self.database.schedule_sync_retry(
                    row["id"],
                    time.time() + self._retry_delay(attempt),
                    getattr(exc, "code", "network_unavailable"),
                    str(exc),
                )
            raise
        rows_by_uid = {
            self._event_uid_key(row["event_uid"]): row
            for row in rows
        }
        acknowledged_ids = set()
        for result in response.get("items", []):
            row = rows_by_uid.get(
                self._event_uid_key(result.get("event_uid"))
            )
            if not row:
                continue
            acknowledged_ids.add(row["id"])
            status = result.get("status")
            if status in {"accepted", "duplicate", "stale"}:
                self.database.mark_sync_item_sent(
                    row["id"],
                    result.get("server_revision"),
                )
            elif result.get("retryable"):
                attempt = int(row["attempt_count"]) + 1
                self.database.schedule_sync_retry(
                    row["id"],
                    time.time() + self._retry_delay(attempt),
                    str(result.get("code") or "server_retry"),
                    str(result.get("message") or "服务器要求重试"),
                )
            else:
                self.database.quarantine_sync_item(
                    row["id"],
                    str(result.get("code") or "invalid_item"),
                    str(result.get("message") or "同步项被服务器永久拒绝"),
                )
        for row in rows:
            if row["id"] in acknowledged_ids:
                continue
            attempt = int(row["attempt_count"]) + 1
            self.database.schedule_sync_retry(
                row["id"],
                time.time() + self._retry_delay(attempt),
                "missing_sync_receipt",
                "服务器响应中缺少该同步项的确认结果",
            )

    def _pull(self, access_token: str) -> bool:
        state = self.database.get_sync_state()
        data_changed = False
        if state.get("needs_snapshot"):
            snapshot = self.session.api.snapshot(access_token)
            self.database.apply_sync_snapshot(snapshot)
            state = self.database.get_sync_state()
            data_changed = True
        after_revision = int(state.get("last_revision") or 0)
        while True:
            response = self.session.api.pull(access_token, after_revision)
            self.database.apply_sync_changes(response)
            changes = response.get("changes") or []
            if changes:
                data_changed = True
                after_revision = int(changes[-1]["revision"])
            if not response.get("has_more"):
                break
        if self.database.get_sync_state().get("needs_snapshot"):
            snapshot = self.session.api.snapshot(access_token)
            self.database.apply_sync_snapshot(snapshot)
            data_changed = True
        return data_changed

    def _delay_due_items(self, code: str, message: str) -> None:
        server_account_id = (
            self.session.state.account.server_account_id
            if self.session.state
            else None
        )
        for row in self.database.get_due_sync_items(
            limit=100,
            server_account_id=server_account_id,
        ):
            attempt = int(row["attempt_count"]) + 1
            self.database.schedule_sync_retry(
                row["id"],
                time.time() + self._retry_delay(attempt),
                code,
                message,
            )

    def run_once(self) -> SyncStatus:
        if not self._lock.acquire(blocking=False):
            return self.status("syncing")
        try:
            access_token = self.session.access_token()
            self._push(access_token)
            data_changed = self._pull(access_token)
            now = datetime.now().astimezone().isoformat(timespec="seconds")
            self.database.mark_sync_success(now)
            self.session.note_online()
            return self.status("online", data_changed=data_changed)
        except NetworkUnavailable as exc:
            self._delay_due_items("network_unavailable", str(exc))
            self.database.mark_sync_error(str(exc))
            self.session.note_offline()
            return self.status("offline", str(exc))
        except ApiResponseError as exc:
            self.database.mark_sync_error(exc.message)
            fatal_session_codes = {
                "authentication_required",
                "invalid_access_token",
                "access_token_expired",
                "invalid_refresh_token",
                "session_revoked",
                "device_revoked",
                "account_unavailable",
                "password_change_required",
                "refresh_token_expired",
                "refresh_token_reuse",
            }
            if exc.code in fatal_session_codes or exc.status_code == 401:
                self.database.enforce_own_cache_for_current_account()
                self.session.invalidate_credentials()
                return self.status("reauth_required", exc.message)
            if exc.status_code >= 500 or exc.retryable:
                self._delay_due_items(exc.code, exc.message)
                self.session.note_offline()
                return self.status("offline", exc.message)
            return self.status("error", exc.message)
        except Exception as exc:
            self.database.mark_sync_error(str(exc))
            return self.status("error", str(exc))
        finally:
            self._lock.release()

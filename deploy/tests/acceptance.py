from __future__ import annotations

import argparse
import asyncio
import json
import ssl
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone

import websockets


class Client:
    def __init__(self, base_url: str, ca_bundle: str):
        self.base_url = base_url.rstrip("/")
        self.context = ssl.create_default_context(cafile=ca_bundle)

    def request(self, method: str, path: str, body=None, token=None, expected=200):
        data = (
            json.dumps(body, ensure_ascii=False).encode("utf-8")
            if body is not None
            else None
        )
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(
            f"{self.base_url}/api/v1{path}",
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(
                request,
                context=self.context,
                timeout=15,
            ) as response:
                status = response.status
                payload = (
                    json.loads(response.read().decode("utf-8"))
                    if status != 204
                    else None
                )
        except urllib.error.HTTPError as exc:
            status = exc.code
            payload = json.loads(exc.read().decode("utf-8"))
        if status != expected:
            raise AssertionError(
                f"{method} {path}: expected {expected}, got {status}: {payload}"
            )
        return payload


def login(client: Client, username: str, password: str, device_uid: str):
    return client.request(
        "POST",
        "/auth/login",
        {
            "username": username,
            "password": password,
            "device_uid": device_uid,
            "device_name": f"acceptance-{device_uid[-4:]}",
            "client_version": "0.2.4-acceptance",
        },
    )


def activity_item(
    *,
    amount: int,
    occurred_at: datetime | None = None,
    reason: str = "验收原因",
):
    event_uid = str(uuid.uuid4())
    occurred_at = occurred_at or datetime.now(timezone.utc)
    return {
        "schema_version": 1,
        "event_uid": event_uid,
        "kind": "activity_event",
        "entity_id": event_uid,
        "revision": 1,
        "occurred_at": occurred_at.isoformat(),
        "payload": {
            "metric_key": "workflow_detail_total",
            "amount": amount,
            "business_date": occurred_at.date().isoformat(),
            "source": "unified_workflow",
            "task_id": None,
            "summary": {
                "row_count": amount,
                "violation_counts": {
                    reason: {"total": 1, "has_phone": 0, "other": 1}
                },
            },
        },
    }


async def expect_websocket_revision(
    websocket_url: str,
    ca_bundle: str,
    token: str,
    trigger,
):
    context = ssl.create_default_context(cafile=ca_bundle)
    async with websockets.connect(
        websocket_url,
        ssl=context,
        additional_headers={"Authorization": f"Bearer {token}"},
        open_timeout=10,
    ) as socket:
        connected = json.loads(await asyncio.wait_for(socket.recv(), timeout=10))
        assert connected["type"] == "connected"
        trigger()
        while True:
            message = json.loads(await asyncio.wait_for(socket.recv(), timeout=10))
            if message.get("type") == "revision_changed":
                return message


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("base_url")
    parser.add_argument("ca_bundle")
    args = parser.parse_args()
    client = Client(args.base_url, args.ca_bundle)

    try:
        urllib.request.urlopen(
            f"{args.base_url}/api/v1/health/live",
            context=ssl.create_default_context(),
            timeout=5,
        )
    except ssl.SSLCertVerificationError:
        pass
    except urllib.error.URLError as exc:
        if not isinstance(exc.reason, ssl.SSLCertVerificationError):
            raise
    else:
        raise AssertionError(
            "private-CA endpoint unexpectedly trusted without its root"
        )

    assert client.request("GET", "/health/live")["status"] == "live"
    not_ready = client.request("GET", "/health/ready", expected=503)
    assert not_ready["reason"] == "admin_password_change_required"

    device_a = str(uuid.uuid4())
    admin_a = login(client, "admin", "123456", device_a)
    admin_a = client.request(
        "POST",
        "/auth/change-password",
        {"current_password": "123456", "new_password": "Admin!23456"},
        admin_a["access_token"],
    )
    assert client.request("GET", "/health/ready")["status"] == "ready"

    account_id = admin_a["account"]["id"]
    client.request(
        "PATCH",
        f"/admin/accounts/{account_id}",
        {"device_limit": 2},
        admin_a["access_token"],
    )
    device_b = str(uuid.uuid4())
    admin_b = login(client, "admin", "Admin!23456", device_b)
    third = client.request(
        "POST",
        "/auth/login",
        {
            "username": "admin",
            "password": "Admin!23456",
            "device_uid": str(uuid.uuid4()),
            "device_name": "acceptance-device",
            "client_version": "acceptance",
        },
        expected=409,
    )
    assert third["code"] == "device_limit_reached"

    item = activity_item(amount=4)
    event_uid = item["event_uid"]

    def push_once():
        pushed = client.request(
            "POST",
            "/sync/push",
            {"items": [item]},
            admin_a["access_token"],
        )
        assert pushed["items"][0]["status"] == "accepted"

    websocket_url = args.base_url.replace("https://", "wss://", 1)
    websocket_url += "/api/v1/ws/updates"
    asyncio.run(
        expect_websocket_revision(
            websocket_url,
            args.ca_bundle,
            admin_b["access_token"],
            push_once,
        )
    )

    deadline = time.monotonic() + 10
    while True:
        pulled = client.request(
            "GET",
            "/sync/pull?after_revision=0",
            token=admin_b["access_token"],
        )
        if any(
            change.get("payload", {}).get("event_uid") == event_uid
            for change in pulled["changes"]
        ):
            break
        if time.monotonic() >= deadline:
            raise AssertionError(
                "logical client B did not observe update within 10 seconds"
            )
        time.sleep(0.25)

    duplicate = client.request(
        "POST",
        "/sync/push",
        {"items": [item]},
        admin_a["access_token"],
    )
    assert duplicate["items"][0]["status"] == "duplicate"

    created = client.request(
        "POST",
        "/admin/accounts",
        {
            "username": "acceptance_station",
            "display_name": "验收站",
            "role": "user",
            "stats_scope": "own",
            "device_limit": 1,
            "is_active": True,
        },
        admin_a["access_token"],
        expected=201,
    )
    station = login(
        client,
        "acceptance_station",
        "123456",
        str(uuid.uuid4()),
    )
    station_device = station["device"]["device_uid"]
    station = client.request(
        "POST",
        "/auth/change-password",
        {"current_password": "123456", "new_password": "Station!234"},
        station["access_token"],
    )

    own_pull = client.request(
        "GET",
        "/sync/pull?after_revision=0",
        token=station["access_token"],
    )
    assert not any(
        change.get("payload", {}).get("event_uid") == event_uid
        for change in own_pull["changes"]
    )

    client.request(
        "PATCH",
        f"/admin/accounts/{created['id']}",
        {"stats_scope": "all"},
        admin_a["access_token"],
    )
    station = login(
        client,
        "acceptance_station",
        "Station!234",
        station_device,
    )
    all_pull = client.request(
        "GET",
        "/sync/pull?after_revision=0",
        token=station["access_token"],
    )
    assert any(
        change.get("payload", {}).get("event_uid") == event_uid
        for change in all_pull["changes"]
    )

    client.request(
        "PATCH",
        f"/admin/accounts/{created['id']}",
        {"stats_scope": "own"},
        admin_a["access_token"],
    )
    revoked_scope_token = client.request(
        "GET",
        "/sync/pull?after_revision=0",
        token=station["access_token"],
        expected=401,
    )
    assert revoked_scope_token["code"] == "session_revoked"
    station = login(
        client,
        "acceptance_station",
        "Station!234",
        station_device,
    )

    additional_station = login(
        client,
        "acceptance_station",
        "Station!234",
        str(uuid.uuid4()),
    )
    assert additional_station["account"]["id"] == created["id"]
    station_devices = client.request(
        "GET",
        f"/admin/accounts/{created['id']}/devices",
        token=admin_a["access_token"],
    )
    current_station_device = next(
        device for device in station_devices if device["device_uid"] == station_device
    )
    client.request(
        "POST",
        f"/admin/devices/{current_station_device['id']}/revoke",
        token=admin_a["access_token"],
        expected=204,
    )
    replacement_device = str(uuid.uuid4())
    station = login(
        client,
        "acceptance_station",
        "Station!234",
        replacement_device,
    )

    queued_while_offline = activity_item(amount=2)
    client.request(
        "POST",
        "/admin/data-reset",
        {"account_id": created["id"], "confirmation": "RESET"},
        admin_a["access_token"],
        expected=204,
    )
    tombstoned = client.request(
        "POST",
        "/sync/push",
        {"items": [queued_while_offline]},
        station["access_token"],
    )
    assert tombstoned["items"][0]["status"] == "rejected"
    assert tombstoned["items"][0]["code"] == "data_reset_tombstone"

    post_reset_item = activity_item(amount=3, reason="重置后验收")
    post_reset_push = client.request(
        "POST",
        "/sync/push",
        {"items": [post_reset_item]},
        station["access_token"],
    )
    assert post_reset_push["items"][0]["status"] == "accepted"

    client.request(
        "POST",
        f"/admin/accounts/{created['id']}/archive",
        token=admin_a["access_token"],
    )
    archived_snapshot = client.request(
        "GET",
        "/sync/snapshot",
        token=admin_a["access_token"],
    )
    assert any(
        event.get("event_uid") == post_reset_item["event_uid"]
        for event in archived_snapshot["activity_events"]
    )
    archived = client.request(
        "POST",
        "/auth/login",
        {
            "username": "acceptance_station",
            "password": "Station!234",
            "device_uid": str(uuid.uuid4()),
            "device_name": "archived",
            "client_version": "test",
        },
        expected=403,
    )
    assert archived["code"] == "account_archived"
    print("IntDemo Compose acceptance passed")


if __name__ == "__main__":
    main()

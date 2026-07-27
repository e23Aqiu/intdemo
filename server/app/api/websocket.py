from __future__ import annotations

import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..database import SessionLocal, utcnow
from ..models import Account, Device
from ..realtime import update_hub
from ..security import decode_access_token
from ..services import latest_revision

router = APIRouter(tags=["websocket"])


def _bearer_token(websocket: WebSocket) -> str | None:
    header = websocket.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() == "bearer" and value:
        return value
    return None


@router.websocket("/ws/updates")
async def updates(websocket: WebSocket) -> None:
    token = _bearer_token(websocket)
    if not token:
        await websocket.close(code=4401, reason="authentication required")
        return
    try:
        claims = decode_access_token(token)
        account_id = uuid.UUID(claims["sub"])
        device_id = uuid.UUID(claims["did"])
    except Exception:
        await websocket.close(code=4401, reason="invalid access token")
        return
    with SessionLocal() as db:
        account = db.get(Account, account_id)
        device = db.get(Device, device_id)
        valid = (
            account is not None
            and device is not None
            and device.account_id == account.id
            and device.revoked_at is None
            and account.is_active
            and not account.is_archived
            and not account.must_change_password
            and account.token_version == int(claims.get("ver", -1))
            and device.token_version == int(claims.get("dver", -1))
        )
        if valid:
            device.last_seen_at = utcnow()
            db.commit()
            revision = latest_revision(db)
        else:
            revision = 0
    if not valid:
        await websocket.close(code=4403, reason="session unavailable")
        return
    await update_hub.connect(websocket)
    try:
        await websocket.send_json({"type": "connected", "revision": revision})
        while True:
            message = await websocket.receive_text()
            if message == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        pass
    finally:
        await update_hub.disconnect(websocket)

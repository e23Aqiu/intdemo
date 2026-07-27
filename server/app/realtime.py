from __future__ import annotations

import asyncio
from typing import Any

from fastapi import WebSocket


class UpdateHub:
    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self._clients.add(websocket)

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(websocket)

    async def broadcast_revision(self, revision: int) -> None:
        payload: dict[str, Any] = {
            "type": "revision_changed",
            "revision": revision,
        }
        async with self._lock:
            clients = tuple(self._clients)
        failed = []
        for client in clients:
            try:
                await client.send_json(payload)
            except Exception:
                failed.append(client)
        if failed:
            async with self._lock:
                for client in failed:
                    self._clients.discard(client)


update_hub = UpdateHub()

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from fastapi import WebSocket


class UpdateHub:
    def __init__(self) -> None:
        self._clients: dict[WebSocket, uuid.UUID] = {}
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket, device_id: uuid.UUID) -> None:
        await websocket.accept()
        async with self._lock:
            self._clients[websocket] = device_id

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self._lock:
            self._clients.pop(websocket, None)

    async def connected_device_ids(self) -> set[uuid.UUID]:
        async with self._lock:
            return set(self._clients.values())

    async def close_device(self, device_id: uuid.UUID) -> int:
        async with self._lock:
            clients = tuple(
                client
                for client, connected_device_id in self._clients.items()
                if connected_device_id == device_id
            )
        return await self._close_clients(clients)

    async def close_blocked(self, is_blocked) -> int:
        async with self._lock:
            clients = tuple(
                client
                for client, device_id in self._clients.items()
                if is_blocked(device_id)
            )
        return await self._close_clients(clients)

    async def _close_clients(self, clients: tuple[WebSocket, ...]) -> int:
        closed = 0
        for client in clients:
            try:
                await client.send_json({"type": "test_connection_blocked"})
            except Exception:
                pass
            try:
                await client.close(
                    code=4410,
                    reason="connection disabled by test control",
                )
                closed += 1
            except Exception:
                pass
        if clients:
            async with self._lock:
                for client in clients:
                    self._clients.pop(client, None)
        return closed

    async def broadcast_revision(self, revision: int) -> None:
        await self.broadcast_event("revision_changed", revision=revision)

    async def broadcast_event(self, event_type: str, **fields: Any) -> None:
        payload: dict[str, Any] = {"type": str(event_type), **fields}
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
                    self._clients.pop(client, None)


update_hub = UpdateHub()

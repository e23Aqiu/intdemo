from __future__ import annotations

import threading
import uuid

from .errors import ApiError


class ConnectionTestGate:
    """Process-local, fail-open connection gate used by the publisher test tool."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._global_blocked = False
        self._blocked_devices: set[uuid.UUID] = set()
        self._allowed_devices: set[uuid.UUID] = set()
        self._control_devices: set[uuid.UUID] = set()

    @property
    def global_blocked(self) -> bool:
        with self._lock:
            return self._global_blocked

    def reset(self) -> None:
        with self._lock:
            self._global_blocked = False
            self._blocked_devices.clear()
            self._allowed_devices.clear()
            self._control_devices.clear()

    def register_control_device(self, device_id: uuid.UUID) -> None:
        with self._lock:
            self._control_devices.add(device_id)

    def unregister_control_device(self, device_id: uuid.UUID) -> None:
        with self._lock:
            self._control_devices.discard(device_id)

    def is_control_device(self, device_id: uuid.UUID) -> bool:
        with self._lock:
            return device_id in self._control_devices

    def is_blocked(self, device_id: uuid.UUID) -> bool:
        with self._lock:
            if self._global_blocked:
                return device_id not in self._allowed_devices
            return device_id in self._blocked_devices

    def disconnect_all(self) -> None:
        with self._lock:
            self._global_blocked = True
            self._blocked_devices.clear()
            self._allowed_devices.clear()

    def restore_all(self) -> None:
        with self._lock:
            self._global_blocked = False
            self._blocked_devices.clear()
            self._allowed_devices.clear()

    def disconnect_device(self, device_id: uuid.UUID) -> None:
        with self._lock:
            self._allowed_devices.discard(device_id)
            self._blocked_devices.add(device_id)

    def restore_device(self, device_id: uuid.UUID) -> None:
        with self._lock:
            self._blocked_devices.discard(device_id)
            if self._global_blocked:
                self._allowed_devices.add(device_id)
            else:
                self._allowed_devices.discard(device_id)

    def require_available(self, device_id: uuid.UUID) -> None:
        if self.is_blocked(device_id):
            raise ApiError(
                "test_connection_blocked",
                "客户端与服务器的连接已被测试工具临时断开",
                status_code=503,
                retryable=True,
            )


connection_test_gate = ConnectionTestGate()

from __future__ import annotations

import os
import tempfile
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_TEST_DIR = Path(tempfile.mkdtemp(prefix="intdemo-server-tests-"))
os.environ.setdefault("INTDEMO_ENVIRONMENT", "test")
os.environ.setdefault(
    "INTDEMO_DATABASE_URL",
    f"sqlite:///{(_TEST_DIR / 'test.db').as_posix()}",
)
os.environ.setdefault(
    "INTDEMO_JWT_SECRET",
    "test-secret-that-is-long-enough-for-intdemo",
)
os.environ.setdefault("INTDEMO_BOOTSTRAP_ENABLED", "1")

from app.bootstrap import bootstrap_database  # noqa: E402
from app.database import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture
def client() -> TestClient:
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        bootstrap_database(db)
    with TestClient(app, raise_server_exceptions=True) as test_client:
        yield test_client


def device_uid(number: int = 1) -> str:
    return str(uuid.UUID(int=number))


def login(
    client: TestClient,
    username: str,
    password: str,
    *,
    device: int = 1,
) -> dict:
    response = client.post(
        "/api/v1/auth/login",
        json={
            "username": username,
            "password": password,
            "device_uid": device_uid(device),
            "device_name": f"test-device-{device}",
            "client_version": "0.2.0-test",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def changed_admin(client: TestClient, *, device: int = 1) -> dict:
    bundle = login(client, "admin", "123456", device=device)
    response = client.post(
        "/api/v1/auth/change-password",
        headers={"Authorization": f"Bearer {bundle['access_token']}"},
        json={
            "current_password": "123456",
            "new_password": "Admin!23456",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def auth_header(bundle: dict) -> dict[str, str]:
    return {"Authorization": f"Bearer {bundle['access_token']}"}

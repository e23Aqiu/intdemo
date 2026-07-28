from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote_plus


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _secret_value(value_name: str, file_name: str, default: str = "") -> str:
    direct = os.getenv(value_name)
    if direct is not None:
        return direct
    path = os.getenv(file_name)
    if path:
        try:
            return Path(path).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise RuntimeError(f"cannot read secret file from {file_name}") from exc
    return default


@dataclass(frozen=True, slots=True)
class Settings:
    environment: str
    database_url: str
    jwt_secret: str
    jwt_issuer: str
    access_token_minutes: int
    refresh_token_days: int
    offline_entitlement_days: int
    offline_private_key: str | None
    cors_origins: tuple[str, ...]
    bootstrap_enabled: bool
    maintenance_mode: bool
    trusted_proxy_headers: bool
    max_sync_items: int
    max_sync_item_bytes: int
    updates_dir: Path

    @property
    def is_production(self) -> bool:
        return self.environment.lower() == "production"

    def validate(self) -> None:
        if self.is_production and len(self.jwt_secret) < 32:
            raise RuntimeError("INTDEMO_JWT_SECRET must contain at least 32 characters")
        if self.is_production and not self.database_url.startswith("postgresql+psycopg://"):
            raise RuntimeError("production requires a PostgreSQL database URL")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    cors = tuple(
        item.strip() for item in os.getenv("INTDEMO_CORS_ORIGINS", "").split(",") if item.strip()
    )
    database_url = os.getenv("INTDEMO_DATABASE_URL")
    if not database_url and os.getenv("INTDEMO_DATABASE_PASSWORD_FILE"):
        password = _secret_value(
            "INTDEMO_DATABASE_PASSWORD",
            "INTDEMO_DATABASE_PASSWORD_FILE",
        )
        user = os.getenv("INTDEMO_DATABASE_USER", "intdemo")
        host = os.getenv("INTDEMO_DATABASE_HOST", "postgres")
        port = int(os.getenv("INTDEMO_DATABASE_PORT", "5432"))
        name = os.getenv("INTDEMO_DATABASE_NAME", "intdemo")
        database_url = (
            f"postgresql+psycopg://{quote_plus(user)}:{quote_plus(password)}"
            f"@{host}:{port}/{quote_plus(name)}"
        )
    settings = Settings(
        environment=os.getenv("INTDEMO_ENVIRONMENT", "development"),
        database_url=database_url
        or f"sqlite:///{Path.cwd().joinpath('intdemo-server-dev.db').as_posix()}",
        jwt_secret=_secret_value(
            "INTDEMO_JWT_SECRET",
            "INTDEMO_JWT_SECRET_FILE",
            "development-only-change-me",
        ),
        jwt_issuer=os.getenv("INTDEMO_JWT_ISSUER", "intdemo-sync"),
        access_token_minutes=int(os.getenv("INTDEMO_ACCESS_TOKEN_MINUTES", "15")),
        refresh_token_days=int(os.getenv("INTDEMO_REFRESH_TOKEN_DAYS", "30")),
        offline_entitlement_days=int(os.getenv("INTDEMO_OFFLINE_ENTITLEMENT_DAYS", "7")),
        offline_private_key=_secret_value(
            "INTDEMO_OFFLINE_PRIVATE_KEY",
            "INTDEMO_OFFLINE_PRIVATE_KEY_FILE",
        )
        or None,
        cors_origins=cors,
        bootstrap_enabled=_env_bool("INTDEMO_BOOTSTRAP_ENABLED", True),
        maintenance_mode=_env_bool("INTDEMO_MAINTENANCE_MODE", False),
        trusted_proxy_headers=_env_bool("INTDEMO_TRUSTED_PROXY_HEADERS", True),
        max_sync_items=int(os.getenv("INTDEMO_MAX_SYNC_ITEMS", "100")),
        max_sync_item_bytes=int(os.getenv("INTDEMO_MAX_SYNC_ITEM_BYTES", str(64 * 1024))),
        updates_dir=Path(
            os.getenv(
                "INTDEMO_UPDATES_DIR",
                str(Path.cwd().joinpath("deploy", "updates")),
            )
        ).expanduser().resolve(),
    )
    settings.validate()
    return settings

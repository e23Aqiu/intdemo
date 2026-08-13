"""Load the dedicated offline trust roots for enhanced trainer packages.

Trainer component signatures deliberately use a separate Ed25519 trust file.
This module never imports or consults online entitlement/update key material.
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
from pathlib import Path

from .trainer_component import (
    Ed25519ManifestVerifier,
    TrainerComponentManager,
)

TRAINER_TRUST_ENVIRONMENT = "INTDEMO_TRAINER_TRUST_FILE"
TRAINER_TRUST_FILE_NAME = "trainer-trust.json"
TRAINER_TRUST_SCHEMA_VERSION = 1
_KEY_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class TrainerTrustConfigurationError(ValueError):
    """The configured trainer trust file is unreadable or invalid."""


def _object_pairs_without_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key: {key}")
        result[key] = value
    return result


def _read_trust_file(path: Path) -> dict[str, bytes]:
    try:
        raw = path.read_bytes()
        if len(raw) > 256 * 1024:
            raise ValueError("file is too large")
        document = json.loads(
            raw.decode("utf-8-sig"),
            object_pairs_hook=_object_pairs_without_duplicates,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"invalid JSON constant: {value}")
            ),
        )
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        RecursionError,
        ValueError,
    ) as exc:
        raise TrainerTrustConfigurationError(
            f"无法读取强化组件可信公钥配置：{path}"
        ) from exc
    if not isinstance(document, dict) or set(document) != {
        "schema_version",
        "keys",
    }:
        raise TrainerTrustConfigurationError(
            "强化组件可信公钥配置字段无效，只允许 schema_version 和 keys"
        )
    if type(document["schema_version"]) is not int or (
        document["schema_version"] != TRAINER_TRUST_SCHEMA_VERSION
    ):
        raise TrainerTrustConfigurationError(
            "强化组件可信公钥配置版本不受支持"
        )
    keys = document["keys"]
    if not isinstance(keys, dict) or not keys:
        raise TrainerTrustConfigurationError(
            "强化组件可信公钥配置必须包含至少一个公钥"
        )
    normalized: dict[str, bytes] = {}
    for key_id, encoded_key in keys.items():
        if not isinstance(key_id, str) or not _KEY_ID_PATTERN.fullmatch(key_id):
            raise TrainerTrustConfigurationError(
                f"强化组件可信公钥编号无效：{key_id or '-'}"
            )
        if not isinstance(encoded_key, str):
            raise TrainerTrustConfigurationError(
                f"强化组件可信公钥 {key_id} 必须是 Base64 字符串"
            )
        try:
            public_key = base64.b64decode(encoded_key, validate=True)
        except (TypeError, ValueError) as exc:
            raise TrainerTrustConfigurationError(
                f"强化组件可信公钥 {key_id} 不是有效的 Base64"
            ) from exc
        if len(public_key) != 32:
            raise TrainerTrustConfigurationError(
                f"强化组件可信公钥 {key_id} 必须是 32 字节 Ed25519 公钥"
            )
        normalized[key_id] = public_key
    return normalized


def trainer_trust_candidates() -> tuple[Path, ...]:
    """Return implicit trust-file locations in deterministic priority order."""

    executable_directory = Path(sys.executable).resolve().parent
    executable = executable_directory / TRAINER_TRUST_FILE_NAME
    candidates = [executable]
    if sys.platform.startswith("linux"):
        configured_root = os.environ.get("XDG_CONFIG_HOME", "").strip()
        config_root = (
            Path(configured_root).expanduser()
            if configured_root
            else Path.home() / ".config"
        )
        candidates.append(config_root / "intdemo-client" / TRAINER_TRUST_FILE_NAME)
        # UOS packages keep public configuration at the package root while the
        # PyInstaller executable lives in ``app/``.  Limit this compatibility
        # location to that exact directory name so ordinary source/venv runs
        # never search an unrelated parent.
        if executable_directory.name == "app":
            candidates.append(executable_directory.parent / TRAINER_TRUST_FILE_NAME)
    candidates.append(Path(__file__).resolve().with_name(TRAINER_TRUST_FILE_NAME))
    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        identity = os.path.normcase(str(candidate))
        if identity not in seen:
            seen.add(identity)
            unique.append(candidate)
    return tuple(unique)


def load_trainer_trusted_public_keys(
    path: str | Path | None = None,
) -> dict[str, bytes] | None:
    """Load trusted keys, returning ``None`` only when no implicit file exists.

    An explicit function argument or ``INTDEMO_TRAINER_TRUST_FILE`` is strict:
    a missing, non-file, or invalid path is always an error.
    """

    configured = path
    if configured is None:
        configured = os.environ.get(TRAINER_TRUST_ENVIRONMENT)
    if configured is not None:
        configured_text = str(configured).strip()
        if not configured_text:
            raise TrainerTrustConfigurationError(
                f"{TRAINER_TRUST_ENVIRONMENT} 不能为空"
            )
        candidate = Path(configured_text).expanduser()
        if not candidate.is_file():
            raise TrainerTrustConfigurationError(
                f"找不到强化组件可信公钥配置：{candidate}"
            )
        return _read_trust_file(candidate.resolve())

    for candidate in trainer_trust_candidates():
        if candidate.is_file():
            return _read_trust_file(candidate)
    return None


def load_trainer_signature_verifier(
    path: str | Path | None = None,
) -> Ed25519ManifestVerifier | None:
    keys = load_trainer_trusted_public_keys(path)
    return Ed25519ManifestVerifier(keys) if keys is not None else None


def create_trainer_component_manager(
    *,
    trust_file: str | Path | None = None,
    **manager_options,
) -> TrainerComponentManager:
    """Create the manager with the dedicated trainer signature verifier."""

    verifier = load_trainer_signature_verifier(trust_file)
    return TrainerComponentManager(
        signature_verifier=verifier,
        **manager_options,
    )


__all__ = [
    "TRAINER_TRUST_ENVIRONMENT",
    "TRAINER_TRUST_FILE_NAME",
    "TRAINER_TRUST_SCHEMA_VERSION",
    "TrainerTrustConfigurationError",
    "create_trainer_component_manager",
    "load_trainer_signature_verifier",
    "load_trainer_trusted_public_keys",
    "trainer_trust_candidates",
]

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from .config import get_settings

router = APIRouter(tags=["updates"])

_CHANNELS = {"test", "stable"}
_VERSION_PATTERN = re.compile(r"^\d+(?:\.\d+){1,3}$")
_UPDATER_USER_AGENT = re.compile(
    r"^IntDemoUpdater/(?P<version>\d+(?:\.\d+){1,3})(?:\s|$)",
    re.IGNORECASE,
)
_PACKAGE_FIELDS = ("installer_path", "sha256", "size")
_PACKAGE_KEYS = (*_PACKAGE_FIELDS, "full", "delta", "deltas")
_PLATFORMS = {"windows-x86_64", "linux-aarch64"}
_LEGACY_PLATFORM = "windows-x86_64"
_UOS_DELTA_FORMAT = "uos-deb-xdelta-v1"
_CAPABILITY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def request_client_version(request: Request) -> str | None:
    explicit = request.headers.get("x-intdemo-version", "").strip()
    if _VERSION_PATTERN.fullmatch(explicit):
        return explicit
    match = _UPDATER_USER_AGENT.match(request.headers.get("user-agent", "").strip())
    return match.group("version") if match else None


def request_client_platform(request: Request) -> str:
    explicit = request.headers.get("x-intdemo-platform", "").strip().casefold()
    return explicit if explicit in _PLATFORMS else _LEGACY_PLATFORM


def request_update_capabilities(request: Request) -> frozenset[str]:
    raw = request.headers.get("x-intdemo-update-capabilities", "")
    if len(raw) > 512:
        return frozenset()
    values = {
        item.strip().casefold()
        for item in raw.split(",")
        if item.strip()
    }
    return frozenset(
        item for item in values if _CAPABILITY_PATTERN.fullmatch(item)
    )


def select_platform_manifest(
    payload: dict[str, Any],
    platform_key: str,
) -> dict[str, Any] | None:
    platforms = payload.get("platforms")
    if not isinstance(platforms, dict):
        return dict(payload) if platform_key == _LEGACY_PLATFORM else None
    platform_payload = platforms.get(platform_key)
    if not isinstance(platform_payload, dict):
        return None
    selected = dict(payload)
    for key in (*_PACKAGE_KEYS, "primary_kind", "primary_from_version"):
        selected.pop(key, None)
    selected.update(platform_payload)
    selected["selected_platform"] = platform_key
    return selected


def matching_delta(payload: dict[str, Any], current_version: str | None) -> dict | None:
    if current_version is None:
        return None
    candidates = payload.get("deltas")
    if candidates is None and isinstance(payload.get("delta"), dict):
        candidates = [payload["delta"]]
    if not isinstance(candidates, list):
        return None
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        from_versions = candidate.get("from_versions")
        if from_versions is None:
            from_versions = [candidate.get("from_version")]
        if not isinstance(from_versions, list):
            continue
        normalized = {str(item or "").strip() for item in from_versions}
        if current_version in normalized:
            return candidate
    return None


def select_manifest_package(
    payload: dict[str, Any],
    current_version: str | None,
    *,
    platform_key: str = _LEGACY_PLATFORM,
    capabilities: frozenset[str] = frozenset(),
) -> tuple[dict[str, Any], str]:
    """Flatten the exact package into schema-v1 fields for legacy clients."""

    selected = dict(payload)
    full = payload.get("full") if isinstance(payload.get("full"), dict) else payload
    delta = matching_delta(payload, current_version)
    if platform_key == "linux-aarch64" and (
        delta is None
        or str(delta.get("format") or "").strip().casefold()
        != _UOS_DELTA_FORMAT
        or _UOS_DELTA_FORMAT not in capabilities
    ):
        delta = None
        # Do not expose optional UOS patch metadata to old clients. v1.1.0,
        # v1.1.1 and v1.2.0 therefore see the same full-DEB response shape
        # they already understand even when their version matches a patch.
        selected.pop("delta", None)
        selected.pop("deltas", None)
        platforms = selected.get("platforms")
        if isinstance(platforms, dict):
            sanitized_platforms = dict(platforms)
            nested_platform = sanitized_platforms.get(platform_key)
            if isinstance(nested_platform, dict):
                sanitized_platform = dict(nested_platform)
                sanitized_platform.pop("delta", None)
                sanitized_platform.pop("deltas", None)
                sanitized_platforms[platform_key] = sanitized_platform
                selected["platforms"] = sanitized_platforms
    package = delta if delta is not None else full
    kind = "delta" if delta is not None else "full"

    for field in _PACKAGE_FIELDS:
        selected[field] = package.get(field)
    selected["primary_kind"] = kind
    if kind == "delta":
        selected["primary_from_version"] = current_version
    else:
        selected.pop("primary_from_version", None)
    return selected, kind


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="update manifest not found") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=503, detail="update manifest unavailable") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=503, detail="update manifest is invalid")
    return payload


@router.get("/updates/{channel}.json", include_in_schema=False)
def update_manifest(channel: str, request: Request) -> Response:
    if channel not in _CHANNELS:
        raise HTTPException(status_code=404, detail="update channel not found")
    settings = get_settings()
    payload = _load_manifest(settings.updates_dir / f"{channel}.json")
    current_version = request_client_version(request)
    platform_key = request_client_platform(request)
    capabilities = request_update_capabilities(request)
    if payload.get("paused") is True:
        headers = {
            "Cache-Control": "no-store",
            "X-IntDemo-Update-Package": "paused",
            "X-IntDemo-Update-Distribution": "paused",
            "X-IntDemo-Platform": platform_key,
        }
        if current_version is None:
            return Response(status_code=204, headers=headers)
        headers["X-IntDemo-Client-Version"] = current_version
        return JSONResponse(
            {
                "schema_version": 1,
                "channel": channel,
                "version": current_version,
                "paused": True,
                "paused_version": str(payload.get("paused_version") or ""),
                "selected_platform": platform_key,
            },
            headers=headers,
        )
    platform_manifest = select_platform_manifest(payload, platform_key)
    if platform_manifest is None:
        return Response(
            status_code=204,
            headers={
                "Cache-Control": "no-store",
                "X-IntDemo-Update-Package": "unavailable",
                "X-IntDemo-Platform": platform_key,
            },
        )
    selected, package_kind = select_manifest_package(
        platform_manifest,
        current_version,
        platform_key=platform_key,
        capabilities=capabilities,
    )
    headers = {
        "Cache-Control": "no-store",
        "X-IntDemo-Update-Package": package_kind,
        "X-IntDemo-Platform": platform_key,
    }
    if current_version is not None:
        headers["X-IntDemo-Client-Version"] = current_version
    return JSONResponse(selected, headers=headers)

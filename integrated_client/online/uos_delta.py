from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from ..config import APP_VERSION, get_data_dir

UOS_DELTA_FORMAT = "uos-deb-xdelta-v1"
UOS_DELTA_ALGORITHM = "xdelta3"

_DEB_NAME_PATTERN = re.compile(
    r"^IntDemo-UOS-arm64-(?P<version>\d+\.\d+\.\d+)\.deb$",
    re.IGNORECASE,
)
_LOCK_STALE_SECONDS = 60 * 60
_PATCH_TIMEOUT_SECONDS = 180
_DISK_RESERVE_BYTES = 128 * 1024 * 1024


class UosDeltaError(RuntimeError):
    pass


class UosDeltaCancelled(UosDeltaError):
    pass


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _safe_deb_name(value: str, *, version: str | None = None) -> str:
    name = str(value or "").strip()
    match = _DEB_NAME_PATTERN.fullmatch(name)
    if not match or Path(name).name != name:
        raise UosDeltaError("目标 UOS DEB 文件名无效")
    if version is not None and match.group("version") != version:
        raise UosDeltaError("目标 UOS DEB 文件名与版本不一致")
    return name


def bundled_xdelta3_path(explicit: str | Path | None = None) -> Path:
    configured = str(
        explicit or os.environ.get("INTDEMO_XDELTA3_PATH", "")
    ).strip()
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured).expanduser())

    bundle_root = str(getattr(sys, "_MEIPASS", "") or "").strip()
    if bundle_root:
        candidates.append(Path(bundle_root) / "tools" / "xdelta3")

    executable_root = Path(sys.executable).resolve().parent
    candidates.extend(
        [
            executable_root / "_internal" / "tools" / "xdelta3",
            executable_root / "tools" / "xdelta3",
        ]
    )
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if resolved.is_file() and os.access(resolved, os.X_OK):
            return resolved
    raise UosDeltaError("客户端内置的 ARM64 xdelta3 补丁引擎不可用")


class UosDeltaCache:
    def __init__(
        self,
        data_dir: str | Path | None = None,
        *,
        current_version: str | None = None,
        engine_path: str | Path | None = None,
    ):
        self.data_dir = Path(data_dir or get_data_dir()).expanduser().resolve()
        self.current_version = str(current_version or APP_VERSION).strip()
        self.engine_path = engine_path
        self.root = self.data_dir / "updates"
        self.base_dir = self.root / "base"
        self.pending_dir = self.root / "pending"
        self.patches_dir = self.root / "patches"
        self.base_metadata_path = self.base_dir / "metadata.json"
        self.pending_metadata_path = self.pending_dir / "pending-install.json"
        self.lock_path = self.root / ".uos-delta.lock"

    def _ensure_directories(self) -> None:
        for directory in (
            self.root,
            self.base_dir,
            self.pending_dir,
            self.patches_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
            try:
                directory.chmod(0o700)
            except OSError:
                pass

    @staticmethod
    def _read_json(path: Path) -> dict | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _write_json_atomic(path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            try:
                temporary.chmod(0o600)
            except OSError:
                pass
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _verified_file(path: Path, *, size: int, sha256: str) -> bool:
        try:
            return (
                path.is_file()
                and path.stat().st_size == int(size)
                and file_sha256(path) == str(sha256).casefold()
            )
        except (OSError, ValueError):
            return False

    def confirm_pending(self) -> bool:
        """Promote a downloaded DEB only after its target version starts."""
        metadata = self._read_json(self.pending_metadata_path)
        if not metadata or str(metadata.get("version") or "") != self.current_version:
            return False
        try:
            name = _safe_deb_name(
                str(metadata.get("file") or ""),
                version=self.current_version,
            )
            size = int(metadata.get("size"))
            digest = str(metadata.get("sha256") or "").casefold()
        except (TypeError, ValueError, UosDeltaError):
            return False
        pending = self.pending_dir / name
        if not self._verified_file(pending, size=size, sha256=digest):
            return False

        self._ensure_directories()
        target = self.base_dir / name
        os.replace(pending, target)
        self._write_json_atomic(
            self.base_metadata_path,
            {
                "schema_version": 1,
                "platform": "linux-aarch64",
                "version": self.current_version,
                "file": name,
                "size": size,
                "sha256": digest,
                "installed_confirmed_at": _utc_now(),
            },
        )
        self.pending_metadata_path.unlink(missing_ok=True)
        self._remove_other_debs(self.base_dir, keep=target)
        self._remove_other_debs(self.pending_dir)
        self._remove_files(self.patches_dir)
        return True

    @staticmethod
    def _remove_other_debs(directory: Path, keep: Path | None = None) -> None:
        keep_resolved = keep.resolve() if keep is not None else None
        for candidate in directory.glob("*.deb"):
            try:
                if keep_resolved is not None and candidate.resolve() == keep_resolved:
                    continue
                candidate.unlink(missing_ok=True)
            except OSError:
                continue

    @staticmethod
    def _remove_files(directory: Path) -> None:
        for candidate in directory.iterdir():
            if candidate.is_file():
                try:
                    candidate.unlink(missing_ok=True)
                except OSError:
                    continue

    def locate_base(self, *, version: str, size: int, sha256: str) -> Path | None:
        expected_name = f"IntDemo-UOS-arm64-{version}.deb"
        metadata = self._read_json(self.base_metadata_path)
        if metadata and str(metadata.get("version") or "") == version:
            try:
                name = _safe_deb_name(str(metadata.get("file") or ""), version=version)
            except UosDeltaError:
                name = ""
            if name:
                candidate = self.base_dir / name
                if self._verified_file(candidate, size=size, sha256=sha256):
                    return candidate

        # v1.2.0's updater already retained its downloaded DEB in updates/.
        # Adopt that exact file only after it matches the new manifest's
        # authoritative base size and hash. Manual installations simply miss
        # this candidate and safely fall back to a full update.
        for candidate in (
            self.base_dir / expected_name,
            self.root / expected_name,
        ):
            if not self._verified_file(candidate, size=size, sha256=sha256):
                continue
            self._ensure_directories()
            target = self.base_dir / expected_name
            if candidate != target:
                os.replace(candidate, target)
            self._write_json_atomic(
                self.base_metadata_path,
                {
                    "schema_version": 1,
                    "platform": "linux-aarch64",
                    "version": version,
                    "file": expected_name,
                    "size": int(size),
                    "sha256": str(sha256).casefold(),
                    "installed_confirmed_at": _utc_now(),
                },
            )
            self._remove_other_debs(self.base_dir, keep=target)
            return target
        return None

    def pending_path(self, *, name: str, version: str) -> Path:
        self._ensure_directories()
        return self.pending_dir / _safe_deb_name(name, version=version)

    def patch_path(self, name: str) -> Path:
        self._ensure_directories()
        normalized = str(name or "").strip()
        if (
            Path(normalized).name != normalized
            or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._-]*\.intdelta",
                normalized,
                re.IGNORECASE,
            )
        ):
            raise UosDeltaError("UOS 增量包文件名无效")
        return self.patches_dir / normalized

    def register_pending(
        self,
        path: Path,
        *,
        version: str,
        size: int,
        sha256: str,
    ) -> Path:
        self._ensure_directories()
        path = path.resolve()
        try:
            path.relative_to(self.pending_dir.resolve())
        except ValueError as exc:
            raise UosDeltaError("待安装 DEB 不在受控缓存目录") from exc
        name = _safe_deb_name(path.name, version=version)
        if not self._verified_file(path, size=size, sha256=sha256):
            raise UosDeltaError("待安装 UOS DEB 校验失败")
        self._remove_other_debs(self.pending_dir, keep=path)
        self._write_json_atomic(
            self.pending_metadata_path,
            {
                "schema_version": 1,
                "platform": "linux-aarch64",
                "version": version,
                "file": name,
                "size": int(size),
                "sha256": str(sha256).casefold(),
                "prepared_at": _utc_now(),
            },
        )
        return path

    @contextmanager
    def update_lock(self):
        self.root.mkdir(parents=True, exist_ok=True)
        for attempt in range(2):
            try:
                descriptor = os.open(
                    self.lock_path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
            except FileExistsError as exc:
                try:
                    stale = time.time() - self.lock_path.stat().st_mtime
                except OSError:
                    stale = 0
                if attempt == 0 and stale > _LOCK_STALE_SECONDS:
                    self.lock_path.unlink(missing_ok=True)
                    continue
                raise UosDeltaError("另一项 UOS 更新任务正在运行") from exc
            else:
                with os.fdopen(descriptor, "w", encoding="ascii") as stream:
                    stream.write(f"{os.getpid()}\n{time.time():.0f}\n")
                break
        try:
            yield
        finally:
            self.lock_path.unlink(missing_ok=True)

    def reconstruct(
        self,
        *,
        base_path: Path,
        patch_path: Path,
        target_name: str,
        target_version: str,
        target_size: int,
        target_sha256: str,
        cancelled_callback=None,
        state_callback=None,
    ) -> Path:
        target = self.pending_path(name=target_name, version=target_version)
        partial = target.with_suffix(target.suffix + ".part")
        required = int(target_size) + patch_path.stat().st_size + _DISK_RESERVE_BYTES
        if shutil.disk_usage(self.pending_dir).free < required:
            raise UosDeltaError("磁盘可用空间不足，无法重建完整 UOS 安装包")
        engine = bundled_xdelta3_path(self.engine_path)

        def ensure_not_cancelled() -> None:
            if cancelled_callback is not None and cancelled_callback():
                raise UosDeltaCancelled("UOS 增量更新已停止")

        with self.update_lock():
            ensure_not_cancelled()
            partial.unlink(missing_ok=True)
            if state_callback is not None:
                state_callback("reconstructing", "正在重建完整 UOS 安装包…")
            started_at = time.monotonic()
            process: subprocess.Popen | None = None
            try:
                with tempfile.TemporaryFile() as error_log:
                    process = subprocess.Popen(
                        [
                            str(engine),
                            "-d",
                            "-s",
                            str(base_path),
                            str(patch_path),
                            str(partial),
                        ],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=error_log,
                        shell=False,
                    )
                    while process.poll() is None:
                        if cancelled_callback is not None and cancelled_callback():
                            process.terminate()
                            try:
                                process.wait(timeout=5)
                            except subprocess.TimeoutExpired:
                                process.kill()
                            raise UosDeltaCancelled("UOS 增量更新已停止")
                        if time.monotonic() - started_at > _PATCH_TIMEOUT_SECONDS:
                            process.kill()
                            process.wait(timeout=5)
                            raise UosDeltaError("UOS 增量包重建超过 180 秒")
                        time.sleep(0.1)
                    if process.returncode != 0:
                        error_log.seek(0, os.SEEK_END)
                        length = error_log.tell()
                        error_log.seek(max(0, length - 4096))
                        detail = (
                            error_log.read()
                            .decode("utf-8", errors="replace")
                            .strip()
                        )
                        suffix = f"：{detail}" if detail else ""
                        raise UosDeltaError(f"UOS 增量包重建失败{suffix}")
                ensure_not_cancelled()
                if state_callback is not None:
                    state_callback("verifying_target", "正在校验重建后的 UOS 安装包…")
                if partial.stat().st_size != int(target_size):
                    raise UosDeltaError("重建后的 UOS DEB 大小不匹配")
                if file_sha256(partial) != str(target_sha256).casefold():
                    raise UosDeltaError("重建后的 UOS DEB SHA-256 校验失败")
                os.replace(partial, target)
                self.register_pending(
                    target,
                    version=target_version,
                    size=target_size,
                    sha256=target_sha256,
                )
                patch_path.unlink(missing_ok=True)
                return target
            except OSError as exc:
                raise UosDeltaError(f"无法执行 UOS 增量重建：{exc}") from exc
            finally:
                if process is not None and process.poll() is None:
                    process.kill()
                partial.unlink(missing_ok=True)

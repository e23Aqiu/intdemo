from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import platform
import re
import shlex
import shutil
import ssl
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)
REQUEST_ID_PATTERN = re.compile(r"^[0-9A-Za-z._-]{12,80}$")
REMOTE_HOST_PATTERN = re.compile(r"^(?!-)[A-Za-z0-9._@:-]+$")
REMOTE_PATH_PATTERN = re.compile(r"^/[A-Za-z0-9._/-]+$")
GITHUB_REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
GIT_REMOTE_PATTERN = re.compile(r"^(?!-)[A-Za-z0-9._-]+$")
REQUEST_FILE = "windows-build-request.json"
RESULT_FILE = "windows-build-result.json"
UOS_RESULT_FILE = "uos-build-result.json"
UOS_DELTA_BASE_FILE = "uos-delta-base.json"
WINDOWS_DELTA_BASE_FILE = "windows-delta-base.json"
SOURCE_VERSION_TARGETING_CAPABILITY = "source-version-targeting-v1"
UOS_LAYERED_UPDATE_CAPABILITY = "uos-layered-v1"
UOS_FILE_UPDATE_CAPABILITY = "uos-file-update-v2"
MAX_ELIGIBLE_CLIENT_VERSIONS = 32
WINDOWS_WORKFLOW_NAME = "Windows release build"
WINDOWS_TAG_PREFIX = "intdemo-windows/v"
GITHUB_UNAVAILABLE_EXIT = 20
SSH_CONNECTION_OPTIONS = (
    "BatchMode=yes",
    "ConnectTimeout=20",
    "ServerAliveInterval=15",
    "ServerAliveCountMax=20",
    "TCPKeepAlive=yes",
)
SSH_COMMAND_TIMEOUT_SECONDS = 60.0
SSH_PROGRESS_QUERY_TIMEOUT_SECONDS = 30.0
SSH_HASH_TIMEOUT_SECONDS = 600.0
SFTP_UPLOAD_ATTEMPTS = 6
SFTP_PROGRESS_INTERVAL_SECONDS = 10.0
SFTP_STALL_TIMEOUT_SECONDS = 300.0
SFTP_RETRY_DELAYS_SECONDS = (2, 5, 10, 20, 30)
UPDATE_HTTP_RETRY_DELAYS_SECONDS = (1, 2, 4)


class ReleaseTaskError(RuntimeError):
    pass


class GithubUnavailable(ReleaseTaskError):
    pass


class CommandFailure(ReleaseTaskError):
    def __init__(
        self,
        arguments: list[str],
        returncode: int,
        stdout: str = "",
        stderr: str = "",
    ):
        self.arguments = arguments
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        detail = (stderr or stdout).strip()
        suffix = f"\n{detail}" if detail else ""
        super().__init__(
            f"命令执行失败（{returncode}）：{' '.join(arguments)}{suffix}"
        )


def repo_root() -> Path:
    return REPOSITORY_ROOT


def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def version_key(value: str) -> tuple[int, int, int]:
    normalized = str(value or "").strip()
    if not VERSION_PATTERN.fullmatch(normalized):
        raise ReleaseTaskError(f"版本号必须使用 x.y.z 格式：{normalized or '-'}")
    return tuple(int(item) for item in normalized.split("."))


def normalize_eligible_client_versions(
    values: list[str] | tuple[str, ...] | None,
    *,
    target_version: str,
) -> tuple[str, ...]:
    normalized: list[str] = []
    target_key = version_key(target_version)
    for value in values or ():
        version = str(value or "").strip()
        candidate_key = version_key(version)
        if candidate_key >= target_key:
            raise ReleaseTaskError(
                f"定向更新来源版本必须低于目标版本：{version}"
            )
        if version in normalized:
            raise ReleaseTaskError(f"定向更新客户端版本重复：{version}")
        normalized.append(version)
    if len(normalized) > MAX_ELIGIBLE_CLIENT_VERSIONS:
        raise ReleaseTaskError(
            f"定向更新最多允许 {MAX_ELIGIBLE_CLIENT_VERSIONS} 个客户端版本"
        )
    return tuple(normalized)


def normalize_base_url(value: str) -> str:
    normalized = str(value or "").strip().rstrip("/")
    if not normalized or any(character.isspace() for character in normalized):
        raise ReleaseTaskError("服务地址不能为空或包含空白字符")
    try:
        parsed = urlsplit(normalized)
        port = parsed.port
    except ValueError as exc:
        raise ReleaseTaskError(f"服务地址不是有效的 HTTPS URL：{normalized}") from exc
    if (
        parsed.scheme.casefold() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ReleaseTaskError(
            "服务地址必须是无账号、查询参数和片段的 HTTPS 基础地址"
        )
    if port is not None and not 1 <= port <= 65535:
        raise ReleaseTaskError("服务地址端口必须位于 1 到 65535 之间")
    return normalized


def safe_remote_path(value: str) -> str:
    normalized = str(value or "").strip().rstrip("/")
    if (
        normalized == "/"
        or not REMOTE_PATH_PATTERN.fullmatch(normalized)
        or any(part in {"", ".", ".."} for part in normalized.split("/")[1:])
    ):
        raise ReleaseTaskError("远程更新目录必须是安全的非根绝对 Linux 路径")
    return normalized


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def json_bytes(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def write_bytes_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(payload)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    write_bytes_atomic(path, json_bytes(payload))


def copy_atomic(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def run_command(
    arguments: list[str],
    *,
    cwd: Path,
    capture: bool = False,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            arguments,
            cwd=str(cwd),
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=capture,
            env=env,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        seconds = timeout if timeout is not None else exc.timeout
        raise ReleaseTaskError(
            f"命令执行超时（{seconds:g} 秒）：{' '.join(arguments)}"
        ) from exc
    except OSError as exc:
        raise ReleaseTaskError(f"无法启动命令 {arguments[0]}：{exc}") from exc
    if result.returncode != 0:
        raise CommandFailure(
            arguments,
            result.returncode,
            result.stdout or "",
            result.stderr or "",
        )
    return result


def capture_command(
    arguments: list[str],
    *,
    cwd: Path,
    timeout: float | None = None,
) -> str:
    return run_command(
        arguments,
        cwd=cwd,
        capture=True,
        timeout=timeout,
    ).stdout.strip()


def git(root: Path, *arguments: str) -> str:
    return capture_command(["git", *arguments], cwd=root)


def current_version(root: Path) -> str:
    path = root / "integrated_client" / "config.py"
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ReleaseTaskError(f"无法读取项目版本：{path}") from exc
    match = re.search(
        r'(?m)^APP_VERSION\s*=\s*"(?P<version>\d+\.\d+\.\d+)"\s*$',
        content,
    )
    if not match:
        raise ReleaseTaskError(f"无法识别项目版本：{path}")
    return match.group("version")


def source_state(
    root: Path,
    *,
    require_clean: bool = True,
    allow_detached: bool = False,
) -> tuple[str, str]:
    branch = git(root, "rev-parse", "--abbrev-ref", "HEAD")
    if not branch or (branch == "HEAD" and not allow_detached):
        raise ReleaseTaskError("构建任务要求当前处于具名 Git 分支")
    commit = git(root, "rev-parse", "HEAD").casefold()
    if not COMMIT_PATTERN.fullmatch(commit):
        raise ReleaseTaskError("无法读取当前完整 Git 提交")
    if require_clean and git(root, "status", "--porcelain=v1"):
        raise ReleaseTaskError("构建和发布要求 Git 工作区无未提交变更")
    return branch, commit


def artifact_descriptor(path: Path, file_name: str) -> dict[str, Any]:
    if not path.is_file():
        raise ReleaseTaskError(f"缺少构建产物：{path}")
    return {
        "file": file_name,
        "size": path.stat().st_size,
        "sha256": sha256(path),
    }


def validate_descriptor(descriptor: Any, label: str) -> dict[str, Any]:
    if not isinstance(descriptor, dict):
        raise ReleaseTaskError(f"{label}描述格式无效")
    file_name = str(descriptor.get("file") or "")
    pure = PurePosixPath(file_name)
    if (
        not file_name
        or pure.is_absolute()
        or ".." in pure.parts
        or "\\" in file_name
        or pure.as_posix() != file_name
    ):
        raise ReleaseTaskError(f"{label}文件名不安全：{file_name}")
    try:
        size = int(descriptor.get("size"))
    except (TypeError, ValueError) as exc:
        raise ReleaseTaskError(f"{label}大小无效") from exc
    digest = str(descriptor.get("sha256") or "").casefold()
    if size < 0 or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ReleaseTaskError(f"{label}大小或 SHA-256 无效")
    return {"file": file_name, "size": size, "sha256": digest}


def verify_file(root: Path, descriptor: Any, label: str) -> Path:
    normalized = validate_descriptor(descriptor, label)
    path = root / PurePosixPath(normalized["file"])
    if not path.is_file():
        raise ReleaseTaskError(f"{label}不存在：{path}")
    if path.stat().st_size != normalized["size"]:
        raise ReleaseTaskError(f"{label}大小不匹配：{path}")
    if sha256(path) != normalized["sha256"]:
        raise ReleaseTaskError(f"{label} SHA-256 不匹配：{path}")
    return path


def extract_zip_safely(archive: Path, destination: Path) -> None:
    try:
        with zipfile.ZipFile(archive) as source:
            _validated_zip_members(source)
            source.extractall(destination)
    except (OSError, zipfile.BadZipFile) as exc:
        raise ReleaseTaskError(f"无法读取 ZIP：{archive}") from exc


def _validated_zip_members(
    archive: zipfile.ZipFile,
) -> dict[str, zipfile.ZipInfo]:
    members: dict[str, zipfile.ZipInfo] = {}
    for info in archive.infolist():
        name = info.filename
        pure = PurePosixPath(name)
        mode = (info.external_attr >> 16) & 0o170000
        if (
            not name
            or name in members
            or pure.is_absolute()
            or ".." in pure.parts
            or "\\" in name
            or ":" in name
            or pure.as_posix() != name.rstrip("/")
            or mode == 0o120000
        ):
            raise ReleaseTaskError(f"ZIP 包含不安全路径：{name}")
        members[name] = info
    return members


def make_zip(source_root: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        with zipfile.ZipFile(
            temporary,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
        ) as archive:
            for path in sorted(source_root.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(source_root).as_posix())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def ca_fingerprint(path_value: str) -> str:
    normalized = str(path_value or "").strip()
    if not normalized:
        return ""
    path = Path(normalized).expanduser().resolve()
    if not path.is_file():
        raise ReleaseTaskError(f"CA 根证书不存在：{path}")
    return sha256(path)


def validate_request(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or int(payload.get("schema_version") or 0) != 1:
        raise ReleaseTaskError("Windows 构建任务版本不受支持")
    request_id = str(payload.get("request_id") or "")
    version = str(payload.get("version") or "")
    source_commit = str(payload.get("source_commit") or "").casefold()
    source_branch = str(payload.get("source_branch") or "")
    repository_url = str(payload.get("repository_url") or "")
    base_url = normalize_base_url(str(payload.get("base_url") or ""))
    channel = str(payload.get("channel") or "")
    delta_version = str(payload.get("delta_from_version") or "")
    if not isinstance(payload.get("build_portable"), bool):
        raise ReleaseTaskError("Windows 构建任务的便携包选项无效")
    build_portable = payload["build_portable"]
    inputs = payload.get("inputs")
    if not REQUEST_ID_PATTERN.fullmatch(request_id):
        raise ReleaseTaskError("Windows 构建任务 ID 无效")
    version_key(version)
    if not COMMIT_PATTERN.fullmatch(source_commit):
        raise ReleaseTaskError("Windows 构建任务缺少完整源提交")
    if (
        not source_branch
        or source_branch.startswith("-")
        or ".." in source_branch
        or not re.fullmatch(r"[A-Za-z0-9._/-]+", source_branch)
    ):
        raise ReleaseTaskError("Windows 构建任务源分支无效")
    if not repository_url or any(character.isspace() for character in repository_url):
        raise ReleaseTaskError("Windows 构建任务仓库地址无效")
    if channel not in {"test", "stable"}:
        raise ReleaseTaskError("Windows 构建任务通道无效")
    if not isinstance(inputs, dict):
        raise ReleaseTaskError("Windows 构建任务缺少 inputs")
    allowed_inputs = {"ca_bundle", "baseline_snapshot"}
    if not set(inputs).issubset(allowed_inputs) or not {
        "ca_bundle",
        "baseline_snapshot",
    }.issubset(inputs):
        raise ReleaseTaskError("Windows 构建任务 inputs 集合无效")
    ca_input = inputs.get("ca_bundle")
    baseline_input = inputs.get("baseline_snapshot")
    if ca_input is not None:
        validate_descriptor(ca_input, "CA 根证书")
    try:
        host_is_ip = ipaddress.ip_address(urlsplit(base_url).hostname or "") is not None
    except ValueError:
        host_is_ip = False
    if host_is_ip and ca_input is None:
        raise ReleaseTaskError("IP 服务地址的 Windows 构建任务必须附带 CA 根证书")
    if delta_version:
        if version_key(delta_version) >= version_key(version):
            raise ReleaseTaskError("Windows 增量来源版本必须低于目标版本")
        validate_descriptor(baseline_input, "Windows 增量基线快照")
    elif baseline_input is not None:
        raise ReleaseTaskError("未请求增量包却附带了基线快照")
    return {
        **payload,
        "request_id": request_id,
        "version": version,
        "source_commit": source_commit,
        "source_branch": source_branch,
        "repository_url": repository_url,
        "base_url": base_url,
        "channel": channel,
        "delta_from_version": delta_version,
        "build_portable": build_portable,
        "inputs": inputs,
    }


def validate_snapshot(path: Path, version: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseTaskError(f"无法读取 Windows 发布快照：{path}") from exc
    if (
        not isinstance(payload, dict)
        or int(payload.get("schema_version") or 0) != 1
        or payload.get("version") != version
        or not isinstance(payload.get("files"), list)
        or not payload["files"]
    ):
        raise ReleaseTaskError("Windows 发布快照版本或格式无效")
    seen: set[str] = set()
    for entry in payload["files"]:
        if not isinstance(entry, dict):
            raise ReleaseTaskError("Windows 发布快照文件描述无效")
        relative = str(entry.get("path") or "")
        pure = PurePosixPath(relative)
        try:
            size = int(entry.get("size"))
        except (TypeError, ValueError) as exc:
            raise ReleaseTaskError("Windows 发布快照文件大小无效") from exc
        digest = str(entry.get("sha256") or "").casefold()
        if (
            not relative
            or relative in seen
            or pure.is_absolute()
            or ".." in pure.parts
            or "\\" in relative
            or ":" in relative
            or pure.as_posix() != relative
            or size < 0
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
        ):
            raise ReleaseTaskError(
                f"Windows 发布快照包含无效文件描述：{relative or '-'}"
            )
        seen.add(relative)
    return payload


def load_request_from_directory(directory: Path) -> tuple[dict[str, Any], Path, str]:
    request_path = directory / REQUEST_FILE
    try:
        payload = json.loads(request_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseTaskError(f"无法读取 Windows 构建任务：{request_path}") from exc
    request = validate_request(payload)
    for key, label in (
        ("ca_bundle", "CA 根证书"),
        ("baseline_snapshot", "Windows 增量基线快照"),
    ):
        descriptor = request["inputs"].get(key)
        if descriptor is not None:
            verify_file(directory, descriptor, label)
    return request, request_path, sha256(request_path)


def create_windows_request(
    root: Path,
    *,
    version: str,
    base_url: str,
    channel: str,
    ca_bundle: str,
    delta_from_version: str,
    build_portable: bool,
    github_remote: str,
) -> Path:
    version_key(version)
    normalized_url = normalize_base_url(base_url)
    if channel not in {"test", "stable"}:
        raise ReleaseTaskError("发布通道只能是 test 或 stable")
    if not GIT_REMOTE_PATTERN.fullmatch(str(github_remote or "")):
        raise ReleaseTaskError("GitHub Git 远程名称格式无效")
    branch, commit = source_state(root)
    if current_version(root) != version:
        raise ReleaseTaskError(
            f"项目当前版本是 {current_version(root)}，不是目标版本 {version}"
        )
    repository_url = git(root, "remote", "get-url", github_remote)
    request_id = (
        datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        + "-"
        + uuid.uuid4().hex[:10]
    )
    request_dir = root / "dist" / "windows-build-requests" / version / request_id

    with tempfile.TemporaryDirectory(prefix="intdemo-windows-request-") as temporary:
        staging = Path(temporary)
        inputs: dict[str, Any] = {
            "ca_bundle": None,
            "baseline_snapshot": None,
        }
        ca_value = str(ca_bundle or "").strip()
        if ca_value:
            source_ca = Path(ca_value).expanduser().resolve()
            if not source_ca.is_file():
                raise ReleaseTaskError(f"CA 根证书不存在：{source_ca}")
            target_ca = staging / "inputs" / "intdemo-ca-root.crt"
            target_ca.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_ca, target_ca)
            inputs["ca_bundle"] = artifact_descriptor(
                target_ca,
                "inputs/intdemo-ca-root.crt",
            )
        try:
            host_is_ip = ipaddress.ip_address(
                urlsplit(normalized_url).hostname or ""
            ) is not None
        except ValueError:
            host_is_ip = False
        if host_is_ip and inputs["ca_bundle"] is None:
            raise ReleaseTaskError("IP 服务地址必须选择 CA 根证书")

        delta_version = str(delta_from_version or "").strip()
        if delta_version:
            if version_key(delta_version) >= version_key(version):
                raise ReleaseTaskError("Windows 增量来源版本必须低于目标版本")
            baseline = root / "dist" / "release-snapshots" / f"{delta_version}.json"
            if not baseline.is_file():
                raise ReleaseTaskError(f"缺少实际发布的 Windows 基线快照：{baseline}")
            validate_snapshot(baseline, delta_version)
            target_baseline = staging / "inputs" / f"windows-{delta_version}.json"
            target_baseline.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(baseline, target_baseline)
            inputs["baseline_snapshot"] = artifact_descriptor(
                target_baseline,
                f"inputs/windows-{delta_version}.json",
            )

        request = validate_request(
            {
                "schema_version": 1,
                "request_id": request_id,
                "version": version,
                "source_commit": commit,
                "source_branch": branch,
                "repository_url": repository_url,
                "base_url": normalized_url,
                "channel": channel,
                "build_portable": bool(build_portable),
                "delta_from_version": delta_version,
                "created_at": utc_now(),
                "inputs": inputs,
            }
        )
        request_payload = json_bytes(request)
        (staging / REQUEST_FILE).write_bytes(request_payload)
        instructions = (
            "IntDemo Windows 真机构建任务\n\n"
            f"版本：{version}\n"
            f"源提交：{commit}\n"
            f"仓库：{repository_url}\n\n"
            "1. 在 Windows x64 电脑克隆或更新仓库。\n"
            f"2. 切换到提交 {commit}。\n"
            "3. 安装 Inno Setup 6。\n"
            "4. 在仓库根目录执行：\n"
            "   powershell -ExecutionPolicy Bypass -File "
            "scripts\\build-windows-request.ps1 -RequestArchive <本 ZIP 路径>\n"
            "5. 把生成的 windows-build-result ZIP 带回统信打包器导入。\n\n"
            "构建过程不会连接 IntDemo 程序服务器。\n"
        )
        (staging / "BUILD-WINDOWS.txt").write_text(instructions, encoding="utf-8")
        request_dir.mkdir(parents=True, exist_ok=False)
        archive_path = request_dir / f"windows-build-request-{request_id}.zip"
        make_zip(staging, archive_path)
        write_bytes_atomic(request_dir / REQUEST_FILE, request_payload)
    print(f"Windows 构建任务包：{archive_path}", flush=True)
    print(f"SHA-256：{sha256(archive_path)}", flush=True)
    return archive_path


def powershell_arguments(script: Path, arguments: list[str]) -> list[str]:
    return [
        "powershell.exe",
        "-NoLogo",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script),
        *arguments,
    ]


def build_windows_result(
    root: Path,
    *,
    request_archive: Path,
    output: Path,
    inno_compiler: str,
    run_tests: bool,
    github_run_id: str = "",
    github_run_url: str = "",
    tests_prevalidated: bool = False,
) -> Path:
    machine = platform.machine().casefold()
    if (
        os.name != "nt"
        or machine not in {"amd64", "x86_64"}
        or struct.calcsize("P") != 8
    ):
        raise ReleaseTaskError("Windows 结果包必须在 Windows x64 主机上构建")
    request_archive = request_archive.expanduser().resolve()
    if not request_archive.is_file():
        raise ReleaseTaskError(f"Windows 构建任务包不存在：{request_archive}")
    with tempfile.TemporaryDirectory(prefix="intdemo-windows-build-") as temporary:
        extracted = Path(temporary) / "request"
        extracted.mkdir()
        extract_zip_safely(request_archive, extracted)
        request, request_path, request_hash = load_request_from_directory(extracted)
        _branch, commit = source_state(root, allow_detached=True)
        if commit != request["source_commit"]:
            raise ReleaseTaskError(
                f"当前 Git 提交 {commit[:12]} 与任务 {request['source_commit'][:12]} 不一致"
            )
        if current_version(root) != request["version"]:
            raise ReleaseTaskError("当前项目版本与 Windows 构建任务不一致")

        baseline_descriptor = request["inputs"].get("baseline_snapshot")
        if baseline_descriptor is not None:
            baseline_source = verify_file(
                extracted,
                baseline_descriptor,
                "Windows 增量基线快照",
            )
            baseline_target = (
                root
                / "dist"
                / "release-snapshots"
                / f"{request['delta_from_version']}.json"
            )
            copy_atomic(baseline_source, baseline_target)

        python = root / ".venv" / "Scripts" / "python.exe"
        if not python.is_file():
            raise ReleaseTaskError(f"Windows 构建环境不存在：{python}")
        if run_tests:
            environment = os.environ.copy()
            environment["QT_QPA_PLATFORM"] = "offscreen"
            run_command(
                [str(python), "-m", "unittest", "discover", "-s", "tests"],
                cwd=root,
                env=environment,
            )

        ca_descriptor = request["inputs"].get("ca_bundle")
        ca_path = (
            verify_file(extracted, ca_descriptor, "CA 根证书")
            if ca_descriptor is not None
            else None
        )
        build_arguments = [
            "-BaseUrl",
            request["base_url"],
            "-Channel",
            request["channel"],
            "-Version",
            request["version"],
        ]
        if ca_path is not None:
            build_arguments.extend(["-CaBundle", str(ca_path)])
        if request["delta_from_version"]:
            build_arguments.extend(
                ["-DeltaFromVersion", request["delta_from_version"]]
            )
        if inno_compiler:
            build_arguments.extend(["-InnoCompiler", inno_compiler])
        build_script = root / "scripts" / (
            "build-releases.ps1"
            if request["build_portable"]
            else "build-installer.ps1"
        )
        run_command(
            powershell_arguments(build_script, build_arguments),
            cwd=root,
        )
        candidate_snapshot = (
            root
            / "dist"
            / "windows-build-candidates"
            / f"{request['version']}.json"
        )
        run_command(
            powershell_arguments(
                root / "scripts" / "save-release-snapshot.ps1",
                [
                    "-Version",
                    request["version"],
                    "-OutputPath",
                    str(candidate_snapshot),
                ],
            ),
            cwd=root,
        )

        version = request["version"]
        artifact_sources: dict[str, Path] = {
            "windows_installer": (
                root / "dist" / "installer" / f"IntDemoOnline-Setup-{version}.exe"
            ),
            "windows_snapshot": (
                candidate_snapshot
            ),
        }
        if request["delta_from_version"]:
            artifact_sources["windows_delta"] = (
                root
                / "dist"
                / "installer"
                / (
                    f"IntDemoOnline-Patch-{request['delta_from_version']}"
                    f"-to-{version}.exe"
                )
            )
        if request["build_portable"]:
            artifact_sources["windows_portable"] = (
                root / "dist" / "portable" / f"IntDemoOnline-Portable-{version}.zip"
            )
        result_names = {
            "windows_installer": f"IntDemoOnline-Setup-{version}.exe",
            "windows_snapshot": f"IntDemoOnline-Snapshot-{version}.json",
            "windows_delta": (
                f"IntDemoOnline-Patch-{request['delta_from_version']}-to-{version}.exe"
            ),
            "windows_portable": f"IntDemoOnline-Portable-{version}.zip",
        }

        result_root = Path(temporary) / "result"
        result_root.mkdir()
        shutil.copy2(request_path, result_root / REQUEST_FILE)
        for key, label in (
            ("ca_bundle", "CA 根证书"),
            ("baseline_snapshot", "Windows 增量基线快照"),
        ):
            descriptor = request["inputs"].get(key)
            if descriptor is None:
                continue
            source = verify_file(extracted, descriptor, label)
            target = result_root / PurePosixPath(descriptor["file"])
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        artifacts: dict[str, dict[str, Any]] = {}
        for key, source in artifact_sources.items():
            target = result_root / "artifacts" / result_names[key]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            artifacts[key] = artifact_descriptor(
                target,
                f"artifacts/{target.name}",
            )
        result = {
            "schema_version": 1,
            "request_id": request["request_id"],
            "request_sha256": request_hash,
            "version": version,
            "source_commit": commit,
            "base_url": request["base_url"],
            "channel": request["channel"],
            "delta_from_version": request["delta_from_version"],
            "build_portable": request["build_portable"],
            "tests_passed": bool(run_tests or tests_prevalidated),
            "built_at": utc_now(),
            "builder": {
                "kind": "github-actions" if github_run_id else "windows-manual",
                "os": platform.platform(),
                "machine": platform.machine(),
                "python": platform.python_version(),
                "github_run_id": str(github_run_id),
                "github_run_url": str(github_run_url),
            },
            "artifacts": artifacts,
        }
        write_json(result_root / RESULT_FILE, result)
        output = output.expanduser().resolve()
        make_zip(result_root, output)
    print(f"Windows 构建结果包：{output}", flush=True)
    print(f"SHA-256：{sha256(output)}", flush=True)
    return output


def build_local_windows_result(
    root: Path,
    *,
    version: str,
    base_url: str,
    channel: str,
    ca_bundle: str,
    delta_from_version: str,
    build_portable: bool,
    github_remote: str,
    inno_compiler: str,
    tests_prevalidated: bool,
) -> Path:
    request_archive = create_windows_request(
        root,
        version=version,
        base_url=base_url,
        channel=channel,
        ca_bundle=ca_bundle,
        delta_from_version=delta_from_version,
        build_portable=build_portable,
        github_remote=github_remote,
    )
    result_archive = (
        root
        / "dist"
        / "windows-build-results"
        / version
        / f"windows-build-result-{request_archive.parent.name}.zip"
    )
    build_windows_result(
        root,
        request_archive=request_archive,
        output=result_archive,
        inno_compiler=inno_compiler,
        run_tests=not tests_prevalidated,
        tests_prevalidated=tests_prevalidated,
    )
    return import_windows_result(
        root,
        result_archive=result_archive,
        version=version,
        base_url=base_url,
        channel=channel,
        ca_bundle=ca_bundle,
        delta_from_version=delta_from_version,
        build_portable=build_portable,
    )


def expected_ca_hash(request: dict[str, Any]) -> str:
    descriptor = request["inputs"].get("ca_bundle")
    return "" if descriptor is None else str(descriptor["sha256"])


def validate_result_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or int(payload.get("schema_version") or 0) != 1:
        raise ReleaseTaskError("Windows 构建结果版本不受支持")
    request_id = str(payload.get("request_id") or "")
    request_hash = str(payload.get("request_sha256") or "").casefold()
    source_commit = str(payload.get("source_commit") or "").casefold()
    artifacts = payload.get("artifacts")
    if not REQUEST_ID_PATTERN.fullmatch(request_id):
        raise ReleaseTaskError("Windows 构建结果任务 ID 无效")
    if not re.fullmatch(r"[0-9a-f]{64}", request_hash):
        raise ReleaseTaskError("Windows 构建结果缺少任务 SHA-256")
    if not COMMIT_PATTERN.fullmatch(source_commit):
        raise ReleaseTaskError("Windows 构建结果缺少完整源提交")
    if payload.get("tests_passed") is not True:
        raise ReleaseTaskError("Windows 构建结果没有通过完整客户端测试")
    if not isinstance(payload.get("build_portable"), bool):
        raise ReleaseTaskError("Windows 构建结果的便携包选项无效")
    if not isinstance(artifacts, dict):
        raise ReleaseTaskError("Windows 构建结果缺少 artifacts")
    return {
        **payload,
        "request_id": request_id,
        "request_sha256": request_hash,
        "source_commit": source_commit,
        "artifacts": artifacts,
    }


def detect_windows_result_portable(result_archive: str | Path) -> bool:
    """Read a result ZIP safely and return whether it contains a portable build."""
    archive_path = Path(result_archive).expanduser().resolve()
    if not archive_path.is_file():
        raise ReleaseTaskError(f"Windows 构建结果包不存在：{archive_path}")

    try:
        with zipfile.ZipFile(archive_path) as archive:
            members = _validated_zip_members(archive)

            def read_metadata(name: str, label: str) -> tuple[Any, bytes]:
                info = members.get(name)
                if info is None or info.is_dir():
                    raise ReleaseTaskError(f"Windows 构建结果缺少{label}：{name}")
                if info.file_size > 2 * 1024 * 1024:
                    raise ReleaseTaskError(f"Windows 构建结果的{label}过大")
                payload = archive.read(info)
                return json.loads(payload.decode("utf-8")), payload

            result_payload, _result_bytes = read_metadata(
                RESULT_FILE,
                "结果元数据",
            )
            request_payload, request_bytes = read_metadata(
                REQUEST_FILE,
                "任务元数据",
            )
            result = validate_result_payload(result_payload)
            request = validate_request(request_payload)

            if result["request_sha256"] != hashlib.sha256(request_bytes).hexdigest():
                raise ReleaseTaskError("Windows 结果绑定的构建任务 SHA-256 不匹配")
            if result["request_id"] != request["request_id"]:
                raise ReleaseTaskError("Windows 结果与内含构建任务 ID 不一致")
            for key in (
                "version",
                "source_commit",
                "base_url",
                "channel",
                "delta_from_version",
                "build_portable",
            ):
                if result.get(key) != request.get(key):
                    raise ReleaseTaskError(
                        f"Windows 结果元数据 {key} 与构建任务不一致"
                    )
            for key, label in (
                ("ca_bundle", "CA 根证书"),
                ("baseline_snapshot", "Windows 增量基线快照"),
            ):
                descriptor = request["inputs"].get(key)
                if descriptor is None:
                    continue
                normalized = validate_descriptor(descriptor, label)
                member = members.get(normalized["file"])
                if member is None or member.is_dir():
                    raise ReleaseTaskError(f"Windows 结果缺少{label}")
                if member.file_size != normalized["size"]:
                    raise ReleaseTaskError(f"Windows 结果中的{label}大小不匹配")

            version = request["version"]
            expected_names = {
                "windows_installer": f"IntDemoOnline-Setup-{version}.exe",
                "windows_snapshot": f"IntDemoOnline-Snapshot-{version}.json",
            }
            if request["delta_from_version"]:
                expected_names["windows_delta"] = (
                    f"IntDemoOnline-Patch-{request['delta_from_version']}"
                    f"-to-{version}.exe"
                )
            if request["build_portable"]:
                expected_names["windows_portable"] = (
                    f"IntDemoOnline-Portable-{version}.zip"
                )
            if set(result["artifacts"]) != set(expected_names):
                raise ReleaseTaskError("Windows 结果产物集合与便携包标记不一致")
            for key, expected_name in expected_names.items():
                descriptor = validate_descriptor(result["artifacts"][key], key)
                expected_file = f"artifacts/{expected_name}"
                if descriptor["file"] != expected_file:
                    raise ReleaseTaskError(
                        f"Windows 结果文件名不符合约定：{descriptor['file']}"
                    )
                member = members.get(expected_file)
                if member is None or member.is_dir():
                    raise ReleaseTaskError(f"Windows 结果缺少构建产物：{expected_file}")
                if member.file_size != descriptor["size"]:
                    raise ReleaseTaskError(f"Windows 构建产物大小不匹配：{expected_file}")
    except ReleaseTaskError:
        raise
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        zipfile.BadZipFile,
        RuntimeError,
    ) as exc:
        raise ReleaseTaskError(f"无法读取 Windows 构建结果：{archive_path}") from exc

    return bool(request["build_portable"])


def compare_request_to_expected(
    request: dict[str, Any],
    *,
    version: str,
    source_commit: str,
    base_url: str,
    channel: str,
    ca_bundle: str,
    delta_from_version: str,
    build_portable: bool,
) -> None:
    expected = {
        "version": version,
        "source_commit": source_commit.casefold(),
        "base_url": normalize_base_url(base_url),
        "channel": channel,
        "delta_from_version": str(delta_from_version or ""),
        "build_portable": bool(build_portable),
    }
    for key, value in expected.items():
        if request.get(key) != value:
            raise ReleaseTaskError(
                f"Windows 结果的构建任务 {key} 不匹配："
                f"{request.get(key)!r} != {value!r}"
            )
    if expected_ca_hash(request) != ca_fingerprint(ca_bundle):
        raise ReleaseTaskError("Windows 结果内的 CA 根证书与当前发布配置不一致")


def receipt_artifact(root: Path, path: Path) -> dict[str, Any]:
    resolved_root = root.resolve()
    resolved = path.resolve()
    try:
        relative = resolved.relative_to(resolved_root).as_posix()
    except ValueError as exc:
        raise ReleaseTaskError(f"候选产物必须位于仓库内：{resolved}") from exc
    descriptor = artifact_descriptor(resolved, relative)
    return {
        "path": descriptor["file"],
        "size": descriptor["size"],
        "sha256": descriptor["sha256"],
    }


def import_windows_result(
    root: Path,
    *,
    result_archive: Path,
    version: str,
    base_url: str,
    channel: str,
    ca_bundle: str,
    delta_from_version: str,
    build_portable: bool,
) -> Path:
    _branch, commit = source_state(root)
    result_archive = result_archive.expanduser().resolve()
    if not result_archive.is_file():
        raise ReleaseTaskError(f"Windows 构建结果包不存在：{result_archive}")
    if current_version(root) != version:
        raise ReleaseTaskError("当前项目版本与待导入 Windows 结果不一致")
    with tempfile.TemporaryDirectory(prefix="intdemo-windows-import-") as temporary:
        extracted = Path(temporary)
        extract_zip_safely(result_archive, extracted)
        try:
            result = validate_result_payload(
                json.loads((extracted / RESULT_FILE).read_text(encoding="utf-8"))
            )
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ReleaseTaskError("无法读取 Windows 构建结果元数据") from exc
        request, request_path, request_hash = load_request_from_directory(extracted)
        if result["request_sha256"] != request_hash:
            raise ReleaseTaskError("Windows 结果绑定的构建任务 SHA-256 不匹配")
        if result["request_id"] != request["request_id"]:
            raise ReleaseTaskError("Windows 结果与内含构建任务 ID 不一致")
        compare_request_to_expected(
            request,
            version=version,
            source_commit=commit,
            base_url=base_url,
            channel=channel,
            ca_bundle=ca_bundle,
            delta_from_version=delta_from_version,
            build_portable=build_portable,
        )
        for key in (
            "version",
            "source_commit",
            "base_url",
            "channel",
            "delta_from_version",
            "build_portable",
        ):
            if result.get(key) != request.get(key):
                raise ReleaseTaskError(f"Windows 结果元数据 {key} 与构建任务不一致")
        required = {"windows_installer", "windows_snapshot"}
        if delta_from_version:
            required.add("windows_delta")
        if build_portable:
            required.add("windows_portable")
        result_artifacts = result["artifacts"]
        if set(result_artifacts) != required:
            raise ReleaseTaskError(
                "Windows 结果产物集合不匹配："
                f"{sorted(result_artifacts)} != {sorted(required)}"
            )
        sources = {
            key: verify_file(extracted, result_artifacts[key], key)
            for key in sorted(required)
        }
        validate_snapshot(sources["windows_snapshot"], version)
        expected_names = {
            "windows_installer": f"IntDemoOnline-Setup-{version}.exe",
            "windows_snapshot": f"IntDemoOnline-Snapshot-{version}.json",
            "windows_delta": (
                f"IntDemoOnline-Patch-{delta_from_version}-to-{version}.exe"
            ),
            "windows_portable": f"IntDemoOnline-Portable-{version}.zip",
        }
        for key, source in sources.items():
            if source.name != expected_names[key]:
                raise ReleaseTaskError(
                    f"Windows 结果文件名不符合约定：{source.name}"
                )

        targets: dict[str, Path] = {
            "windows_installer": (
                root / "dist" / "installer" / expected_names["windows_installer"]
            ),
            "windows_snapshot": (
                root
                / "dist"
                / "windows-build-candidates"
                / f"{version}.json"
            ),
        }
        if delta_from_version:
            targets["windows_delta"] = (
                root / "dist" / "installer" / expected_names["windows_delta"]
            )
        if build_portable:
            targets["windows_portable"] = (
                root / "dist" / "portable" / expected_names["windows_portable"]
            )
        for key, source in sources.items():
            copy_atomic(source, targets[key])

        receipt = {
            "schema_version": 1,
            "platform": "windows-x86_64",
            "version": version,
            "source_commit": commit,
            "base_url": request["base_url"],
            "channel": channel,
            "ca_sha256": expected_ca_hash(request),
            "delta_from_version": delta_from_version,
            "build_portable": build_portable,
            "request_id": request["request_id"],
            "request_sha256": request_hash,
            "result_archive_sha256": sha256(result_archive),
            "validated_at": utc_now(),
            "builder": result.get("builder") or {},
            "artifacts": {
                key: receipt_artifact(root, target)
                for key, target in targets.items()
            },
        }
        receipt_path = (
            root
            / "dist"
            / "windows-build-results"
            / version
            / "validated-result.json"
        )
        write_json(receipt_path, receipt)
        request_record = (
            root
            / "dist"
            / "windows-build-requests"
            / version
            / request["request_id"]
            / REQUEST_FILE
        )
        write_bytes_atomic(request_record, request_path.read_bytes())
    print(f"Windows 构建结果已导入并校验：{receipt_path}", flush=True)
    return receipt_path


def parse_build_info(path: Path) -> dict[str, str]:
    try:
        return {
            key.strip(): value.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if "=" in line
            for key, value in [line.split("=", 1)]
        }
    except OSError as exc:
        raise ReleaseTaskError(f"无法读取 UOS 构建信息：{path}") from exc


def validate_uos_payload(
    payload_root: Path,
    *,
    version: str,
    commit: str,
    base_url: str,
    channel: str,
    ca_hash: str,
    label: str,
) -> None:
    from integrated_client.online.uos_file_update import (
        UosFileUpdateError,
        read_file_layout,
        validate_file_package_root,
    )
    from integrated_client.online.uos_layers import (
        UosLayerError,
        read_layout,
        validate_bootstrap,
        validate_package_root,
    )

    build_info = parse_build_info(payload_root / "build-info.txt")
    if build_info.get("version") != version:
        raise ReleaseTaskError(f"{label}构建信息版本不匹配")
    if build_info.get("git_commit", "").casefold() != commit:
        raise ReleaseTaskError(f"{label}与当前 Git 提交不一致")
    try:
        config = json.loads(
            (payload_root / "client-online.json").read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseTaskError(f"无法读取{label}在线配置") from exc
    if not isinstance(config, dict):
        raise ReleaseTaskError(f"{label}在线配置根节点必须是对象")
    if str(config.get("base_url") or "").rstrip("/") != base_url:
        raise ReleaseTaskError(f"{label}服务地址与当前配置不一致")
    if str(config.get("channel") or "") != channel:
        raise ReleaseTaskError(f"{label}通道与当前配置不一致")
    package_ca_value = str(config.get("ca_bundle") or "")
    if package_ca_value:
        relative_ca = PurePosixPath(package_ca_value)
        if (
            relative_ca.is_absolute()
            or ".." in relative_ca.parts
            or "\\" in package_ca_value
            or ":" in package_ca_value
            or relative_ca.as_posix() != package_ca_value
        ):
            raise ReleaseTaskError(f"{label}声明了不安全的 CA 根证书路径")
        package_ca = payload_root / relative_ca
        if not package_ca.is_file():
            raise ReleaseTaskError(f"{label}声明的 CA 根证书不存在")
        packaged_ca = sha256(package_ca)
    else:
        packaged_ca = ""
    if packaged_ca != ca_hash:
        raise ReleaseTaskError(f"{label}CA 根证书与当前配置不一致")
    try:
        layer_layout = read_layout(payload_root, version=version)
        validate_package_root(payload_root, layer_layout)
        validate_bootstrap(payload_root, layer_layout)
    except UosLayerError as exc:
        raise ReleaseTaskError(f"{label}UOS 分层基线无效：{exc}") from exc
    try:
        file_layout = read_file_layout(payload_root, version=version)
        validate_file_package_root(payload_root, file_layout)
    except UosFileUpdateError as exc:
        raise ReleaseTaskError(f"{label}UOS 逐文件基线无效：{exc}") from exc


def validate_uos_deb_payload(
    root: Path,
    artifact: Path,
    *,
    version: str,
    commit: str,
    base_url: str,
    channel: str,
    ca_hash: str,
) -> None:
    if not artifact.is_file():
        raise ReleaseTaskError(f"UOS DEB 不存在：{artifact}")
    if shutil.which("dpkg-deb") is None:
        raise ReleaseTaskError("校验 UOS DEB 需要系统提供 dpkg-deb")
    with tempfile.TemporaryDirectory(prefix="intdemo-uos-deb-check-") as temporary:
        extracted = Path(temporary)
        run_command(
            ["dpkg-deb", "--extract", str(artifact), str(extracted)],
            cwd=root,
        )
        validate_uos_payload(
            extracted / "opt" / "apps" / "com.e23aqiu.intdemo" / "files",
            version=version,
            commit=commit,
            base_url=base_url,
            channel=channel,
            ca_hash=ca_hash,
            label="UOS DEB ",
        )


def validate_published_uos_base(
    source_deb: Path,
    source_receipt_path: Path,
    *,
    version: str,
) -> dict[str, Any]:
    version_key(version)
    source_receipt = load_json_object(
        source_receipt_path,
        "UOS 来源发布收据",
    )
    try:
        receipt_schema_version = int(source_receipt.get("schema_version") or 0)
    except (TypeError, ValueError) as exc:
        raise ReleaseTaskError("UOS 来源发布收据版本无效") from exc
    if receipt_schema_version != 1:
        raise ReleaseTaskError("UOS 来源发布收据版本无效")
    if str(source_receipt.get("version") or "") != version:
        raise ReleaseTaskError("UOS 来源发布收据版本与增量基线不一致")
    source_artifacts = source_receipt.get("artifacts")
    source_artifact = (
        source_artifacts.get("uos_installer")
        if isinstance(source_artifacts, dict)
        else None
    )
    if not isinstance(source_artifact, dict):
        raise ReleaseTaskError("UOS 来源发布收据缺少完整 DEB")
    expected_name = f"IntDemo-UOS-arm64-{version}.deb"
    if source_deb.name != expected_name or not source_deb.is_file():
        raise ReleaseTaskError("UOS 增量基线不是正式发布 DEB")
    source_expected = {
        "name": expected_name,
        "size": source_deb.stat().st_size,
        "sha256": sha256(source_deb),
    }
    if any(
        source_artifact.get(key) != value
        for key, value in source_expected.items()
    ):
        raise ReleaseTaskError("UOS 来源发布收据与真实发布 DEB 增量基线不一致")
    return source_receipt


def validate_uos_delta_candidate(
    root: Path,
    *,
    from_version: str,
    target_version: str,
    target_deb: Path,
) -> tuple[Path, Path, dict[str, Any]]:
    version_key(from_version)
    if version_key(from_version) >= version_key(target_version):
        raise ReleaseTaskError("UOS 增量来源版本必须低于目标版本")
    patch = (
        root
        / "dist"
        / "uos-arm64"
        / f"IntDemo-UOS-arm64-Patch-{from_version}-to-{target_version}.intdelta"
    )
    report_path = patch.with_suffix(patch.suffix + ".json")
    if not patch.is_file() or not report_path.is_file():
        raise ReleaseTaskError("缺少 UOS 增量包或回放验证报告")
    report = load_json_object(report_path, "UOS 增量验证报告")
    expected = {
        "schema_version": 1,
        "format": "uos-deb-xdelta-v1",
        "algorithm": "xdelta3",
        "from_version": from_version,
        "target_version": target_version,
        "patch_name": patch.name,
        "target_name": target_deb.name,
        "target_size": target_deb.stat().st_size,
        "target_sha256": sha256(target_deb),
        "patch_size": patch.stat().st_size,
        "patch_sha256": sha256(patch),
        "eligible": True,
        "byte_identical": True,
    }
    for key, value in expected.items():
        if report.get(key) != value:
            raise ReleaseTaskError(
                f"UOS 增量验证报告 {key} 不匹配："
                f"{report.get(key)!r} != {value!r}"
            )
    try:
        base_size = int(report.get("base_size"))
    except (TypeError, ValueError) as exc:
        raise ReleaseTaskError("UOS 增量验证报告的基线大小无效") from exc
    base_sha256 = str(report.get("base_sha256") or "").casefold()
    if base_size <= 0 or not re.fullmatch(r"[0-9a-f]{64}", base_sha256):
        raise ReleaseTaskError("UOS 增量验证报告的基线信息无效")
    if patch.stat().st_size * 2 >= target_deb.stat().st_size:
        raise ReleaseTaskError("UOS 增量包未达到小于完整 DEB 50% 的发布门槛")

    source_deb = (
        root
        / "dist"
        / "update-release"
        / "files"
        / f"IntDemo-UOS-arm64-{from_version}.deb"
    )
    source_receipt_path = (
        root
        / "dist"
        / "release-results"
        / from_version
        / "publish-receipt.json"
    )
    validate_published_uos_base(
        source_deb,
        source_receipt_path,
        version=from_version,
    )
    if source_deb.stat().st_size != base_size or sha256(source_deb) != base_sha256:
        raise ReleaseTaskError("UOS 增量基线不是经过收据校验的真实发布 DEB")
    return patch, report_path, report


def validate_uos_layer_candidate(
    root: Path,
    *,
    from_version: str,
    target_version: str,
    target_deb: Path,
) -> tuple[Path | None, Path, dict[str, Any]]:
    """Validate a layered candidate or its intentional full-DEB fallback.

    The report is mandatory whenever an operator selected a UOS source
    version. An absent archive is valid only when the native UOS replay marked
    it ineligible and explicitly selected the full package fallback.
    """

    version_key(from_version)
    if version_key(from_version) >= version_key(target_version):
        raise ReleaseTaskError("UOS 分层来源版本必须低于目标版本")
    archive = (
        root
        / "dist"
        / "uos-arm64"
        / (
            f"IntDemo-UOS-arm64-Layers-{from_version}"
            f"-to-{target_version}.intlayer"
        )
    )
    report_path = archive.with_suffix(archive.suffix + ".json")
    if not report_path.is_file():
        raise ReleaseTaskError("缺少 UOS 分层构建报告")
    report = load_json_object(report_path, "UOS 分层构建报告")
    expected = {
        "schema_version": 1,
        "format": "uos-layered-v1",
        "platform": "linux-aarch64",
        "from_version": from_version,
        "target_version": target_version,
        "target_name": target_deb.name,
        "target_size": target_deb.stat().st_size,
        "target_sha256": sha256(target_deb),
        "archive_name": archive.name,
    }
    for key, value in expected.items():
        if report.get(key) != value:
            raise ReleaseTaskError(
                f"UOS 分层构建报告 {key} 不匹配："
                f"{report.get(key)!r} != {value!r}"
            )
    source_deb = (
        root
        / "dist"
        / "update-release"
        / "files"
        / f"IntDemo-UOS-arm64-{from_version}.deb"
    )
    source_receipt_path = (
        root
        / "dist"
        / "release-results"
        / from_version
        / "publish-receipt.json"
    )
    validate_published_uos_base(
        source_deb,
        source_receipt_path,
        version=from_version,
    )
    source_expected = {
        "source_name": source_deb.name,
        "source_size": source_deb.stat().st_size,
        "source_sha256": sha256(source_deb),
    }
    for key, value in source_expected.items():
        if report.get(key) != value:
            raise ReleaseTaskError(f"UOS 分层报告的来源基线 {key} 不匹配")

    eligible = report.get("eligible") is True
    if not eligible:
        if report.get("fallback_to_full") is not True:
            raise ReleaseTaskError("UOS 分层未通过时必须明确回退完整 DEB")
        if not str(report.get("reason") or "").strip():
            raise ReleaseTaskError("UOS 分层回退报告缺少原因")
        if archive.exists():
            raise ReleaseTaskError("UOS 分层报告已回退完整包，但候选包仍然存在")
        return None, report_path, report

    if report.get("fallback_to_full") is not False:
        raise ReleaseTaskError("UOS 分层候选状态无效")
    if report.get("replay_verified") is not True:
        raise ReleaseTaskError("UOS 分层候选没有通过回放验证")
    if not archive.is_file():
        raise ReleaseTaskError("UOS 分层报告声明可发布，但候选包不存在")
    archive_expected = {
        "archive_size": archive.stat().st_size,
        "archive_sha256": sha256(archive),
    }
    for key, value in archive_expected.items():
        if report.get(key) != value:
            raise ReleaseTaskError(f"UOS 分层报告的候选包 {key} 不匹配")
    for key in ("source_layout_sha256", "target_layout_sha256"):
        if not re.fullmatch(r"[0-9a-f]{64}", str(report.get(key) or "")):
            raise ReleaseTaskError(f"UOS 分层报告 {key} 无效")
    source_bootstrap_sha256 = str(
        report.get("source_bootstrap_sha256") or ""
    ).casefold()
    target_bootstrap_sha256 = str(
        report.get("target_bootstrap_sha256") or ""
    ).casefold()
    if (
        not re.fullmatch(r"[0-9a-f]{64}", source_bootstrap_sha256)
        or not re.fullmatch(r"[0-9a-f]{64}", target_bootstrap_sha256)
        or source_bootstrap_sha256 != target_bootstrap_sha256
    ):
        raise ReleaseTaskError("UOS 分层报告的引导文件指纹不兼容")
    changed_layers = report.get("changed_layers")
    if (
        not isinstance(changed_layers, list)
        or not changed_layers
        or any(name not in {"app", "runtime", "browser"} for name in changed_layers)
    ):
        raise ReleaseTaskError("UOS 分层报告的变更层列表无效")
    try:
        from integrated_client.online.uos_layers import (
            UosLayerError,
            read_layer_manifest,
        )

        layer_manifest = read_layer_manifest(
            archive,
            from_version=from_version,
            target_version=target_version,
            source_layout_sha256=str(report["source_layout_sha256"]),
            target_layout_sha256=str(report["target_layout_sha256"]),
        )
    except UosLayerError as exc:
        raise ReleaseTaskError(f"UOS 分层包结构校验失败：{exc}") from exc
    if layer_manifest.get("changed_layers") != changed_layers:
        raise ReleaseTaskError("UOS 分层包与回放报告的变更层不一致")
    if archive.stat().st_size * 2 >= target_deb.stat().st_size:
        raise ReleaseTaskError("UOS 分层包未达到小于完整 DEB 50% 的发布门槛")
    return archive, report_path, report


def validate_uos_file_candidate(
    root: Path,
    *,
    from_version: str,
    target_version: str,
    target_deb: Path,
) -> tuple[Path | None, Path, dict[str, Any]]:
    """Validate a replayed file update or its intentional full-DEB fallback."""

    version_key(from_version)
    if version_key(from_version) >= version_key(target_version):
        raise ReleaseTaskError("UOS 逐文件来源版本必须低于目标版本")
    archive = (
        root
        / "dist"
        / "uos-arm64"
        / (
            f"IntDemo-UOS-arm64-Files-{from_version}"
            f"-to-{target_version}.intlayer"
        )
    )
    report_path = archive.with_suffix(archive.suffix + ".json")
    if not report_path.is_file():
        raise ReleaseTaskError("缺少 UOS 逐文件构建报告")
    report = load_json_object(report_path, "UOS 逐文件构建报告")
    expected = {
        "schema_version": 2,
        "format": UOS_FILE_UPDATE_CAPABILITY,
        "platform": "linux-aarch64",
        "from_version": from_version,
        "target_version": target_version,
        "target_name": target_deb.name,
        "target_size": target_deb.stat().st_size,
        "target_sha256": sha256(target_deb),
        "archive_name": archive.name,
    }
    for key, value in expected.items():
        if report.get(key) != value:
            raise ReleaseTaskError(
                f"UOS 逐文件构建报告 {key} 不匹配："
                f"{report.get(key)!r} != {value!r}"
            )
    source_deb = (
        root
        / "dist"
        / "update-release"
        / "files"
        / f"IntDemo-UOS-arm64-{from_version}.deb"
    )
    source_receipt_path = (
        root
        / "dist"
        / "release-results"
        / from_version
        / "publish-receipt.json"
    )
    validate_published_uos_base(
        source_deb,
        source_receipt_path,
        version=from_version,
    )
    for key, value in {
        "source_name": source_deb.name,
        "source_size": source_deb.stat().st_size,
        "source_sha256": sha256(source_deb),
    }.items():
        if report.get(key) != value:
            raise ReleaseTaskError(f"UOS 逐文件报告的来源基线 {key} 不匹配")

    eligible = report.get("eligible") is True
    if not eligible:
        if report.get("fallback_to_full") is not True:
            raise ReleaseTaskError("UOS 逐文件包未通过时必须明确回退完整 DEB")
        if not str(report.get("reason") or "").strip():
            raise ReleaseTaskError("UOS 逐文件回退报告缺少原因")
        if archive.exists():
            raise ReleaseTaskError("UOS 逐文件报告已回退完整包，但候选包仍然存在")
        return None, report_path, report

    if report.get("fallback_to_full") is not False:
        raise ReleaseTaskError("UOS 逐文件候选状态无效")
    if report.get("replay_verified") is not True:
        raise ReleaseTaskError("UOS 逐文件候选没有通过回放验证")
    if not archive.is_file():
        raise ReleaseTaskError("UOS 逐文件报告声明可发布，但候选包不存在")
    for key, value in {
        "archive_size": archive.stat().st_size,
        "archive_sha256": sha256(archive),
    }.items():
        if report.get(key) != value:
            raise ReleaseTaskError(f"UOS 逐文件报告的候选包 {key} 不匹配")
    for key in ("source_layout_sha256", "target_layout_sha256"):
        if not re.fullmatch(r"[0-9a-f]{64}", str(report.get(key) or "")):
            raise ReleaseTaskError(f"UOS 逐文件报告 {key} 无效")
    source_bootstrap = str(report.get("source_bootstrap_sha256") or "").casefold()
    target_bootstrap = str(report.get("target_bootstrap_sha256") or "").casefold()
    if (
        not re.fullmatch(r"[0-9a-f]{64}", source_bootstrap)
        or not re.fullmatch(r"[0-9a-f]{64}", target_bootstrap)
        or source_bootstrap != target_bootstrap
    ):
        raise ReleaseTaskError("UOS 逐文件报告的引导文件指纹不兼容")
    changed_paths = report.get("changed_paths")
    deleted_paths = report.get("deleted_paths")
    if not isinstance(changed_paths, list) or not isinstance(deleted_paths, list):
        raise ReleaseTaskError("UOS 逐文件报告缺少变更或删除清单")
    if changed_paths != sorted(set(changed_paths)):
        raise ReleaseTaskError("UOS 逐文件报告的变更清单无效")
    if deleted_paths != sorted(set(deleted_paths)):
        raise ReleaseTaskError("UOS 逐文件报告的删除清单无效")
    try:
        from integrated_client.online.uos_file_update import (
            UosFileUpdateError,
            read_file_manifest,
        )

        file_manifest = read_file_manifest(
            archive,
            from_version=from_version,
            target_version=target_version,
            source_layout_sha256=str(report["source_layout_sha256"]),
            target_layout_sha256=str(report["target_layout_sha256"]),
        )
    except UosFileUpdateError as exc:
        raise ReleaseTaskError(f"UOS 逐文件包结构校验失败：{exc}") from exc
    if file_manifest["changed_paths"] != changed_paths:
        raise ReleaseTaskError("UOS 逐文件包与回放报告的变更清单不一致")
    if file_manifest["deleted_paths"] != deleted_paths:
        raise ReleaseTaskError("UOS 逐文件包与回放报告的删除清单不一致")
    if archive.stat().st_size * 2 >= target_deb.stat().st_size:
        raise ReleaseTaskError("UOS 逐文件包未达到小于完整 DEB 50% 的发布门槛")
    return archive, report_path, report


def record_uos_result(
    root: Path,
    *,
    version: str,
    base_url: str,
    channel: str,
    ca_bundle: str,
    uos_delta_from_version: str = "",
    allow_detached: bool = False,
) -> Path:
    _branch, commit = source_state(root, allow_detached=allow_detached)
    normalized_url = normalize_base_url(base_url)
    if channel not in {"test", "stable"}:
        raise ReleaseTaskError("UOS 构建通道无效")
    if current_version(root) != version:
        raise ReleaseTaskError("当前项目版本与 UOS 候选包不一致")
    package_root = (
        root
        / "dist"
        / "uos-arm64"
        / "package"
        / f"IntDemo-UOS-arm64-{version}"
    )
    artifact = root / "dist" / "uos-arm64" / f"IntDemo-UOS-arm64-{version}.deb"
    expected_ca = ca_fingerprint(ca_bundle)
    validate_uos_payload(
        package_root,
        version=version,
        commit=commit,
        base_url=normalized_url,
        channel=channel,
        ca_hash=expected_ca,
        label="UOS 候选包",
    )
    validate_uos_deb_payload(
        root,
        artifact,
        version=version,
        commit=commit,
        base_url=normalized_url,
        channel=channel,
        ca_hash=expected_ca,
    )
    artifacts = {
        "uos_installer": receipt_artifact(root, artifact),
    }
    uos_update_kind = "full"
    if uos_delta_from_version:
        file_update, report_path, _report = validate_uos_file_candidate(
            root,
            from_version=uos_delta_from_version,
            target_version=version,
            target_deb=artifact,
        )
        artifacts["uos_file_report"] = receipt_artifact(root, report_path)
        if file_update is not None:
            artifacts["uos_file"] = receipt_artifact(root, file_update)
            uos_update_kind = "file"
    receipt = {
        "schema_version": 1,
        "platform": "linux-aarch64",
        "version": version,
        "source_commit": commit,
        "base_url": normalized_url,
        "channel": channel,
        "ca_sha256": expected_ca,
        "validated_at": utc_now(),
        "uos_delta_from_version": str(uos_delta_from_version or ""),
        "uos_update_kind": uos_update_kind,
        "artifacts": artifacts,
    }
    receipt_path = (
        root / "dist" / "uos-build-results" / version / "validated-result.json"
    )
    write_json(receipt_path, receipt)
    print(f"UOS DEB 构建结果已校验：{receipt_path}", flush=True)
    return receipt_path


def validate_uos_result_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ReleaseTaskError("UOS 构建结果版本不受支持")
    try:
        schema_version = int(payload.get("schema_version") or 0)
    except (TypeError, ValueError) as exc:
        raise ReleaseTaskError("UOS 构建结果版本不受支持") from exc
    if schema_version != 1:
        raise ReleaseTaskError("UOS 构建结果版本不受支持")
    platform_key = str(payload.get("platform") or "")
    version = str(payload.get("version") or "")
    source_commit = str(payload.get("source_commit") or "").casefold()
    base_url = normalize_base_url(str(payload.get("base_url") or ""))
    channel = str(payload.get("channel") or "")
    ca_hash = str(payload.get("ca_sha256") or "").casefold()
    artifacts = payload.get("artifacts")
    uos_delta_from_version = str(payload.get("uos_delta_from_version") or "")
    uos_update_kind = str(payload.get("uos_update_kind") or "").strip()
    if platform_key != "linux-aarch64":
        raise ReleaseTaskError("UOS 构建结果平台必须是 linux-aarch64")
    version_key(version)
    if not COMMIT_PATTERN.fullmatch(source_commit):
        raise ReleaseTaskError("UOS 构建结果缺少完整源提交")
    if channel not in {"test", "stable"}:
        raise ReleaseTaskError("UOS 构建结果通道无效")
    if ca_hash and not re.fullmatch(r"[0-9a-f]{64}", ca_hash):
        raise ReleaseTaskError("UOS 构建结果 CA 指纹无效")
    if payload.get("payload_validated") is not True:
        raise ReleaseTaskError("UOS 构建结果未声明已完成安装内容校验")
    if not isinstance(artifacts, dict):
        raise ReleaseTaskError("UOS 构建结果缺少 artifacts")
    if not uos_update_kind:
        if "uos_delta" in artifacts:
            uos_update_kind = "delta"
        elif "uos_file" in artifacts:
            uos_update_kind = "file"
        elif "uos_layer" in artifacts:
            uos_update_kind = "layered"
        else:
            uos_update_kind = "full"
    if uos_update_kind not in {"full", "file", "layered", "delta"}:
        raise ReleaseTaskError("UOS 构建结果的更新类型无效")
    if uos_update_kind != "full" and not uos_delta_from_version:
        raise ReleaseTaskError("UOS 增量构建结果缺少来源版本")
    if uos_delta_from_version and version_key(
        uos_delta_from_version
    ) >= version_key(version):
        raise ReleaseTaskError("UOS 构建结果的增量来源版本无效")
    builder = payload.get("builder")
    if not isinstance(builder, dict):
        raise ReleaseTaskError("UOS 构建结果缺少构建机信息")
    return {
        **payload,
        "platform": platform_key,
        "version": version,
        "source_commit": source_commit,
        "base_url": base_url,
        "channel": channel,
        "ca_sha256": ca_hash,
        "artifacts": artifacts,
        "builder": builder,
        "uos_delta_from_version": uos_delta_from_version,
        "uos_update_kind": uos_update_kind,
    }


def expected_uos_result_artifacts(payload: dict[str, Any]) -> set[str]:
    kind = str(payload.get("uos_update_kind") or "full")
    source = str(payload.get("uos_delta_from_version") or "")
    artifacts = payload.get("artifacts")
    artifact_keys = set(artifacts) if isinstance(artifacts, dict) else set()
    if kind == "delta":
        return {"uos_installer", "uos_delta", "uos_delta_report"}
    if kind == "file":
        return {"uos_installer", "uos_file", "uos_file_report"}
    if kind == "layered":
        return {"uos_installer", "uos_layer", "uos_layer_report"}
    if source:
        if "uos_layer_report" in artifact_keys:
            return {"uos_installer", "uos_layer_report"}
        return {"uos_installer", "uos_file_report"}
    return {"uos_installer"}


def validate_uos_delta_base_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ReleaseTaskError("UOS 增量基线包版本不受支持")
    try:
        schema_version = int(payload.get("schema_version") or 0)
    except (TypeError, ValueError) as exc:
        raise ReleaseTaskError("UOS 增量基线包版本不受支持") from exc
    if schema_version != 1:
        raise ReleaseTaskError("UOS 增量基线包版本不受支持")
    if str(payload.get("kind") or "") != "uos-delta-base":
        raise ReleaseTaskError("所选文件不是 UOS 增量基线包")
    version = str(payload.get("version") or "").strip()
    version_key(version)
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, dict) or set(artifacts) != {
        "uos_installer",
        "publish_receipt",
    }:
        raise ReleaseTaskError("UOS 增量基线包的产物集合不完整")
    return {**payload, "version": version, "artifacts": artifacts}


def _inspect_uos_delta_base_archive(
    base_archive: str | Path,
) -> tuple[Path, dict[str, Any]]:
    archive_path = Path(base_archive).expanduser().resolve()
    if not archive_path.is_file():
        raise ReleaseTaskError(f"UOS 增量基线包不存在：{archive_path}")
    try:
        with zipfile.ZipFile(archive_path) as archive:
            members = _validated_zip_members(archive)
            metadata = members.get(UOS_DELTA_BASE_FILE)
            if metadata is None or metadata.is_dir():
                raise ReleaseTaskError("UOS 增量基线包缺少元数据")
            if metadata.file_size > 2 * 1024 * 1024:
                raise ReleaseTaskError("UOS 增量基线包元数据过大")
            payload = validate_uos_delta_base_payload(
                json.loads(archive.read(metadata).decode("utf-8"))
            )
            for key, label in (
                ("uos_installer", "完整 DEB"),
                ("publish_receipt", "发布收据"),
            ):
                descriptor = validate_descriptor(payload["artifacts"][key], label)
                member = members.get(descriptor["file"])
                if member is None or member.is_dir():
                    raise ReleaseTaskError(f"UOS 增量基线包缺少{label}")
                if member.file_size != descriptor["size"]:
                    raise ReleaseTaskError(f"UOS 增量基线包中的{label}大小不匹配")
    except ReleaseTaskError:
        raise
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        zipfile.BadZipFile,
        RuntimeError,
    ) as exc:
        raise ReleaseTaskError(f"无法读取 UOS 增量基线包：{archive_path}") from exc
    return archive_path, payload


def detect_uos_delta_base_version(base_archive: str | Path) -> str:
    _archive, payload = _inspect_uos_delta_base_archive(base_archive)
    return str(payload["version"])


def validate_windows_delta_base_payload(payload: Any) -> dict[str, Any]:
    """Validate metadata for a portable Windows incremental base archive.

    A Windows delta only needs the published file snapshot and its publish
    receipt.  Keeping the archive small is important because it is commonly
    transferred from the Windows release host to a UOS build host.  The
    receipt provides provenance while the snapshot is the input consumed by
    ``build-delta-installer.ps1``.
    """
    if not isinstance(payload, dict):
        raise ReleaseTaskError("Windows 增量基线包版本不受支持")
    try:
        schema_version = int(payload.get("schema_version") or 0)
    except (TypeError, ValueError) as exc:
        raise ReleaseTaskError("Windows 增量基线包版本不受支持") from exc
    if schema_version != 1:
        raise ReleaseTaskError("Windows 增量基线包版本不受支持")
    if str(payload.get("kind") or "") != "windows-delta-base":
        raise ReleaseTaskError("所选文件不是 Windows 增量基线包")
    version = str(payload.get("version") or "").strip()
    version_key(version)
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, dict) or set(artifacts) != {
        "windows_snapshot",
        "publish_receipt",
    }:
        raise ReleaseTaskError("Windows 增量基线包的产物集合不完整")
    for key, label in (
        ("windows_snapshot", "Windows 发布快照"),
        ("publish_receipt", "发布收据"),
    ):
        validate_descriptor(artifacts[key], label)
    source_commit = str(payload.get("source_commit") or "").casefold()
    if source_commit and not COMMIT_PATTERN.fullmatch(source_commit):
        raise ReleaseTaskError("Windows 增量基线包源提交无效")
    return {
        **payload,
        "schema_version": 1,
        "version": version,
        "source_commit": source_commit,
        "artifacts": artifacts,
    }


def _validate_windows_baseline_receipt(
    receipt: dict[str, Any],
    *,
    version: str,
) -> dict[str, Any]:
    """Check the release receipt without requiring a platform-specific shape.

    Receipts created by older publisher builds contain a smaller set of fields;
    they remain valid as long as they identify the published version.  When a
    Windows artifact descriptor is present, it is checked as well.
    """
    try:
        schema_version = int(receipt.get("schema_version") or 0)
    except (TypeError, ValueError) as exc:
        raise ReleaseTaskError("Windows 来源发布收据版本无效") from exc
    if schema_version != 1:
        raise ReleaseTaskError("Windows 来源发布收据版本无效")
    if str(receipt.get("version") or "") != version:
        raise ReleaseTaskError("Windows 来源发布收据版本与增量基线不一致")
    artifacts = receipt.get("artifacts")
    if artifacts is not None and not isinstance(artifacts, dict):
        raise ReleaseTaskError("Windows 来源发布收据 artifacts 无效")
    if isinstance(artifacts, dict) and "windows_installer" in artifacts:
        descriptor = artifacts["windows_installer"]
        if not isinstance(descriptor, dict):
            raise ReleaseTaskError("Windows 来源发布收据缺少完整安装包")
        name = str(descriptor.get("name") or "")
        expected_name = f"IntDemoOnline-Setup-{version}.exe"
        if name != expected_name:
            raise ReleaseTaskError("Windows 来源发布收据完整包名称不一致")
        try:
            size = int(descriptor.get("size"))
        except (TypeError, ValueError) as exc:
            raise ReleaseTaskError("Windows 来源发布收据完整包大小无效") from exc
        digest = str(descriptor.get("sha256") or "").casefold()
        if size < 0 or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ReleaseTaskError("Windows 来源发布收据完整包校验值无效")
    return receipt


def _inspect_windows_delta_base_archive(
    base_archive: str | Path,
) -> tuple[Path, dict[str, Any]]:
    archive_path = Path(base_archive).expanduser().resolve()
    if not archive_path.is_file():
        raise ReleaseTaskError(f"Windows 增量基线包不存在：{archive_path}")
    try:
        with zipfile.ZipFile(archive_path) as archive:
            members = _validated_zip_members(archive)
            metadata = members.get(WINDOWS_DELTA_BASE_FILE)
            if metadata is None or metadata.is_dir():
                raise ReleaseTaskError("Windows 增量基线包缺少元数据")
            if metadata.file_size > 2 * 1024 * 1024:
                raise ReleaseTaskError("Windows 增量基线包元数据过大")
            payload = validate_windows_delta_base_payload(
                json.loads(archive.read(metadata).decode("utf-8"))
            )
            snapshot_descriptor = validate_descriptor(
                payload["artifacts"]["windows_snapshot"],
                "Windows 发布快照",
            )
            receipt_descriptor = validate_descriptor(
                payload["artifacts"]["publish_receipt"],
                "发布收据",
            )
            snapshot_member = members.get(snapshot_descriptor["file"])
            receipt_member = members.get(receipt_descriptor["file"])
            if snapshot_member is None or snapshot_member.is_dir():
                raise ReleaseTaskError("Windows 增量基线包缺少发布快照")
            if receipt_member is None or receipt_member.is_dir():
                raise ReleaseTaskError("Windows 增量基线包缺少发布收据")
            if snapshot_member.file_size != snapshot_descriptor["size"]:
                raise ReleaseTaskError("Windows 增量基线包中的发布快照大小不匹配")
            if receipt_member.file_size != receipt_descriptor["size"]:
                raise ReleaseTaskError("Windows 增量基线包中的发布收据大小不匹配")
            if hashlib.sha256(archive.read(snapshot_member)).hexdigest() != snapshot_descriptor[
                "sha256"
            ]:
                raise ReleaseTaskError("Windows 增量基线包中的发布快照 SHA-256 不匹配")
            if hashlib.sha256(archive.read(receipt_member)).hexdigest() != receipt_descriptor[
                "sha256"
            ]:
                raise ReleaseTaskError("Windows 增量基线包中的发布收据 SHA-256 不匹配")
            snapshot_payload = json.loads(archive.read(snapshot_member).decode("utf-8"))
            # Validate the snapshot directly from the archive bytes so import
            # never needs to trust an unverified temporary path.
            if (
                not isinstance(snapshot_payload, dict)
                or int(snapshot_payload.get("schema_version") or 0) != 1
                or str(snapshot_payload.get("version") or "")
                != payload["version"]
                or not isinstance(snapshot_payload.get("files"), list)
                or not snapshot_payload["files"]
            ):
                raise ReleaseTaskError("Windows 增量基线包中的发布快照无效")
            receipt_payload = json.loads(archive.read(receipt_member).decode("utf-8"))
            if not isinstance(receipt_payload, dict):
                raise ReleaseTaskError("Windows 增量基线包中的发布收据无效")
            _validate_windows_baseline_receipt(
                receipt_payload,
                version=payload["version"],
            )
    except ReleaseTaskError:
        raise
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        zipfile.BadZipFile,
        RuntimeError,
        ValueError,
    ) as exc:
        raise ReleaseTaskError(f"无法读取 Windows 增量基线包：{archive_path}") from exc
    return archive_path, payload


def detect_windows_delta_base_version(base_archive: str | Path) -> str:
    _archive, payload = _inspect_windows_delta_base_archive(base_archive)
    return str(payload["version"])


def export_windows_delta_base(
    root: Path,
    *,
    version: str,
    output: Path | None = None,
) -> Path:
    """Export a real published Windows snapshot for transfer to another host."""
    version_key(version)
    snapshot = root / "dist" / "release-snapshots" / f"{version}.json"
    receipt_path = (
        root / "dist" / "release-results" / version / "publish-receipt.json"
    )
    if not snapshot.is_file():
        raise ReleaseTaskError(f"缺少实际发布的 Windows 基线快照：{snapshot}")
    if not receipt_path.is_file():
        raise ReleaseTaskError(f"缺少 Windows 来源发布收据：{receipt_path}")
    validate_snapshot(snapshot, version)
    receipt = _validate_windows_baseline_receipt(
        load_json_object(receipt_path, "Windows 来源发布收据"),
        version=version,
    )
    result = (
        output.expanduser().resolve()
        if output is not None
        else (
            root
            / "dist"
            / "windows-delta-bases"
            / version
            / f"windows-delta-base-{version}.zip"
        )
    )
    with tempfile.TemporaryDirectory(prefix="intdemo-windows-delta-base-") as temporary:
        staging = Path(temporary)
        staged_snapshot = staging / "artifacts" / f"IntDemoOnline-Snapshot-{version}.json"
        staged_receipt = staging / "publish-receipt.json"
        staged_snapshot.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(snapshot, staged_snapshot)
        shutil.copy2(receipt_path, staged_receipt)
        write_json(
            staging / WINDOWS_DELTA_BASE_FILE,
            {
                "schema_version": 1,
                "kind": "windows-delta-base",
                "version": version,
                "source_commit": str(receipt.get("source_commit") or "").casefold(),
                "exported_at": utc_now(),
                "artifacts": {
                    "windows_snapshot": artifact_descriptor(
                        staged_snapshot,
                        f"artifacts/{staged_snapshot.name}",
                    ),
                    "publish_receipt": artifact_descriptor(
                        staged_receipt,
                        staged_receipt.name,
                    ),
                },
            },
        )
        make_zip(staging, result)
    write_bytes_atomic(
        result.with_suffix(result.suffix + ".sha256"),
        f"{sha256(result)}  {result.name}\n".encode("ascii"),
    )
    print(f"Windows 增量基线包：{result}", flush=True)
    print(f"SHA-256：{sha256(result)}", flush=True)
    return result


def import_windows_delta_base(
    root: Path,
    *,
    base_archive: Path,
) -> str:
    archive_path, inspected = _inspect_windows_delta_base_archive(base_archive)
    version = str(inspected["version"])
    with tempfile.TemporaryDirectory(prefix="intdemo-windows-delta-import-") as temporary:
        extracted = Path(temporary)
        extract_zip_safely(archive_path, extracted)
        payload = validate_windows_delta_base_payload(
            load_json_object(extracted / WINDOWS_DELTA_BASE_FILE, "Windows 增量基线元数据")
        )
        snapshot = verify_file(
            extracted,
            payload["artifacts"]["windows_snapshot"],
            "Windows 发布快照",
        )
        receipt = verify_file(
            extracted,
            payload["artifacts"]["publish_receipt"],
            "Windows 发布收据",
        )
        validate_snapshot(snapshot, version)
        _validate_windows_baseline_receipt(
            load_json_object(receipt, "Windows 来源发布收据"),
            version=version,
        )
        targets = (
            (
                snapshot,
                root / "dist" / "release-snapshots" / f"{version}.json",
            ),
            (
                receipt,
                root / "dist" / "release-results" / version / "publish-receipt.json",
            ),
        )
        for source, target in targets:
            if target.exists() and (
                not target.is_file() or sha256(target) != sha256(source)
            ):
                raise ReleaseTaskError(
                    f"本地已有不同的同版本 Windows 增量基线，拒绝覆盖：{target}"
                )
        for source, target in targets:
            if not target.is_file():
                copy_atomic(source, target)
    print(f"Windows 增量基线已导入：{version}", flush=True)
    return version


# User-facing aliases retained for callers that use the shorter "baseline"
# terminology.  The explicit delta names remain the canonical API.
validate_windows_baseline_payload = validate_windows_delta_base_payload
detect_windows_baseline_version = detect_windows_delta_base_version
export_windows_baseline = export_windows_delta_base
import_windows_baseline = import_windows_delta_base


def export_uos_delta_base(
    root: Path,
    *,
    version: str,
    output: Path | None = None,
) -> Path:
    version_key(version)
    source_deb = (
        root
        / "dist/update-release/files"
        / f"IntDemo-UOS-arm64-{version}.deb"
    )
    source_receipt = (
        root / "dist/release-results" / version / "publish-receipt.json"
    )
    receipt = validate_published_uos_base(
        source_deb,
        source_receipt,
        version=version,
    )
    result = (
        output.expanduser().resolve()
        if output is not None
        else (
            root
            / "dist/uos-delta-bases"
            / version
            / f"uos-delta-base-{version}.zip"
        )
    )
    with tempfile.TemporaryDirectory(prefix="intdemo-uos-delta-base-") as temporary:
        staging = Path(temporary)
        staged_deb = staging / "artifacts" / source_deb.name
        staged_receipt = staging / "publish-receipt.json"
        staged_deb.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_deb, staged_deb)
        shutil.copy2(source_receipt, staged_receipt)
        write_json(
            staging / UOS_DELTA_BASE_FILE,
            {
                "schema_version": 1,
                "kind": "uos-delta-base",
                "version": version,
                "source_commit": str(receipt.get("source_commit") or ""),
                "exported_at": utc_now(),
                "artifacts": {
                    "uos_installer": artifact_descriptor(
                        staged_deb,
                        f"artifacts/{staged_deb.name}",
                    ),
                    "publish_receipt": artifact_descriptor(
                        staged_receipt,
                        staged_receipt.name,
                    ),
                },
            },
        )
        make_zip(staging, result)
    write_bytes_atomic(
        result.with_suffix(result.suffix + ".sha256"),
        f"{sha256(result)}  {result.name}\n".encode("ascii"),
    )
    print(f"UOS 增量基线包：{result}", flush=True)
    print(f"SHA-256：{sha256(result)}", flush=True)
    return result


def import_uos_delta_base(
    root: Path,
    *,
    base_archive: Path,
) -> str:
    archive_path, inspected = _inspect_uos_delta_base_archive(base_archive)
    version = str(inspected["version"])
    with tempfile.TemporaryDirectory(prefix="intdemo-uos-delta-import-") as temporary:
        extracted = Path(temporary)
        extract_zip_safely(archive_path, extracted)
        payload = validate_uos_delta_base_payload(
            load_json_object(extracted / UOS_DELTA_BASE_FILE, "UOS 增量基线元数据")
        )
        if payload["version"] != version:
            raise ReleaseTaskError("UOS 增量基线包版本在读取期间发生变化")
        source_deb = verify_file(
            extracted,
            payload["artifacts"]["uos_installer"],
            "UOS 增量基线 DEB",
        )
        source_receipt = verify_file(
            extracted,
            payload["artifacts"]["publish_receipt"],
            "UOS 增量基线发布收据",
        )
        validate_published_uos_base(
            source_deb,
            source_receipt,
            version=version,
        )
        targets = (
            (
                source_deb,
                root / "dist/update-release/files" / source_deb.name,
            ),
            (
                source_receipt,
                root
                / "dist/release-results"
                / version
                / "publish-receipt.json",
            ),
        )
        for source, target in targets:
            if target.exists() and (
                not target.is_file() or sha256(target) != sha256(source)
            ):
                raise ReleaseTaskError(
                    f"本地已有不同的同版本 UOS 增量基线，拒绝覆盖：{target}"
                )
        for source, target in targets:
            if not target.is_file():
                copy_atomic(source, target)
    print(f"UOS 增量基线已导入：{version}", flush=True)
    return version


def detect_uos_result_delta_source(result_archive: str | Path) -> str:
    archive_path = Path(result_archive).expanduser().resolve()
    if not archive_path.is_file():
        raise ReleaseTaskError(f"UOS 构建结果包不存在：{archive_path}")
    try:
        with zipfile.ZipFile(archive_path) as archive:
            members = _validated_zip_members(archive)
            metadata = members.get(UOS_RESULT_FILE)
            if metadata is None or metadata.is_dir():
                raise ReleaseTaskError("UOS 构建结果缺少结果元数据")
            if metadata.file_size > 2 * 1024 * 1024:
                raise ReleaseTaskError("UOS 构建结果元数据过大")
            payload = validate_uos_result_payload(
                json.loads(archive.read(metadata).decode("utf-8"))
            )
            expected = expected_uos_result_artifacts(payload)
            if set(payload["artifacts"]) != expected:
                raise ReleaseTaskError("UOS 构建结果的增量产物集合不完整")
    except ReleaseTaskError:
        raise
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        zipfile.BadZipFile,
        RuntimeError,
    ) as exc:
        raise ReleaseTaskError(f"无法读取 UOS 构建结果：{archive_path}") from exc
    return str(payload["uos_delta_from_version"])


def uos_result_archive_path(root: Path, version: str, commit: str) -> Path:
    return (
        root
        / "dist"
        / "uos-build-results"
        / version
        / f"uos-build-result-{version}-{commit[:12]}.zip"
    )


def export_uos_result(
    root: Path,
    *,
    version: str,
    base_url: str,
    channel: str,
    ca_bundle: str,
    uos_delta_from_version: str = "",
    output: Path | None = None,
    allow_detached: bool = False,
) -> Path:
    validated_receipt_path = record_uos_result(
        root,
        version=version,
        base_url=base_url,
        channel=channel,
        ca_bundle=ca_bundle,
        uos_delta_from_version=uos_delta_from_version,
        allow_detached=allow_detached,
    )
    receipt = load_json_object(validated_receipt_path, "UOS 构建收据")
    receipt_artifacts = receipt.get("artifacts", {})
    if not isinstance(receipt_artifacts, dict):
        raise ReleaseTaskError("UOS 构建收据缺少 artifacts")
    artifact_paths = {
        key: receipt_path(root, descriptor, f"UOS/{key}")
        for key, descriptor in receipt_artifacts.items()
    }
    uos_update_kind = str(receipt.get("uos_update_kind") or "").strip()
    if not uos_update_kind:
        if "uos_delta" in artifact_paths:
            uos_update_kind = "delta"
        elif "uos_file" in artifact_paths:
            uos_update_kind = "file"
        elif "uos_layer" in artifact_paths:
            uos_update_kind = "layered"
        else:
            uos_update_kind = "full"
    result_archive = (
        output.expanduser().resolve()
        if output is not None
        else uos_result_archive_path(root, version, str(receipt["source_commit"]))
    )
    with tempfile.TemporaryDirectory(prefix="intdemo-uos-result-") as temporary:
        staging = Path(temporary)
        target_names = {
            "uos_installer": f"IntDemo-UOS-arm64-{version}.deb",
        }
        if uos_delta_from_version:
            if uos_update_kind == "delta":
                patch_name = (
                    f"IntDemo-UOS-arm64-Patch-{uos_delta_from_version}"
                    f"-to-{version}.intdelta"
                )
                target_names.update(
                    {
                        "uos_delta": patch_name,
                        "uos_delta_report": f"{patch_name}.json",
                    }
                )
            elif uos_update_kind == "file" or "uos_file_report" in artifact_paths:
                file_name = (
                    f"IntDemo-UOS-arm64-Files-{uos_delta_from_version}"
                    f"-to-{version}.intlayer"
                )
                target_names["uos_file_report"] = f"{file_name}.json"
                if uos_update_kind == "file":
                    target_names["uos_file"] = file_name
            else:
                layer_name = (
                    f"IntDemo-UOS-arm64-Layers-{uos_delta_from_version}"
                    f"-to-{version}.intlayer"
                )
                target_names["uos_layer_report"] = f"{layer_name}.json"
                if uos_update_kind == "layered":
                    target_names["uos_layer"] = layer_name
        if set(artifact_paths) != set(target_names):
            raise ReleaseTaskError("UOS 构建收据的产物集合与增量配置不一致")
        staged_artifacts: dict[str, Path] = {}
        for key, name in target_names.items():
            target = staging / "artifacts" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(artifact_paths[key], target)
            staged_artifacts[key] = target
        result = validate_uos_result_payload(
            {
                "schema_version": 1,
                "platform": "linux-aarch64",
                "version": version,
                "source_commit": receipt["source_commit"],
                "base_url": receipt["base_url"],
                "channel": receipt["channel"],
                "ca_sha256": receipt["ca_sha256"],
                "payload_validated": True,
                "uos_delta_from_version": uos_delta_from_version,
                "uos_update_kind": uos_update_kind,
                "built_at": receipt.get("validated_at") or utc_now(),
                "builder": {
                    "kind": "uos-native",
                    "os": platform.platform(),
                    "machine": platform.machine(),
                    "python": platform.python_version(),
                },
                "artifacts": {
                    key: artifact_descriptor(path, f"artifacts/{path.name}")
                    for key, path in staged_artifacts.items()
                },
            }
        )
        write_json(staging / UOS_RESULT_FILE, result)
        make_zip(staging, result_archive)
    write_bytes_atomic(
        result_archive.with_suffix(result_archive.suffix + ".sha256"),
        f"{sha256(result_archive)}  {result_archive.name}\n".encode("ascii"),
    )
    print(f"UOS 构建结果包：{result_archive}", flush=True)
    print(f"SHA-256：{sha256(result_archive)}", flush=True)
    return result_archive


def import_uos_result(
    root: Path,
    *,
    result_archive: Path,
    version: str,
    base_url: str,
    channel: str,
    ca_bundle: str,
    uos_delta_from_version: str = "",
) -> Path:
    _branch, commit = source_state(root)
    result_archive = result_archive.expanduser().resolve()
    if not result_archive.is_file():
        raise ReleaseTaskError(f"UOS 构建结果包不存在：{result_archive}")
    if current_version(root) != version:
        raise ReleaseTaskError("当前项目版本与待导入 UOS 结果不一致")
    with tempfile.TemporaryDirectory(prefix="intdemo-uos-import-") as temporary:
        extracted = Path(temporary)
        extract_zip_safely(result_archive, extracted)
        result = validate_uos_result_payload(
            load_json_object(extracted / UOS_RESULT_FILE, "UOS 构建结果元数据")
        )
        expected = {
            "version": version,
            "source_commit": commit,
            "base_url": normalize_base_url(base_url),
            "channel": channel,
            "ca_sha256": ca_fingerprint(ca_bundle),
            "uos_delta_from_version": str(uos_delta_from_version or ""),
        }
        for key, value in expected.items():
            if result.get(key) != value:
                raise ReleaseTaskError(
                    f"UOS 构建结果 {key} 与当前配置不一致："
                    f"{result.get(key)!r} != {value!r}"
                )
        required_artifacts = expected_uos_result_artifacts(result)
        if set(result["artifacts"]) != required_artifacts:
            raise ReleaseTaskError("UOS 构建结果的产物集合不完整")
        verified = {
            key: verify_file(extracted, result["artifacts"][key], f"UOS/{key}")
            for key in required_artifacts
        }
        source = verified["uos_installer"]
        expected_name = f"IntDemo-UOS-arm64-{version}.deb"
        if source.name != expected_name:
            raise ReleaseTaskError(f"UOS 构建结果文件名不符合约定：{source.name}")
        target = root / "dist" / "uos-arm64" / expected_name
        copy_atomic(source, target)
        imported_artifacts = {"uos_installer": target}
        if result["uos_update_kind"] == "delta":
            patch_name = (
                f"IntDemo-UOS-arm64-Patch-{uos_delta_from_version}"
                f"-to-{version}.intdelta"
            )
            patch_target = root / "dist" / "uos-arm64" / patch_name
            report_target = patch_target.with_suffix(patch_target.suffix + ".json")
            if verified["uos_delta"].name != patch_name:
                raise ReleaseTaskError("UOS 增量包文件名不符合约定")
            if verified["uos_delta_report"].name != report_target.name:
                raise ReleaseTaskError("UOS 增量报告文件名不符合约定")
            copy_atomic(verified["uos_delta"], patch_target)
            copy_atomic(verified["uos_delta_report"], report_target)
            imported_artifacts.update(
                {
                    "uos_delta": patch_target,
                    "uos_delta_report": report_target,
                }
            )
            validate_uos_delta_candidate(
                root,
                from_version=uos_delta_from_version,
                target_version=version,
                target_deb=target,
            )
        elif uos_delta_from_version and (
            result["uos_update_kind"] == "file" or "uos_file_report" in verified
        ):
            file_name = (
                f"IntDemo-UOS-arm64-Files-{uos_delta_from_version}"
                f"-to-{version}.intlayer"
            )
            report_target = root / "dist" / "uos-arm64" / f"{file_name}.json"
            if verified["uos_file_report"].name != report_target.name:
                raise ReleaseTaskError("UOS 逐文件报告文件名不符合约定")
            copy_atomic(verified["uos_file_report"], report_target)
            imported_artifacts["uos_file_report"] = report_target
            file_target = root / "dist" / "uos-arm64" / file_name
            if result["uos_update_kind"] == "file":
                if verified["uos_file"].name != file_name:
                    raise ReleaseTaskError("UOS 逐文件更新包文件名不符合约定")
                copy_atomic(verified["uos_file"], file_target)
                imported_artifacts["uos_file"] = file_target
            else:
                file_target.unlink(missing_ok=True)
            file_update, _report_path, _report = validate_uos_file_candidate(
                root,
                from_version=uos_delta_from_version,
                target_version=version,
                target_deb=target,
            )
            if (file_update is not None) != (
                result["uos_update_kind"] == "file"
            ):
                raise ReleaseTaskError("UOS 逐文件报告与结果包更新类型不一致")
        elif uos_delta_from_version:
            layer_name = (
                f"IntDemo-UOS-arm64-Layers-{uos_delta_from_version}"
                f"-to-{version}.intlayer"
            )
            report_target = (
                root / "dist" / "uos-arm64" / f"{layer_name}.json"
            )
            if verified["uos_layer_report"].name != report_target.name:
                raise ReleaseTaskError("UOS 分层报告文件名不符合约定")
            copy_atomic(verified["uos_layer_report"], report_target)
            imported_artifacts["uos_layer_report"] = report_target
            layer_target = root / "dist" / "uos-arm64" / layer_name
            if result["uos_update_kind"] == "layered":
                if verified["uos_layer"].name != layer_name:
                    raise ReleaseTaskError("UOS 分层更新包文件名不符合约定")
                copy_atomic(verified["uos_layer"], layer_target)
                imported_artifacts["uos_layer"] = layer_target
            else:
                # A verified fallback result explicitly contains no layer.
                # Remove only the exact same source/target candidate so an
                # older local attempt cannot contradict the imported report.
                layer_target.unlink(missing_ok=True)
            layer, _report_path, _report = validate_uos_layer_candidate(
                root,
                from_version=uos_delta_from_version,
                target_version=version,
                target_deb=target,
            )
            if (layer is not None) != (result["uos_update_kind"] == "layered"):
                raise ReleaseTaskError("UOS 分层报告与结果包更新类型不一致")
        if shutil.which("dpkg-deb") is not None:
            validate_uos_deb_payload(
                root,
                target,
                version=version,
                commit=commit,
                base_url=expected["base_url"],
                channel=channel,
                ca_hash=expected["ca_sha256"],
            )
        write_bytes_atomic(
            target.with_suffix(target.suffix + ".sha256"),
            f"{sha256(target)}  {target.name}\n".encode("ascii"),
        )
        receipt = {
            "schema_version": 1,
            "platform": "linux-aarch64",
            "version": version,
            "source_commit": commit,
            "base_url": expected["base_url"],
            "channel": channel,
            "ca_sha256": expected["ca_sha256"],
            "result_archive_sha256": sha256(result_archive),
            "validated_at": utc_now(),
            "builder": result["builder"],
            "uos_delta_from_version": str(uos_delta_from_version or ""),
            "uos_update_kind": result["uos_update_kind"],
            "artifacts": {
                key: receipt_artifact(root, path)
                for key, path in imported_artifacts.items()
            },
        }
        receipt_path = (
            root / "dist" / "uos-build-results" / version / "validated-result.json"
        )
        write_json(receipt_path, receipt)
    print(f"UOS 构建结果已导入并校验：{receipt_path}", flush=True)
    return receipt_path


def load_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseTaskError(f"无法读取{label}：{path}") from exc
    if not isinstance(payload, dict):
        raise ReleaseTaskError(f"{label}根节点必须是对象")
    return payload


def receipt_path(root: Path, descriptor: Any, label: str) -> Path:
    if not isinstance(descriptor, dict):
        raise ReleaseTaskError(f"{label}收据描述无效")
    relative = str(descriptor.get("path") or "")
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts or "\\" in relative:
        raise ReleaseTaskError(f"{label}收据路径不安全：{relative}")
    candidate = (root / pure).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise ReleaseTaskError(f"{label}收据路径超出仓库：{candidate}") from exc
    if not candidate.is_file():
        raise ReleaseTaskError(f"{label}候选产物不存在：{candidate}")
    try:
        expected_size = int(descriptor.get("size"))
    except (TypeError, ValueError) as exc:
        raise ReleaseTaskError(f"{label}收据大小无效") from exc
    expected_hash = str(descriptor.get("sha256") or "").casefold()
    if candidate.stat().st_size != expected_size or sha256(candidate) != expected_hash:
        raise ReleaseTaskError(f"{label}候选产物已在校验后发生变化：{candidate}")
    return candidate


def validate_platform_receipt(
    root: Path,
    *,
    path: Path,
    platform_key: str,
    version: str,
    commit: str,
    base_url: str,
    channel: str,
    ca_hash: str,
) -> tuple[dict[str, Any], dict[str, Path]]:
    receipt = load_json_object(path, f"{platform_key} 构建收据")
    expected = {
        "schema_version": 1,
        "platform": platform_key,
        "version": version,
        "source_commit": commit,
        "base_url": normalize_base_url(base_url),
        "channel": channel,
        "ca_sha256": ca_hash,
    }
    for key, value in expected.items():
        actual = receipt.get(key)
        if actual != value:
            raise ReleaseTaskError(
                f"{platform_key} 构建收据 {key} 不匹配："
                f"{receipt.get(key)!r} != {value!r}"
            )
    artifacts = receipt.get("artifacts")
    if not isinstance(artifacts, dict):
        raise ReleaseTaskError(f"{platform_key} 构建收据缺少 artifacts")
    paths = {
        key: receipt_path(root, descriptor, f"{platform_key}/{key}")
        for key, descriptor in artifacts.items()
    }
    return receipt, paths


def load_release_candidates(
    root: Path,
    *,
    version: str,
    base_url: str,
    channel: str,
    ca_bundle: str,
    delta_from_version: str,
    build_portable: bool,
    uos_delta_from_version: str = "",
) -> tuple[dict[str, Any], dict[str, Path], dict[str, Any], dict[str, Path]]:
    _branch, commit = source_state(root)
    if current_version(root) != version:
        raise ReleaseTaskError("当前项目版本与待发布版本不一致")
    ca_hash = ca_fingerprint(ca_bundle)
    windows_receipt, windows_paths = validate_platform_receipt(
        root,
        path=(
            root
            / "dist"
            / "windows-build-results"
            / version
            / "validated-result.json"
        ),
        platform_key="windows-x86_64",
        version=version,
        commit=commit,
        base_url=base_url,
        channel=channel,
        ca_hash=ca_hash,
    )
    uos_receipt, uos_paths = validate_platform_receipt(
        root,
        path=(
            root / "dist" / "uos-build-results" / version / "validated-result.json"
        ),
        platform_key="linux-aarch64",
        version=version,
        commit=commit,
        base_url=base_url,
        channel=channel,
        ca_hash=ca_hash,
    )
    if windows_receipt.get("delta_from_version") != delta_from_version:
        raise ReleaseTaskError("Windows 构建收据的增量来源与当前发布配置不一致")
    if windows_receipt.get("build_portable") is not bool(build_portable):
        raise ReleaseTaskError("Windows 构建收据的便携包选项与当前发布配置不一致")
    required_windows = {"windows_installer", "windows_snapshot"}
    if delta_from_version:
        required_windows.add("windows_delta")
    if build_portable:
        required_windows.add("windows_portable")
    if set(windows_paths) != required_windows:
        raise ReleaseTaskError("Windows 构建收据的产物集合不完整")
    if str(uos_receipt.get("uos_delta_from_version") or "") != str(
        uos_delta_from_version or ""
    ):
        raise ReleaseTaskError("UOS 构建收据的增量来源与当前发布配置不一致")
    uos_update_kind = str(uos_receipt.get("uos_update_kind") or "").strip()
    if not uos_update_kind:
        uos_update_kind = "delta" if "uos_delta" in uos_paths else "full"
    required_uos = {"uos_installer"}
    if uos_update_kind == "delta":
        required_uos.update({"uos_delta", "uos_delta_report"})
    elif uos_update_kind == "file":
        required_uos.update({"uos_file", "uos_file_report"})
    elif uos_update_kind == "layered":
        required_uos.update({"uos_layer", "uos_layer_report"})
    elif uos_delta_from_version:
        required_uos.add(
            "uos_layer_report"
            if "uos_layer_report" in uos_paths
            else "uos_file_report"
        )
    if uos_update_kind not in {"full", "file", "layered", "delta"}:
        raise ReleaseTaskError("UOS 构建收据的更新类型无效")
    if set(uos_paths) != required_uos:
        raise ReleaseTaskError("UOS 构建收据的产物集合不完整")
    return windows_receipt, windows_paths, uos_receipt, uos_paths


def prepare_update_manifest(
    *,
    version: str,
    channel: str,
    source_commit: str,
    notes: str,
    mandatory: bool,
    windows: dict[str, Any],
    uos: dict[str, Any],
    delta: dict[str, Any] | None,
    delta_from_version: str,
    uos_delta: dict[str, Any] | None = None,
    uos_file: dict[str, Any] | None = None,
    uos_layer: dict[str, Any] | None = None,
    uos_delta_from_version: str = "",
    eligible_client_versions: tuple[str, ...] | list[str] = (),
) -> dict[str, Any]:
    eligible_versions = normalize_eligible_client_versions(
        eligible_client_versions,
        target_version=version,
    )
    windows_full = {
        "installer_path": f"/updates/files/{windows['name']}",
        "sha256": windows["sha256"],
        "size": int(windows["size"]),
    }
    windows_platform: dict[str, Any] = {"full": dict(windows_full)}
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "channel": channel,
        "version": version,
        "published_at": utc_now(),
        "source_commit": source_commit,
        "source_dirty": False,
        "installer_path": windows_full["installer_path"],
        "sha256": windows_full["sha256"],
        "size": windows_full["size"],
        "primary_kind": "full",
        "mandatory": bool(mandatory),
        "notes": notes,
        "platforms": {
            "windows-x86_64": windows_platform,
            "linux-aarch64": {
                "full": {
                    "installer_path": f"/updates/files/{uos['name']}",
                    "sha256": uos["sha256"],
                    "size": int(uos["size"]),
                }
            },
        },
    }
    if eligible_versions:
        manifest["eligible_client_versions"] = list(eligible_versions)
    if delta is not None:
        delta_payload = {
            "from_version": delta_from_version,
            "installer_path": f"/updates/files/{delta['name']}",
            "sha256": delta["sha256"],
            "size": int(delta["size"]),
        }
        manifest["full"] = dict(windows_full)
        manifest["deltas"] = [dict(delta_payload)]
        windows_platform["deltas"] = [dict(delta_payload)]
    if uos_delta is not None:
        uos_full = manifest["platforms"]["linux-aarch64"]["full"]
        if not uos_delta_from_version:
            raise ReleaseTaskError("UOS 增量包缺少来源版本")
        if version_key(uos_delta_from_version) >= version_key(version):
            raise ReleaseTaskError("UOS 增量来源版本必须低于目标版本")
        base_sha256 = str(uos_delta.get("base_sha256") or "").casefold()
        patch_sha256 = str(uos_delta.get("sha256") or "").casefold()
        try:
            base_size = int(uos_delta.get("base_size"))
            patch_size = int(uos_delta.get("size"))
        except (TypeError, ValueError) as exc:
            raise ReleaseTaskError("UOS 增量包大小字段无效") from exc
        if (
            not re.fullmatch(r"[0-9a-f]{64}", base_sha256)
            or not re.fullmatch(r"[0-9a-f]{64}", patch_sha256)
            or base_size <= 0
            or patch_size <= 0
        ):
            raise ReleaseTaskError("UOS 增量包哈希或大小字段无效")
        if patch_size * 2 >= int(uos_full["size"]):
            raise ReleaseTaskError("UOS 增量包未达到小于完整 DEB 50% 的发布门槛")
        uos_delta_payload = {
            "format": "uos-deb-xdelta-v1",
            "algorithm": "xdelta3",
            "from_version": uos_delta_from_version,
            "base_sha256": base_sha256,
            "base_size": base_size,
            "installer_path": f"/updates/files/{uos_delta['name']}",
            "sha256": patch_sha256,
            "size": patch_size,
            "target_sha256": str(uos_full["sha256"]),
            "target_size": int(uos_full["size"]),
        }
        manifest["platforms"]["linux-aarch64"]["deltas"] = [
            uos_delta_payload
        ]
    if uos_file is not None:
        uos_full = manifest["platforms"]["linux-aarch64"]["full"]
        if not uos_delta_from_version:
            raise ReleaseTaskError("UOS 逐文件更新包缺少来源版本")
        if version_key(uos_delta_from_version) >= version_key(version):
            raise ReleaseTaskError("UOS 逐文件来源版本必须低于目标版本")
        file_sha256 = str(uos_file.get("sha256") or "").casefold()
        source_layout_sha256 = str(
            uos_file.get("source_layout_sha256") or ""
        ).casefold()
        target_layout_sha256 = str(
            uos_file.get("target_layout_sha256") or ""
        ).casefold()
        try:
            file_size = int(uos_file.get("size"))
        except (TypeError, ValueError) as exc:
            raise ReleaseTaskError("UOS 逐文件更新包大小字段无效") from exc
        if (
            not re.fullmatch(r"[0-9a-f]{64}", file_sha256)
            or not re.fullmatch(r"[0-9a-f]{64}", source_layout_sha256)
            or not re.fullmatch(r"[0-9a-f]{64}", target_layout_sha256)
            or file_size <= 0
        ):
            raise ReleaseTaskError("UOS 逐文件更新包哈希或大小字段无效")
        if file_size * 2 >= int(uos_full["size"]):
            raise ReleaseTaskError("UOS 逐文件更新包未达到小于完整 DEB 50% 的发布门槛")
        manifest["platforms"]["linux-aarch64"]["file_updates"] = [
            {
                "format": UOS_FILE_UPDATE_CAPABILITY,
                "from_version": uos_delta_from_version,
                "source_layout_sha256": source_layout_sha256,
                "target_layout_sha256": target_layout_sha256,
                "installer_path": f"/updates/files/{uos_file['name']}",
                "sha256": file_sha256,
                "size": file_size,
                "target_sha256": str(uos_full["sha256"]),
                "target_size": int(uos_full["size"]),
            }
        ]
    if uos_layer is not None:
        uos_full = manifest["platforms"]["linux-aarch64"]["full"]
        if not uos_delta_from_version:
            raise ReleaseTaskError("UOS 分层更新包缺少来源版本")
        if version_key(uos_delta_from_version) >= version_key(version):
            raise ReleaseTaskError("UOS 分层来源版本必须低于目标版本")
        layer_sha256 = str(uos_layer.get("sha256") or "").casefold()
        source_layout_sha256 = str(
            uos_layer.get("source_layout_sha256") or ""
        ).casefold()
        target_layout_sha256 = str(
            uos_layer.get("target_layout_sha256") or ""
        ).casefold()
        try:
            layer_size = int(uos_layer.get("size"))
        except (TypeError, ValueError) as exc:
            raise ReleaseTaskError("UOS 分层更新包大小字段无效") from exc
        if (
            not re.fullmatch(r"[0-9a-f]{64}", layer_sha256)
            or not re.fullmatch(r"[0-9a-f]{64}", source_layout_sha256)
            or not re.fullmatch(r"[0-9a-f]{64}", target_layout_sha256)
            or layer_size <= 0
        ):
            raise ReleaseTaskError("UOS 分层更新包哈希或大小字段无效")
        if layer_size * 2 >= int(uos_full["size"]):
            raise ReleaseTaskError("UOS 分层更新包未达到小于完整 DEB 50% 的发布门槛")
        manifest["platforms"]["linux-aarch64"]["layered_updates"] = [
            {
                "format": "uos-layered-v1",
                "from_version": uos_delta_from_version,
                "source_layout_sha256": source_layout_sha256,
                "target_layout_sha256": target_layout_sha256,
                "installer_path": f"/updates/files/{uos_layer['name']}",
                "sha256": layer_sha256,
                "size": layer_size,
                "target_sha256": str(uos_full["sha256"]),
                "target_size": int(uos_full["size"]),
            }
        ]
    return manifest


def stage_release_artifact(source: Path, target: Path) -> dict[str, Any]:
    copy_atomic(source, target)
    return {
        "name": target.name,
        "path": target,
        "size": target.stat().st_size,
        "sha256": sha256(target),
    }


def ssh_options(identity_file: Path | None) -> list[str]:
    options: list[str] = []
    for value in SSH_CONNECTION_OPTIONS:
        options.extend(("-o", value))
    if identity_file is not None:
        options.extend(("-i", str(identity_file)))
    return options


def ssh_capture(
    root: Path,
    host: str,
    command: str,
    identity_file: Path | None,
    *,
    timeout: float = SSH_COMMAND_TIMEOUT_SECONDS,
) -> str:
    return capture_command(
        ["ssh", *ssh_options(identity_file), host, command],
        cwd=root,
        timeout=timeout,
    )


def remote_hash(
    root: Path,
    host: str,
    path: str,
    identity_file: Path | None,
) -> str:
    quoted = shlex.quote(path)
    return ssh_capture(
        root,
        host,
        f"if [ -f {quoted} ]; then sha256sum {quoted} | cut -d ' ' -f 1; fi",
        identity_file,
        timeout=SSH_HASH_TIMEOUT_SECONDS,
    ).strip().casefold()


def remote_file_size(
    root: Path,
    host: str,
    path: str,
    identity_file: Path | None,
) -> int | None:
    quoted = shlex.quote(path)
    value = ssh_capture(
        root,
        host,
        f"if [ -f {quoted} ]; then wc -c < {quoted}; fi",
        identity_file,
        timeout=SSH_PROGRESS_QUERY_TIMEOUT_SECONDS,
    ).strip()
    if not value:
        return None
    if not value.isdigit():
        raise ReleaseTaskError(f"无法读取远程断点文件大小：{path}")
    return int(value)


def release_upload_fingerprint(
    version: str,
    source_commit: str,
    artifacts: dict[str, dict[str, Any]],
) -> str:
    artifact_state = sorted(
        (
            {
                "name": str(descriptor["name"]),
                "size": int(descriptor["size"]),
                "sha256": str(descriptor["sha256"]).casefold(),
            }
            for descriptor in artifacts.values()
        ),
        key=lambda item: (item["name"], item["size"], item["sha256"]),
    )
    state = {
        "version": version,
        "source_commit": source_commit.casefold(),
        "artifacts": artifact_state,
    }
    encoded = json.dumps(
        state,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def release_incoming_path(
    remote_path: str,
    version: str,
    source_commit: str,
    artifacts: dict[str, dict[str, Any]],
) -> str:
    fingerprint = release_upload_fingerprint(version, source_commit, artifacts)
    return (
        f"{remote_path}/.incoming/{version}-{source_commit[:12]}-"
        f"{fingerprint}"
    )


def format_upload_size(value: int) -> str:
    if value < 1024 * 1024:
        return f"{value / 1024:.1f} KiB"
    return f"{value / (1024 * 1024):.1f} MiB"


def print_upload_progress(name: str, uploaded: int, total: int) -> None:
    percentage = 100.0 if total == 0 else min(uploaded, total) / total * 100
    print(
        f"上传进度：{name} {format_upload_size(uploaded)} / "
        f"{format_upload_size(total)}（{percentage:.1f}%）",
        flush=True,
    )


def ensure_remote_partial(
    root: Path,
    host: str,
    path: str,
    identity_file: Path | None,
) -> None:
    quoted = shlex.quote(path)
    command = (
        f"if [ -e {quoted} ] && [ ! -f {quoted} ]; then exit 1; fi; "
        f"if [ ! -e {quoted} ]; then : > {quoted}; fi"
    )
    run_command(
        ["ssh", *ssh_options(identity_file), host, command],
        cwd=root,
        timeout=SSH_COMMAND_TIMEOUT_SECONDS,
    )


def truncate_remote_file(
    root: Path,
    host: str,
    path: str,
    identity_file: Path | None,
) -> None:
    run_command(
        [
            "ssh",
            *ssh_options(identity_file),
            host,
            f": > {shlex.quote(path)}",
        ],
        cwd=root,
        timeout=SSH_COMMAND_TIMEOUT_SECONDS,
    )


def sftp_argument(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def sftp_reput_once(
    root: Path,
    *,
    host: str,
    local_path: Path,
    remote_path: str,
    identity_file: Path | None,
    progress_interval: float = SFTP_PROGRESS_INTERVAL_SECONDS,
    stall_timeout: float = SFTP_STALL_TIMEOUT_SECONDS,
) -> None:
    local_value = local_path.resolve().as_posix()
    batch_line = (
        f"reput {sftp_argument(local_value)} "
        f"{sftp_argument(remote_path)}\n"
    )
    arguments: list[str]
    with tempfile.TemporaryDirectory(prefix="intdemo-sftp-") as directory:
        batch_path = Path(directory) / "upload.batch"
        with batch_path.open("w", encoding="utf-8", newline="\n") as batch:
            batch.write(batch_line)
        arguments = [
            "sftp",
            *ssh_options(identity_file),
            "-b",
            str(batch_path),
            host,
        ]
        with (
            tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stdout,
            tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stderr,
        ):
            try:
                process = subprocess.Popen(
                    arguments,
                    cwd=str(root),
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    stdout=stdout,
                    stderr=stderr,
                )
            except OSError as exc:
                raise ReleaseTaskError(f"无法启动命令 sftp：{exc}") from exc
            last_uploaded: int | None = None
            last_progress_at = time.monotonic()
            try:
                while True:
                    try:
                        returncode = process.wait(timeout=progress_interval)
                    except subprocess.TimeoutExpired:
                        try:
                            uploaded = remote_file_size(
                                root,
                                host,
                                remote_path,
                                identity_file,
                            )
                        except ReleaseTaskError:
                            print(
                                "远程上传进度暂时无法读取；"
                                "SFTP 仍在运行，将继续等待并自动重试。",
                                flush=True,
                            )
                        else:
                            if uploaded is not None and (
                                last_uploaded is None or uploaded > last_uploaded
                            ):
                                last_uploaded = uploaded
                                last_progress_at = time.monotonic()
                            if uploaded is not None:
                                print_upload_progress(
                                    local_path.name,
                                    uploaded,
                                    local_path.stat().st_size,
                                )
                        stalled_for = time.monotonic() - last_progress_at
                        if stalled_for >= stall_timeout:
                            raise ReleaseTaskError(
                                f"SFTP 连续 {int(stall_timeout)} 秒无可确认上传进度："
                                f"{local_path.name}"
                            )
                    else:
                        break
            except BaseException:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                raise
            stdout.seek(0)
            stderr.seek(0)
            stdout_text = stdout.read()
            stderr_text = stderr.read()
    if returncode != 0:
        raise CommandFailure(
            arguments,
            returncode,
            stdout_text,
            stderr_text,
        )


def upload_file_resumable(
    root: Path,
    *,
    host: str,
    local_path: Path,
    remote_path: str,
    expected_sha256: str,
    identity_file: Path | None,
    max_attempts: int = SFTP_UPLOAD_ATTEMPTS,
    retry_delays: tuple[int, ...] = SFTP_RETRY_DELAYS_SECONDS,
    progress_interval: float = SFTP_PROGRESS_INTERVAL_SECONDS,
) -> None:
    total = local_path.stat().st_size
    expected_hash = expected_sha256.casefold()
    last_error: ReleaseTaskError | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            ensure_remote_partial(
                root,
                host,
                remote_path,
                identity_file,
            )
            uploaded = remote_file_size(
                root,
                host,
                remote_path,
                identity_file,
            )
            if uploaded is None:
                raise ReleaseTaskError(
                    f"远程断点文件未能创建：{local_path.name}"
                )
            if uploaded > total:
                print(
                    f"远程断点大于本地文件，将重新上传：{local_path.name}",
                    flush=True,
                )
                truncate_remote_file(
                    root,
                    host,
                    remote_path,
                    identity_file,
                )
                uploaded = 0
            elif uploaded == total:
                if (
                    remote_hash(root, host, remote_path, identity_file)
                    == expected_hash
                ):
                    print(
                        f"远程断点已完整并通过校验，直接复用：{local_path.name}",
                        flush=True,
                    )
                    return
                print(
                    f"远程断点 SHA-256 不匹配，将重新上传：{local_path.name}",
                    flush=True,
                )
                truncate_remote_file(
                    root,
                    host,
                    remote_path,
                    identity_file,
                )
                uploaded = 0

            if uploaded:
                print(
                    f"从断点继续上传：{local_path.name}，已完成 "
                    f"{format_upload_size(uploaded)} / {format_upload_size(total)}",
                    flush=True,
                )
            else:
                print(f"开始上传：{local_path.name}", flush=True)
            sftp_reput_once(
                root,
                host=host,
                local_path=local_path,
                remote_path=remote_path,
                identity_file=identity_file,
                progress_interval=progress_interval,
            )
            uploaded = remote_file_size(
                root,
                host,
                remote_path,
                identity_file,
            )
            if uploaded != total:
                raise ReleaseTaskError(
                    f"上传后文件大小不完整：{local_path.name}"
                )
            if remote_hash(root, host, remote_path, identity_file) != expected_hash:
                truncate_remote_file(
                    root,
                    host,
                    remote_path,
                    identity_file,
                )
                raise ReleaseTaskError(
                    f"上传后 SHA-256 不匹配：{local_path.name}"
                )
            print_upload_progress(local_path.name, total, total)
            print(f"上传完成并通过 SHA-256 校验：{local_path.name}", flush=True)
            return
        except ReleaseTaskError as exc:
            last_error = exc
            if attempt >= max_attempts:
                break
            try:
                retained = remote_file_size(
                    root,
                    host,
                    remote_path,
                    identity_file,
                )
            except ReleaseTaskError:
                retained = None
            delay = retry_delays[min(attempt - 1, len(retry_delays) - 1)]
            retained_text = (
                f"已保留 {format_upload_size(retained)}"
                if retained is not None
                else "断点进度暂时无法读取"
            )
            print(
                f"上传中断（第 {attempt}/{max_attempts} 次）："
                f"{local_path.name}；{retained_text}，{delay} 秒后自动续传",
                flush=True,
            )
            time.sleep(delay)
    detail = f"；最后错误：{last_error}" if last_error is not None else ""
    raise ReleaseTaskError(
        f"上传多次中断：{local_path.name}。服务器上的已有断点不会删除；"
        f"网络恢复后重新点击发布会从当前进度继续{detail}"
    )


def remote_manifest_guard(payload: dict[str, Any] | None, target: str) -> bool:
    if not payload:
        return False
    paused = payload.get("paused") is True
    remote_version = str(
        payload.get("paused_version") if paused else payload.get("version") or ""
    ).strip()
    if not remote_version:
        raise ReleaseTaskError("远程更新清单缺少版本")
    if version_key(remote_version) >= version_key(target):
        raise ReleaseTaskError(
            f"远程通道已发布 {remote_version}；不能覆盖或降级已发布版本"
        )
    return paused


def request_manifest(
    base_url: str,
    channel: str,
    ca_bundle: Path | None,
    platform_key: str,
) -> tuple[int, dict[str, Any] | None, dict[str, str]]:
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/updates/{channel}.json",
        headers={
            "User-Agent": "IntDemoUpdater/0.0.0",
            "X-IntDemo-Version": "0.0.0",
            "X-IntDemo-Platform": platform_key,
        },
    )
    context = (
        ssl.create_default_context(cafile=str(ca_bundle))
        if ca_bundle is not None
        else ssl.create_default_context()
    )
    response = _urlopen_with_retry(
        request,
        context=context,
        error_label="无法读取在线更新清单",
    )
    with response:
        status = int(response.status)
        headers = {key.casefold(): value for key, value in response.headers.items()}
        body = response.read()
    if not body:
        return status, None, headers
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseTaskError("在线更新清单不是有效 JSON") from exc
    return status, payload if isinstance(payload, dict) else None, headers


def _urlopen_with_retry(
    request: urllib.request.Request,
    *,
    context: ssl.SSLContext,
    error_label: str,
):
    last_error: BaseException | None = None
    for attempt in range(len(UPDATE_HTTP_RETRY_DELAYS_SECONDS) + 1):
        try:
            return urllib.request.urlopen(request, timeout=20, context=context)
        except urllib.error.HTTPError as exc:
            return exc
        except (OSError, ssl.SSLError, urllib.error.URLError) as exc:
            reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
            if isinstance(reason, ssl.SSLCertVerificationError):
                raise ReleaseTaskError(
                    f"{error_label}（TLS 证书校验失败）：{exc}"
                ) from exc
            last_error = exc
            if attempt >= len(UPDATE_HTTP_RETRY_DELAYS_SECONDS):
                break
            delay = UPDATE_HTTP_RETRY_DELAYS_SECONDS[attempt]
            print(
                f"提示：{error_label}，将在 {delay} 秒后重试"
                f"（{attempt + 2}/{len(UPDATE_HTTP_RETRY_DELAYS_SECONDS) + 1}）：{exc}",
                flush=True,
            )
            time.sleep(delay)
    raise ReleaseTaskError(f"{error_label}：{last_error}") from last_error


def assert_platform_server_support(
    base_url: str,
    channel: str,
    ca_bundle: Path | None,
    *,
    require_source_version_targeting: bool = False,
    require_uos_layered: bool = False,
    require_uos_file: bool = False,
) -> None:
    if require_source_version_targeting or require_uos_layered or require_uos_file:
        request = urllib.request.Request(
            f"{base_url.rstrip('/')}/updates/capabilities.json",
            headers={"User-Agent": "IntDemoReleasePublisher/1"},
        )
        context = (
            ssl.create_default_context(cafile=str(ca_bundle))
            if ca_bundle is not None
            else ssl.create_default_context()
        )
        with _urlopen_with_retry(
            request,
            context=context,
            error_label="无法读取更新服务器能力",
        ) as response:
            status = int(response.status)
            body = response.read()
        try:
            capability_payload = json.loads(body.decode("utf-8")) if body else {}
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ReleaseTaskError("更新服务器能力响应不是有效 JSON") from exc
        capabilities = (
            capability_payload.get("capabilities")
            if isinstance(capability_payload, dict)
            else None
        )
        if (
            status != 200
            or not isinstance(capabilities, list)
        ):
            raise ReleaseTaskError("在线 API 没有返回有效的更新能力列表")
        if (
            require_source_version_targeting
            and SOURCE_VERSION_TARGETING_CAPABILITY not in capabilities
        ):
            raise ReleaseTaskError(
                "在线 API 尚未启用按客户端版本定向推送；"
                "必须先升级服务器，不能发布定向更新清单"
            )
        if require_uos_layered and UOS_LAYERED_UPDATE_CAPABILITY not in capabilities:
            raise ReleaseTaskError(
                "在线 API 尚未启用 UOS 三层更新选择；"
                "必须先升级服务器，不能发布分层更新包"
            )
        if require_uos_file and UOS_FILE_UPDATE_CAPABILITY not in capabilities:
            raise ReleaseTaskError(
                "在线 API 尚未启用 UOS 逐文件更新选择；"
                "必须先升级服务器，不能发布逐文件更新包"
            )
    status, _payload, headers = request_manifest(
        base_url,
        channel,
        ca_bundle,
        "linux-aarch64",
    )
    if status == 404:
        print("提示：当前通道尚无在线清单，将按首次发布处理。", flush=True)
        return
    if headers.get("x-intdemo-platform", "").casefold() != "linux-aarch64":
        raise ReleaseTaskError(
            "在线 API 尚未启用 X-IntDemo-Platform 双端清单选择，不能发布双端更新"
        )
    if status not in {200, 204}:
        raise ReleaseTaskError(f"在线更新服务预检返回 HTTP {status}")


def publish_remote(
    root: Path,
    *,
    version: str,
    source_commit: str,
    channel: str,
    manifest_path: Path,
    manifest: dict[str, Any],
    artifacts: dict[str, dict[str, Any]],
    host: str,
    remote_path: str,
    identity_file: Path | None,
) -> bool:
    incoming = release_incoming_path(
        remote_path,
        version,
        source_commit,
        artifacts,
    )
    run_command(
        [
            "ssh",
            *ssh_options(identity_file),
            host,
            f"mkdir -p {shlex.quote(remote_path + '/files')} {shlex.quote(incoming)}",
        ],
        cwd=root,
        timeout=SSH_COMMAND_TIMEOUT_SECONDS,
    )
    final_manifest = f"{remote_path}/{channel}.json"
    current_hash = remote_hash(root, host, final_manifest, identity_file)
    current_text = ssh_capture(
        root,
        host,
        (
            f"if [ -f {shlex.quote(final_manifest)} ]; "
            f"then cat {shlex.quote(final_manifest)}; fi"
        ),
        identity_file,
    ).strip()
    current_manifest = None
    if current_text:
        try:
            current_manifest = json.loads(current_text)
        except json.JSONDecodeError as exc:
            raise ReleaseTaskError("远程更新清单不是有效 JSON") from exc
        if not isinstance(current_manifest, dict):
            raise ReleaseTaskError("远程更新清单根节点必须是对象")
    if remote_hash(root, host, final_manifest, identity_file) != current_hash:
        raise ReleaseTaskError("读取期间远程更新清单已变化，请重试")
    was_paused = remote_manifest_guard(current_manifest, version)

    moves: list[tuple[str, str]] = []
    for descriptor in artifacts.values():
        name = str(descriptor["name"])
        target = f"{remote_path}/files/{name}"
        existing_hash = remote_hash(root, host, target, identity_file)
        if existing_hash == descriptor["sha256"]:
            print(f"远程安装包已匹配，跳过上传：{name}", flush=True)
            continue
        if existing_hash:
            raise ReleaseTaskError(
                f"远程已存在同名但 SHA-256 不同的安装包，拒绝覆盖：{name}"
            )
        temporary = f"{incoming}/{name}.part"
        upload_file_resumable(
            root,
            host=host,
            local_path=Path(descriptor["path"]),
            remote_path=temporary,
            expected_sha256=str(descriptor["sha256"]),
            identity_file=identity_file,
        )
        moves.append((temporary, target))

    incoming_manifest = f"{incoming}/{channel}.json.part"
    upload_file_resumable(
        root,
        host=host,
        local_path=manifest_path,
        remote_path=incoming_manifest,
        expected_sha256=sha256(manifest_path),
        identity_file=identity_file,
    )
    guard = (
        f"echo {shlex.quote(current_hash + '  ' + final_manifest)} | "
        "sha256sum -c - >/dev/null"
        if current_hash
        else f"test ! -e {shlex.quote(final_manifest)}"
    )
    commands = [guard]
    commands.extend(
        f"mv {shlex.quote(source)} {shlex.quote(target)}"
        for source, target in moves
    )
    commands.append(
        f"mv {shlex.quote(incoming_manifest)} {shlex.quote(final_manifest)}"
    )
    commands.append(
        f"{{ rmdir {shlex.quote(incoming)} 2>/dev/null || true; }}"
    )
    run_command(
        ["ssh", *ssh_options(identity_file), host, " && ".join(commands)],
        cwd=root,
        timeout=SSH_COMMAND_TIMEOUT_SECONDS,
    )
    if remote_hash(root, host, final_manifest, identity_file) != sha256(manifest_path):
        raise ReleaseTaskError("发布后的远程清单 SHA-256 不匹配")
    installed = load_remote_json(root, host, final_manifest, identity_file)
    if installed != manifest:
        raise ReleaseTaskError("发布后的远程清单与本地清单不一致")
    return was_paused


def load_remote_json(
    root: Path,
    host: str,
    path: str,
    identity_file: Path | None,
) -> dict[str, Any]:
    text = ssh_capture(root, host, f"cat {shlex.quote(path)}", identity_file)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ReleaseTaskError(f"远程 JSON 无效：{path}") from exc
    if not isinstance(payload, dict):
        raise ReleaseTaskError(f"远程 JSON 根节点必须是对象：{path}")
    return payload


def publish_release(
    root: Path,
    *,
    version: str,
    base_url: str,
    channel: str,
    ca_bundle: str,
    delta_from_version: str,
    build_portable: bool,
    uos_delta_from_version: str = "",
    eligible_client_versions: tuple[str, ...] | list[str] = (),
    notes: str,
    mandatory: bool,
    remote_host: str,
    remote_path: str,
    identity_file_value: str,
    confirm_version: str,
) -> Path:
    version_key(version)
    eligible_versions = normalize_eligible_client_versions(
        eligible_client_versions,
        target_version=version,
    )
    normalized_url = normalize_base_url(base_url)
    if confirm_version != version:
        raise ReleaseTaskError(f"发布必须明确确认版本：--confirm-version {version}")
    if channel not in {"test", "stable"}:
        raise ReleaseTaskError("发布通道只能是 test 或 stable")
    notes = str(notes or "").strip()
    if not notes:
        raise ReleaseTaskError("更新说明不能为空")
    if not remote_host:
        raise ReleaseTaskError("双端正式发布必须填写更新服务器 SSH 主机")
    if not REMOTE_HOST_PATTERN.fullmatch(remote_host):
        raise ReleaseTaskError("更新服务器 SSH 主机格式无效")
    normalized_remote_path = safe_remote_path(remote_path)
    if shutil.which("ssh") is None or shutil.which("sftp") is None:
        raise ReleaseTaskError("远程发布需要系统提供 ssh 和 sftp")
    identity_file = None
    if identity_file_value:
        identity_file = Path(identity_file_value).expanduser().resolve()
        if not identity_file.is_file():
            raise ReleaseTaskError(f"SSH 私钥不存在：{identity_file}")
    snapshot_target = root / "dist" / "release-snapshots" / f"{version}.json"
    if snapshot_target.exists():
        raise ReleaseTaskError(f"版本 {version} 已有发布快照，不能覆盖已发布版本")

    windows_receipt, windows_paths, uos_receipt, uos_paths = (
        load_release_candidates(
            root,
            version=version,
            base_url=normalized_url,
            channel=channel,
            ca_bundle=ca_bundle,
            delta_from_version=delta_from_version,
            build_portable=build_portable,
            uos_delta_from_version=uos_delta_from_version,
        )
    )
    _branch, commit = source_state(root)
    ca_path = Path(ca_bundle).expanduser().resolve() if ca_bundle else None
    assert_platform_server_support(
        normalized_url,
        channel,
        ca_path,
        require_source_version_targeting=bool(eligible_versions),
        require_uos_layered=(
            str(uos_receipt.get("uos_update_kind") or "") == "layered"
            or "uos_layer" in uos_paths
        ),
        require_uos_file=(
            str(uos_receipt.get("uos_update_kind") or "") == "file"
            or "uos_file" in uos_paths
        ),
    )

    files_root = root / "dist" / "update-release" / "files"
    windows = stage_release_artifact(
        windows_paths["windows_installer"],
        files_root / f"IntDemoOnline-Setup-{version}.exe",
    )
    uos = stage_release_artifact(
        uos_paths["uos_installer"],
        files_root / f"IntDemo-UOS-arm64-{version}.deb",
    )
    prepared = {
        "windows_installer": windows,
        "uos_installer": uos,
    }
    delta = None
    if delta_from_version:
        delta = stage_release_artifact(
            windows_paths["windows_delta"],
            files_root
            / f"IntDemoOnline-Patch-{delta_from_version}-to-{version}.exe",
        )
        prepared["windows_delta"] = delta
    uos_delta = None
    uos_file = None
    uos_layer = None
    uos_update_kind = str(uos_receipt.get("uos_update_kind") or "").strip()
    if not uos_update_kind:
        if "uos_delta" in uos_paths:
            uos_update_kind = "delta"
        elif "uos_file" in uos_paths:
            uos_update_kind = "file"
        else:
            uos_update_kind = "full"
    if uos_delta_from_version and uos_update_kind == "delta":
        report = load_json_object(
            uos_paths["uos_delta_report"],
            "UOS 增量验证报告",
        )
        uos_delta = stage_release_artifact(
            uos_paths["uos_delta"],
            files_root
            / (
                f"IntDemo-UOS-arm64-Patch-{uos_delta_from_version}"
                f"-to-{version}.intdelta"
            ),
        )
        uos_delta["base_sha256"] = str(report.get("base_sha256") or "")
        uos_delta["base_size"] = int(report.get("base_size") or 0)
        prepared["uos_delta"] = uos_delta
    elif uos_delta_from_version and uos_update_kind == "file":
        report = load_json_object(
            uos_paths["uos_file_report"],
            "UOS 逐文件构建报告",
        )
        uos_file = stage_release_artifact(
            uos_paths["uos_file"],
            files_root
            / (
                f"IntDemo-UOS-arm64-Files-{uos_delta_from_version}"
                f"-to-{version}.intlayer"
            ),
        )
        uos_file["source_layout_sha256"] = str(
            report.get("source_layout_sha256") or ""
        )
        uos_file["target_layout_sha256"] = str(
            report.get("target_layout_sha256") or ""
        )
        prepared["uos_file"] = uos_file
    elif uos_delta_from_version and uos_update_kind == "layered":
        report = load_json_object(
            uos_paths["uos_layer_report"],
            "UOS 分层构建报告",
        )
        uos_layer = stage_release_artifact(
            uos_paths["uos_layer"],
            files_root
            / (
                f"IntDemo-UOS-arm64-Layers-{uos_delta_from_version}"
                f"-to-{version}.intlayer"
            ),
        )
        uos_layer["source_layout_sha256"] = str(
            report.get("source_layout_sha256") or ""
        )
        uos_layer["target_layout_sha256"] = str(
            report.get("target_layout_sha256") or ""
        )
        prepared["uos_layer"] = uos_layer
    manifest = prepare_update_manifest(
        version=version,
        channel=channel,
        source_commit=commit,
        notes=notes,
        mandatory=mandatory,
        windows=windows,
        uos=uos,
        delta=delta,
        delta_from_version=delta_from_version,
        uos_delta=uos_delta,
        uos_file=uos_file,
        uos_layer=uos_layer,
        uos_delta_from_version=uos_delta_from_version,
        eligible_client_versions=eligible_versions,
    )
    manifest_path = root / "dist" / "update-release" / f"{channel}.json"
    write_json(manifest_path, manifest)
    was_paused = publish_remote(
        root,
        version=version,
        source_commit=commit,
        channel=channel,
        manifest_path=manifest_path,
        manifest=manifest,
        artifacts=prepared,
        host=remote_host,
        remote_path=normalized_remote_path,
        identity_file=identity_file,
    )

    copy_atomic(windows_paths["windows_snapshot"], snapshot_target)
    publish_receipt = {
        "schema_version": 1,
        "version": version,
        "source_commit": commit,
        "published_at": utc_now(),
        "channel": channel,
        "remote_host": remote_host,
        "remote_path": normalized_remote_path,
        "manifest_sha256": sha256(manifest_path),
        "resumed_paused_distribution": was_paused,
        "windows_request_id": windows_receipt.get("request_id"),
        "uos_delta_from_version": str(uos_delta_from_version or ""),
        "uos_update_kind": uos_update_kind,
        "eligible_client_versions": list(eligible_versions),
        "artifacts": {
            key: {
                "name": value["name"],
                "size": value["size"],
                "sha256": value["sha256"],
            }
            for key, value in prepared.items()
        },
    }
    publish_receipt_path = (
        root / "dist" / "release-results" / version / "publish-receipt.json"
    )
    write_json(publish_receipt_path, publish_receipt)
    print(
        f"双端更新已发布：{remote_host}:{normalized_remote_path}",
        flush=True,
    )
    print(f"发布收据：{publish_receipt_path}", flush=True)
    return publish_receipt_path


def pause_distribution(
    root: Path,
    *,
    channel: str,
    remote_host: str,
    remote_path: str,
    identity_file_value: str,
) -> None:
    if channel not in {"test", "stable"}:
        raise ReleaseTaskError("暂停通道只能是 test 或 stable")
    if not REMOTE_HOST_PATTERN.fullmatch(remote_host):
        raise ReleaseTaskError("远程 SSH 主机格式无效")
    normalized_path = safe_remote_path(remote_path)
    identity_file = None
    if identity_file_value:
        identity_file = Path(identity_file_value).expanduser().resolve()
        if not identity_file.is_file():
            raise ReleaseTaskError(f"SSH 私钥不存在：{identity_file}")
    manifest_path = f"{normalized_path}/{channel}.json"
    before_hash = remote_hash(root, remote_host, manifest_path, identity_file)
    if not re.fullmatch(r"[0-9a-f]{64}", before_hash):
        raise ReleaseTaskError(f"远程没有有效的 {channel} 活动更新清单")
    active = load_remote_json(root, remote_host, manifest_path, identity_file)
    if int(active.get("schema_version") or 0) != 1 or active.get("channel") != channel:
        raise ReleaseTaskError("远程活动清单格式或通道不匹配")
    after_hash = remote_hash(root, remote_host, manifest_path, identity_file)
    if after_hash != before_hash:
        raise ReleaseTaskError("检查期间远程活动清单已变化，请重试")
    if active.get("paused") is True:
        print(
            f"{channel} 通道已经暂停于版本 {active.get('paused_version')}",
            flush=True,
        )
        return
    active_version = str(active.get("version") or "")
    version_key(active_version)
    paused_at = datetime.now(timezone.utc)
    timestamp = paused_at.strftime("%Y%m%dT%H%M%SZ")
    marker = {
        "schema_version": 1,
        "channel": channel,
        "version": "0.0.0",
        "paused": True,
        "paused_version": active_version,
        "paused_at": paused_at.replace(microsecond=0).isoformat().replace(
            "+00:00", "Z"
        ),
    }
    archive_root = f"{normalized_path}-paused"
    archive_name = (
        f"{channel}-{active_version}-{timestamp}-{before_hash[:12]}.json"
    )
    incoming_root = f"{normalized_path}/.incoming"
    incoming_path = (
        f"{incoming_root}/{channel}-pause-{timestamp}-{uuid.uuid4().hex}.json"
    )
    with tempfile.TemporaryDirectory(prefix="intdemo-pause-") as temporary:
        local_marker = Path(temporary) / f"{channel}.json"
        write_json(local_marker, marker)
        run_command(
            [
                "ssh",
                *ssh_options(identity_file),
                remote_host,
                (
                    f"mkdir -p {shlex.quote(incoming_root)} "
                    f"{shlex.quote(archive_root)}"
                ),
            ],
            cwd=root,
            timeout=SSH_COMMAND_TIMEOUT_SECONDS,
        )
        run_command(
            [
                "scp",
                *ssh_options(identity_file),
                str(local_marker),
                f"{remote_host}:{incoming_path}",
            ],
            cwd=root,
            timeout=SSH_COMMAND_TIMEOUT_SECONDS,
        )
        command = (
            f"echo {shlex.quote(before_hash + '  ' + manifest_path)} | "
            "sha256sum -c - >/dev/null"
            f" && cp {shlex.quote(manifest_path)} "
            f"{shlex.quote(archive_root + '/' + archive_name)}"
            f" && mv {shlex.quote(incoming_path)} {shlex.quote(manifest_path)}"
        )
        run_command(
            ["ssh", *ssh_options(identity_file), remote_host, command],
            cwd=root,
            timeout=SSH_COMMAND_TIMEOUT_SECONDS,
        )
    print(f"已暂停 {channel} 通道，原版本：{active_version}", flush=True)
    print(f"安装包仍保留在：{normalized_path}/files", flush=True)


def infer_github_repository(remote_url: str) -> str:
    match = re.search(
        r"github\.com(?::|/)(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)"
        r"(?:\.git)?$",
        remote_url.strip(),
        re.IGNORECASE,
    )
    if not match:
        raise ReleaseTaskError(
            "无法从 Git 远程推断 GitHub 仓库，请传入 --github-repo owner/repo"
        )
    return match.group("repo")


def gh_json(root: Path, repository: str, endpoint: str) -> dict[str, Any]:
    try:
        content = capture_command(
            ["gh", "api", "--method", "GET", f"repos/{repository}/{endpoint}"],
            cwd=root,
        )
    except ReleaseTaskError as exc:
        raise GithubUnavailable(f"无法连接 GitHub API：{exc}") from exc
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise GithubUnavailable("GitHub CLI 返回了无效 JSON") from exc
    if not isinstance(payload, dict):
        raise GithubUnavailable("GitHub CLI 返回格式无效")
    return payload


def workflow_runs(root: Path, repository: str, commit: str) -> list[dict[str, Any]]:
    payload = gh_json(root, repository, f"actions/runs?head_sha={commit}&per_page=100")
    runs = payload.get("workflow_runs")
    if not isinstance(runs, list):
        raise GithubUnavailable("GitHub Actions 运行列表格式无效")
    return [item for item in runs if isinstance(item, dict)]


def wait_for_windows_run(
    root: Path,
    repository: str,
    tag: str,
    commit: str,
    timeout_seconds: int,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        for run in workflow_runs(root, repository, commit):
            if (
                run.get("name") == WINDOWS_WORKFLOW_NAME
                and run.get("event") == "push"
                and (
                    run.get("head_branch") == tag
                    or run.get("display_title") == f"Windows release · {tag}"
                )
            ):
                return run
        time.sleep(5)
    raise GithubUnavailable(
        f"等待 Windows GitHub Actions 启动超时，请检查标签 {tag}"
    )


def cleanup_github_request(
    root: Path,
    repository: str,
    github_remote: str,
    tag: str,
) -> None:
    failures: list[str] = []
    for arguments in (
        ["gh", "release", "delete", tag, "--repo", repository, "--yes"],
        ["git", "push", github_remote, "--delete", tag],
        ["git", "tag", "--delete", tag],
    ):
        try:
            run_command(arguments, cwd=root, capture=True)
        except ReleaseTaskError as exc:
            failures.append(str(exc))
    if failures:
        print(
            "警告：构建已成功，但临时 GitHub 请求清理不完整：\n"
            + "\n".join(failures),
            file=sys.stderr,
        )


def github_windows_build(
    root: Path,
    *,
    version: str,
    base_url: str,
    channel: str,
    ca_bundle: str,
    delta_from_version: str,
    build_portable: bool,
    github_remote: str,
    github_repo: str,
    workflow_timeout: int,
    result_output: Path | None = None,
) -> Path:
    request_archive = create_windows_request(
        root,
        version=version,
        base_url=base_url,
        channel=channel,
        ca_bundle=ca_bundle,
        delta_from_version=delta_from_version,
        build_portable=build_portable,
        github_remote=github_remote,
    )
    print(
        "提示：如果 GitHub 不可用，可把上面的任务包带到 Windows 真机。",
        flush=True,
    )
    if shutil.which("gh") is None:
        raise GithubUnavailable("未找到 GitHub CLI（gh）")
    try:
        run_command(
            ["gh", "auth", "status", "--hostname", "github.com"],
            cwd=root,
            capture=True,
        )
    except ReleaseTaskError as exc:
        raise GithubUnavailable(f"GitHub CLI 尚未登录或无法连接：{exc}") from exc
    branch, commit = source_state(root)
    remote_url = git(root, "remote", "get-url", github_remote)
    repository = str(github_repo or "").strip() or infer_github_repository(remote_url)
    if not GITHUB_REPOSITORY_PATTERN.fullmatch(repository):
        raise ReleaseTaskError(f"GitHub 仓库格式必须为 owner/repo：{repository}")
    try:
        remote_line = capture_command(
            ["git", "ls-remote", github_remote, f"refs/heads/{branch}"],
            cwd=root,
        )
    except ReleaseTaskError as exc:
        raise GithubUnavailable(f"无法检查 GitHub 源分支：{exc}") from exc
    remote_commit = remote_line.split()[0].casefold() if remote_line else ""
    if remote_commit != commit:
        raise ReleaseTaskError(
            f"当前提交尚未推送到 {github_remote}/{branch}；请先执行双仓库推送"
        )

    request_id = request_archive.parent.name
    tag = f"{WINDOWS_TAG_PREFIX}{version}-{request_id}"
    message = json.dumps(
        {
            "request_id": request_id,
            "version": version,
            "source_commit": commit,
        },
        ensure_ascii=False,
    )
    try:
        run_command(
            ["git", "tag", "--annotate", tag, commit, "--message", message],
            cwd=root,
        )
        run_command(
            ["git", "push", github_remote, f"refs/tags/{tag}:refs/tags/{tag}"],
            cwd=root,
            capture=True,
        )
    except ReleaseTaskError as exc:
        subprocess.run(
            ["git", "tag", "--delete", tag],
            cwd=str(root),
            check=False,
            capture_output=True,
        )
        raise GithubUnavailable(f"无法推送 GitHub Windows 构建标签：{exc}") from exc
    try:
        run_command(
            [
                "gh",
                "release",
                "create",
                tag,
                str(request_archive),
                "--repo",
                repository,
                "--verify-tag",
                "--prerelease",
                "--title",
                f"Temporary Windows build {request_id}",
                "--notes",
                "Temporary build request. It contains no deployment credentials.",
            ],
            cwd=root,
            capture=True,
        )
    except ReleaseTaskError as exc:
        raise GithubUnavailable(
            f"无法上传 GitHub Windows 构建任务包；已保留标签 {tag}：{exc}"
        ) from exc

    run = wait_for_windows_run(
        root,
        repository,
        tag,
        commit,
        workflow_timeout,
    )
    run_id = str(run.get("id") or "")
    run_url = str(run.get("html_url") or run_id)
    print(f"Windows GitHub 构建：{run_url}", flush=True)
    try:
        run_command(
            ["gh", "run", "watch", run_id, "--repo", repository, "--exit-status"],
            cwd=root,
        )
    except ReleaseTaskError as watch_error:
        try:
            refreshed = gh_json(root, repository, f"actions/runs/{run_id}")
        except GithubUnavailable as exc:
            raise GithubUnavailable(
                f"GitHub 构建状态连接中断；请求已保留：{tag}"
            ) from exc
        if refreshed.get("status") == "completed" and refreshed.get("conclusion") != "success":
            raise ReleaseTaskError(
                f"Windows GitHub 构建失败，请查看 {run_url}"
            ) from watch_error
        raise GithubUnavailable(
            f"无法继续读取 GitHub 构建状态；请求已保留：{tag}"
        ) from watch_error

    with tempfile.TemporaryDirectory(prefix="intdemo-gh-download-") as temporary:
        download_root = Path(temporary)
        try:
            run_command(
                [
                    "gh",
                    "run",
                    "download",
                    run_id,
                    "--repo",
                    repository,
                    "--name",
                    f"intdemo-windows-result-{run_id}",
                    "--dir",
                    str(download_root),
                ],
                cwd=root,
                capture=True,
            )
        except ReleaseTaskError as exc:
            raise GithubUnavailable(
                f"Windows 构建已成功，但结果下载失败；运行已保留：{run_url}"
            ) from exc
        archives = list(download_root.glob("windows-build-result*.zip"))
        if len(archives) != 1:
            raise ReleaseTaskError("GitHub Windows 构建结果包数量异常")
        receipt = import_windows_result(
            root,
            result_archive=archives[0],
            version=version,
            base_url=base_url,
            channel=channel,
            ca_bundle=ca_bundle,
            delta_from_version=delta_from_version,
            build_portable=build_portable,
        )
        if result_output is not None:
            copy_atomic(archives[0], result_output.expanduser().resolve())
            print(
                f"Windows 构建结果包已保存：{result_output.expanduser().resolve()}",
                flush=True,
            )
    cleanup_github_request(root, repository, github_remote, tag)
    return receipt


def add_common_build_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--version", required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--channel", choices=("test", "stable"), default="test")
    parser.add_argument("--ca-bundle", default="")
    parser.add_argument("--delta-from-version", default="")
    parser.add_argument("--build-portable", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="IntDemo 双端构建结果、校验和发布工具"
    )
    commands = parser.add_subparsers(dest="command", required=True)

    export = commands.add_parser("export-windows-request")
    add_common_build_arguments(export)
    export.add_argument("--github-remote", default="origin")

    github = commands.add_parser("windows-github")
    add_common_build_arguments(github)
    github.add_argument("--github-remote", default="origin")
    github.add_argument("--github-repo", default="")
    github.add_argument("--workflow-timeout", type=int, default=300)
    github.add_argument("--fallback-on-unavailable", action="store_true")
    github.add_argument(
        "--result-output",
        default="",
        help="保存 GitHub 返回的 Windows 标准结果 ZIP；留空则仅导入安装包",
    )

    windows_build = commands.add_parser("windows-build")
    windows_build.add_argument("--request-archive", required=True)
    windows_build.add_argument("--output", required=True)
    windows_build.add_argument("--inno-compiler", default="")
    windows_build.add_argument("--run-tests", action="store_true")
    windows_build.add_argument("--github-run-id", default="")
    windows_build.add_argument("--github-run-url", default="")

    windows_local = commands.add_parser("windows-local")
    add_common_build_arguments(windows_local)
    windows_local.add_argument("--github-remote", default="origin")
    windows_local.add_argument("--inno-compiler", default="")
    windows_local.add_argument("--tests-prevalidated", action="store_true")

    import_result = commands.add_parser("import-windows-result")
    add_common_build_arguments(import_result)
    import_result.add_argument("--result-archive", required=True)

    export_windows_base = commands.add_parser("export-windows-delta-base")
    export_windows_base.add_argument("--version", required=True)
    export_windows_base.add_argument("--output", default="")

    import_windows_base = commands.add_parser("import-windows-delta-base")
    import_windows_base.add_argument("--base-archive", required=True)

    record_uos = commands.add_parser("record-uos-result")
    record_uos.add_argument("--version", required=True)
    record_uos.add_argument("--base-url", required=True)
    record_uos.add_argument("--channel", choices=("test", "stable"), default="test")
    record_uos.add_argument("--ca-bundle", default="")
    record_uos.add_argument("--uos-delta-from-version", default="")

    export_uos = commands.add_parser("export-uos-result")
    export_uos.add_argument("--version", required=True)
    export_uos.add_argument("--base-url", required=True)
    export_uos.add_argument("--channel", choices=("test", "stable"), default="test")
    export_uos.add_argument("--ca-bundle", default="")
    export_uos.add_argument("--uos-delta-from-version", default="")
    export_uos.add_argument("--output", default="")
    export_uos.add_argument("--allow-detached", action="store_true")

    import_uos = commands.add_parser("import-uos-result")
    import_uos.add_argument("--version", required=True)
    import_uos.add_argument("--base-url", required=True)
    import_uos.add_argument("--channel", choices=("test", "stable"), default="test")
    import_uos.add_argument("--ca-bundle", default="")
    import_uos.add_argument("--uos-delta-from-version", default="")
    import_uos.add_argument("--result-archive", required=True)

    export_uos_base = commands.add_parser("export-uos-delta-base")
    export_uos_base.add_argument("--version", required=True)
    export_uos_base.add_argument("--output", default="")

    import_uos_base = commands.add_parser("import-uos-delta-base")
    import_uos_base.add_argument("--base-archive", required=True)

    publish = commands.add_parser("publish")
    add_common_build_arguments(publish)
    publish.add_argument("--notes", required=True)
    publish.add_argument("--mandatory", action="store_true")
    publish.add_argument("--remote-host", required=True)
    publish.add_argument("--remote-path", required=True)
    publish.add_argument("--identity-file", default="")
    publish.add_argument("--confirm-version", required=True)
    publish.add_argument("--uos-delta-from-version", default="")
    publish.add_argument(
        "--eligible-client-version",
        action="append",
        default=[],
    )

    pause = commands.add_parser("pause")
    pause.add_argument("--channel", choices=("test", "stable"), required=True)
    pause.add_argument("--remote-host", required=True)
    pause.add_argument("--remote-path", required=True)
    pause.add_argument("--identity-file", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    root = repo_root()
    try:
        if args.command == "export-windows-request":
            create_windows_request(
                root,
                version=args.version,
                base_url=args.base_url,
                channel=args.channel,
                ca_bundle=args.ca_bundle,
                delta_from_version=args.delta_from_version,
                build_portable=args.build_portable,
                github_remote=args.github_remote,
            )
        elif args.command == "windows-github":
            github_windows_build(
                root,
                version=args.version,
                base_url=args.base_url,
                channel=args.channel,
                ca_bundle=args.ca_bundle,
                delta_from_version=args.delta_from_version,
                build_portable=args.build_portable,
                github_remote=args.github_remote,
                github_repo=args.github_repo,
                workflow_timeout=args.workflow_timeout,
                result_output=Path(args.result_output) if args.result_output else None,
            )
        elif args.command == "windows-build":
            build_windows_result(
                root,
                request_archive=Path(args.request_archive),
                output=Path(args.output),
                inno_compiler=args.inno_compiler,
                run_tests=args.run_tests,
                github_run_id=args.github_run_id,
                github_run_url=args.github_run_url,
            )
        elif args.command == "windows-local":
            build_local_windows_result(
                root,
                version=args.version,
                base_url=args.base_url,
                channel=args.channel,
                ca_bundle=args.ca_bundle,
                delta_from_version=args.delta_from_version,
                build_portable=args.build_portable,
                github_remote=args.github_remote,
                inno_compiler=args.inno_compiler,
                tests_prevalidated=args.tests_prevalidated,
            )
        elif args.command == "import-windows-result":
            import_windows_result(
                root,
                result_archive=Path(args.result_archive),
                version=args.version,
                base_url=args.base_url,
                channel=args.channel,
                ca_bundle=args.ca_bundle,
                delta_from_version=args.delta_from_version,
                build_portable=args.build_portable,
            )
        elif args.command == "export-windows-delta-base":
            export_windows_delta_base(
                root,
                version=args.version,
                output=Path(args.output) if args.output else None,
            )
        elif args.command == "import-windows-delta-base":
            import_windows_delta_base(
                root,
                base_archive=Path(args.base_archive),
            )
        elif args.command == "record-uos-result":
            record_uos_result(
                root,
                version=args.version,
                base_url=args.base_url,
                channel=args.channel,
                ca_bundle=args.ca_bundle,
                uos_delta_from_version=args.uos_delta_from_version,
            )
        elif args.command == "export-uos-result":
            export_uos_result(
                root,
                version=args.version,
                base_url=args.base_url,
                channel=args.channel,
                ca_bundle=args.ca_bundle,
                uos_delta_from_version=args.uos_delta_from_version,
                output=Path(args.output) if args.output else None,
                allow_detached=args.allow_detached,
            )
        elif args.command == "import-uos-result":
            import_uos_result(
                root,
                result_archive=Path(args.result_archive),
                version=args.version,
                base_url=args.base_url,
                channel=args.channel,
                ca_bundle=args.ca_bundle,
                uos_delta_from_version=args.uos_delta_from_version,
            )
        elif args.command == "export-uos-delta-base":
            export_uos_delta_base(
                root,
                version=args.version,
                output=Path(args.output) if args.output else None,
            )
        elif args.command == "import-uos-delta-base":
            import_uos_delta_base(
                root,
                base_archive=Path(args.base_archive),
            )
        elif args.command == "publish":
            publish_release(
                root,
                version=args.version,
                base_url=args.base_url,
                channel=args.channel,
                ca_bundle=args.ca_bundle,
                delta_from_version=args.delta_from_version,
                build_portable=args.build_portable,
                uos_delta_from_version=args.uos_delta_from_version,
                eligible_client_versions=args.eligible_client_version,
                notes=args.notes,
                mandatory=args.mandatory,
                remote_host=args.remote_host,
                remote_path=args.remote_path,
                identity_file_value=args.identity_file,
                confirm_version=args.confirm_version,
            )
        elif args.command == "pause":
            pause_distribution(
                root,
                channel=args.channel,
                remote_host=args.remote_host,
                remote_path=args.remote_path,
                identity_file_value=args.identity_file,
            )
        else:
            parser.error(f"未知命令：{args.command}")
    except GithubUnavailable as exc:
        print(f"GitHub 暂不可用：{exc}", file=sys.stderr)
        if getattr(args, "fallback_on_unavailable", False):
            print(
                "已保留 Windows 构建任务包，请转到 Windows 真机完成构建后导入。",
                file=sys.stderr,
            )
            return GITHUB_UNAVAILABLE_EXIT
        return 2
    except ReleaseTaskError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

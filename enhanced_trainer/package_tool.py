"""Native-only PyInstaller build and Ed25519-signed .inttrainer packager."""

from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .protocol import COMPONENT_VERSION

WINDOWS_PLATFORM = "windows-x86_64"
LINUX_ARM64_PLATFORM = "linux-aarch64"
SUPPORTED_PLATFORMS = {WINDOWS_PLATFORM, LINUX_ARM64_PLATFORM}
SIGNING_KEY_ENVIRONMENT = "INTDEMO_TRAINER_SIGNING_PRIVATE_KEY"
MAX_SOURCE_FILES = 20_000
MAX_SOURCE_BYTES = 2 * 1024 * 1024 * 1024
_KEY_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_VERSION_PATTERN = re.compile(r"^\d+(?:\.\d+){2,3}$")


class PackageToolError(RuntimeError):
    pass


def native_platform_key() -> str:
    system = sys.platform.casefold()
    machine = platform.machine().casefold()
    if system.startswith("win") and machine in {"amd64", "x86_64"}:
        return WINDOWS_PLATFORM
    if system.startswith("linux") and machine in {"aarch64", "arm64"}:
        return LINUX_ARM64_PLATFORM
    return ""


def require_native_platform(requested: str | None = None) -> str:
    actual = native_platform_key()
    if actual not in SUPPORTED_PLATFORMS:
        raise PackageToolError(
            "强化组件只能在 Windows x64 或 Linux ARM64 本机上构建"
        )
    expected = str(requested or actual).strip().casefold()
    if expected != actual:
        raise PackageToolError(
            f"禁止交叉打包：当前为 {actual}，请求目标为 {expected or '-'}"
        )
    return actual


def _canonical_manifest(manifest: dict) -> bytes:
    unsigned = dict(manifest)
    unsigned.pop("signature", None)
    return json.dumps(
        unsigned,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def load_signing_key(
    *,
    private_key_file: Path | str | None = None,
    environ: dict[str, str] | None = None,
) -> Ed25519PrivateKey:
    """Read a raw 32-byte key from one explicit file or the dedicated env."""

    environment = os.environ if environ is None else environ
    encoded_environment = str(
        environment.get(SIGNING_KEY_ENVIRONMENT) or ""
    ).strip()
    if private_key_file and encoded_environment:
        raise PackageToolError("签名私钥文件与环境变量不能同时使用")
    if private_key_file:
        path = Path(private_key_file).expanduser().resolve()
        try:
            if path.is_symlink() or not path.is_file():
                raise ValueError("not a regular file")
            if path.stat().st_size > 4096:
                raise ValueError("file too large")
            repository = Path(__file__).resolve().parents[1]
            try:
                path.relative_to(repository)
            except ValueError:
                pass
            else:
                raise ValueError("private key inside repository")
            if os.name != "nt" and path.stat().st_mode & 0o077:
                raise ValueError("private key permissions are too broad")
            encoded = path.read_text(encoding="ascii").strip()
        except (OSError, UnicodeError, ValueError) as exc:
            raise PackageToolError(f"无法读取强化组件签名私钥文件：{exc}") from exc
    else:
        encoded = encoded_environment
    if not encoded:
        raise PackageToolError(
            "必须通过 --private-key-file 或 "
            f"{SIGNING_KEY_ENVIRONMENT} 显式提供签名私钥"
        )
    try:
        raw = base64.b64decode(encoded, validate=True)
        if len(raw) != 32:
            raise ValueError("private key length")
        return Ed25519PrivateKey.from_private_bytes(raw)
    except (TypeError, ValueError) as exc:
        raise PackageToolError("签名私钥必须是 Base64 编码的 32 字节 Ed25519 私钥") from exc


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _bundle_files(bundle_dir: Path) -> list[tuple[str, Path, int, str]]:
    files = []
    total = 0
    for source in sorted(bundle_dir.rglob("*"), key=lambda item: item.as_posix()):
        try:
            relative = source.relative_to(bundle_dir)
            if source.is_symlink():
                resolved_source = source.resolve(strict=True)
                try:
                    resolved_source.relative_to(bundle_dir)
                except ValueError as exc:
                    raise PackageToolError(
                        f"强化训练器符号链接指向目录外：{relative}"
                    ) from exc
                if not resolved_source.is_file() or resolved_source.is_symlink():
                    raise PackageToolError(
                        f"强化训练器符号链接未指向普通文件：{relative}"
                    )
            else:
                if source.is_dir():
                    continue
                resolved_source = source
            mode = resolved_source.stat().st_mode
            if not stat.S_ISREG(mode):
                raise PackageToolError(f"强化训练器目录包含特殊文件：{source}")
            if any(
                part in {"", ".", ".."}
                or ":" in part
                or part.endswith((" ", "."))
                or any(ord(character) < 32 for character in part)
                for part in relative.parts
            ):
                raise PackageToolError(
                    f"强化训练器目录包含不安全文件名：{relative}"
                )
            package_path = str(PurePosixPath("bin", *relative.parts))
            size = resolved_source.stat().st_size
        except OSError as exc:
            raise PackageToolError(f"无法读取强化训练器文件：{source}") from exc
        total += size
        if total > MAX_SOURCE_BYTES:
            raise PackageToolError("强化训练器文件总大小超过 2 GiB")
        # PyInstaller 6 uses file symlinks in POSIX onedir bundles.  The outer
        # package stays platform-neutral by materializing their target bytes as
        # ordinary signed ZIP members instead of preserving symlink metadata.
        files.append(
            (package_path, resolved_source, size, _sha256(resolved_source))
        )
        if len(files) > MAX_SOURCE_FILES:
            raise PackageToolError("强化训练器文件数量超过限制")
    if not files:
        raise PackageToolError("强化训练器构建目录为空")
    return files


def package_bundle(
    bundle_dir: Path | str,
    output_path: Path | str,
    *,
    key_id: str,
    private_key: Ed25519PrivateKey,
    component_version: str = COMPONENT_VERSION,
    requested_platform: str | None = None,
) -> Path:
    platform_key = require_native_platform(requested_platform)
    if not _KEY_ID_PATTERN.fullmatch(str(key_id or "")):
        raise PackageToolError("强化组件签名密钥编号格式无效")
    if not _VERSION_PATTERN.fullmatch(str(component_version or "")):
        raise PackageToolError("强化组件版本号格式无效")
    if component_version != COMPONENT_VERSION:
        raise PackageToolError(
            "强化组件清单版本必须与训练器代码中的 COMPONENT_VERSION 一致"
        )
    root = Path(bundle_dir).expanduser().resolve()
    if not root.is_dir() or root.is_symlink():
        raise PackageToolError("强化训练器构建目录不存在或类型无效")
    output = Path(output_path).expanduser().resolve()
    if output.suffix.casefold() != ".inttrainer":
        raise PackageToolError("强化组件输出文件必须使用 .inttrainer 扩展名")
    try:
        output.relative_to(root)
    except ValueError:
        pass
    else:
        raise PackageToolError("强化组件输出文件不能位于待打包目录内")
    if output.exists():
        raise PackageToolError("强化组件输出文件已存在，已拒绝覆盖")

    expected_executable = (
        "intdemo-trainer.exe"
        if platform_key == WINDOWS_PLATFORM
        else "intdemo-trainer"
    )
    executable = root / expected_executable
    if not executable.is_file() or executable.is_symlink():
        raise PackageToolError(
            f"PyInstaller 构建目录缺少入口程序：{expected_executable}"
        )
    files = _bundle_files(root)
    manifest = {
        "schema_version": 1,
        "component_id": "intdemo-enhanced-trainer",
        "version": component_version,
        "platform": platform_key,
        "protocol_version": 1,
        "min_client_version": "1.1.0",
        "entrypoint": f"bin/{expected_executable}",
        "capabilities": {
            "numeric": "tiny-cnn-four-head-v1",
            "click": "tiny-cnn-character-v1",
        },
        "files": [
            {"path": name, "size": size, "sha256": digest}
            for name, _source, size, digest in files
        ],
    }
    signature = private_key.sign(_canonical_manifest(manifest))
    manifest["signature"] = {
        "algorithm": "ed25519",
        "key_id": key_id,
        "value": base64.b64encode(signature).decode("ascii"),
    }
    manifest_bytes = json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")

    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.",
        suffix=".tmp",
        dir=str(output.parent),
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with zipfile.ZipFile(
            temporary,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
            allowZip64=True,
        ) as archive:
            manifest_info = zipfile.ZipInfo("manifest.json", (1980, 1, 1, 0, 0, 0))
            manifest_info.compress_type = zipfile.ZIP_DEFLATED
            manifest_info.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(manifest_info, manifest_bytes)
            for name, source, _size, _digest in files:
                info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                executable_file = name == f"bin/{expected_executable}"
                info.external_attr = (
                    stat.S_IFREG | (0o755 if executable_file else 0o644)
                ) << 16
                with source.open("rb") as stream, archive.open(info, "w") as target:
                    while True:
                        block = stream.read(1024 * 1024)
                        if not block:
                            break
                        target.write(block)
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    return output


def _require_build_dependencies() -> None:
    missing = [
        name
        for name in ("torch", "onnx", "onnxruntime", "PIL", "PyInstaller")
        if importlib.util.find_spec(name) is None
    ]
    if missing:
        raise PackageToolError(
            "本机强化训练构建环境缺少："
            + "、".join(missing)
            + "；构建工具不会联网安装依赖"
        )
    import torch

    if torch.version.cuda is not None:
        raise PackageToolError("请使用 PyTorch CPU 构建环境，禁止打包 CUDA 运行库")


def _child_environment() -> dict[str, str]:
    """Never expose the component-signing secret to build/test children."""

    environment = {str(key): str(value) for key, value in os.environ.items()}
    environment.pop(SIGNING_KEY_ENVIRONMENT, None)
    return environment


def self_test_bundle(
    bundle_dir: Path | str,
    *,
    requested_platform: str | None = None,
    runner=subprocess.run,
) -> dict:
    """Run the frozen native executable before it may be signed or packaged."""

    platform_key = require_native_platform(requested_platform)
    root = Path(bundle_dir).expanduser().resolve()
    executable = root / (
        "intdemo-trainer.exe"
        if platform_key == WINDOWS_PLATFORM
        else "intdemo-trainer"
    )
    if not executable.is_file() or executable.is_symlink():
        raise PackageToolError("强化训练器入口不存在或类型无效")
    if platform_key == LINUX_ARM64_PLATFORM:
        executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    command = [str(executable), "self-test", "--protocol-version", "1"]
    try:
        result = runner(
            command,
            cwd=str(root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=120,
            check=False,
            shell=False,
            env=_child_environment(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PackageToolError(f"无法启动强化训练器自检：{exc}") from exc
    if result.returncode != 0:
        detail = " ".join(str(result.stderr or "").split())[:500]
        raise PackageToolError(
            "强化训练器自检失败" + (f"：{detail}" if detail else "")
        )
    try:
        payload = json.loads(result.stdout)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise PackageToolError("强化训练器自检返回了无效 JSON") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("ok") is not True
        or payload.get("protocol_version") != 1
        or payload.get("component_version") != COMPONENT_VERSION
    ):
        raise PackageToolError("强化训练器自检结果与组件协议不一致")
    return payload


def build_native_bundle(
    output_root: Path | str,
    *,
    requested_platform: str | None = None,
    runner=subprocess.run,
) -> Path:
    """Run PyInstaller on the current native OS; never installs dependencies."""

    platform_key = require_native_platform(requested_platform)
    _require_build_dependencies()
    repository = Path(__file__).resolve().parents[1]
    entrypoint = repository / "enhanced_trainer" / "entrypoint.py"
    root = Path(output_root).expanduser().resolve()
    if root in {Path(root.anchor), Path.home().resolve(), repository}:
        raise PackageToolError("强化训练器构建目录不能是磁盘根、用户目录或仓库根目录")
    dist = root / "dist"
    build = root / "build"
    specs = root / "spec"
    for directory in (dist, build, specs):
        if directory.exists():
            if directory.is_symlink() or not directory.is_dir():
                raise PackageToolError(f"强化训练器构建路径类型无效：{directory}")
            shutil.rmtree(directory)
    for directory in (dist, build, specs):
        directory.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onedir",
        "--name",
        "intdemo-trainer",
        "--distpath",
        str(dist),
        "--workpath",
        str(build),
        "--specpath",
        str(specs),
        "--paths",
        str(repository),
        "--hidden-import",
        "torch",
        "--hidden-import",
        "onnx",
        "--hidden-import",
        "onnxruntime",
        str(entrypoint),
    ]
    try:
        result = runner(
            command,
            cwd=str(repository),
            env=_child_environment(),
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PackageToolError(f"无法启动 PyInstaller：{exc}") from exc
    if result.returncode != 0:
        raise PackageToolError(f"PyInstaller 构建失败，退出码 {result.returncode}")
    bundle = dist / "intdemo-trainer"
    expected = bundle / (
        "intdemo-trainer.exe"
        if platform_key == WINDOWS_PLATFORM
        else "intdemo-trainer"
    )
    if not expected.is_file():
        raise PackageToolError("PyInstaller 未生成预期的强化训练器入口")
    self_test_bundle(
        bundle,
        requested_platform=platform_key,
        runner=runner,
    )
    return bundle


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="intdemo-trainer-packager")
    parser.add_argument("--platform", choices=sorted(SUPPORTED_PLATFORMS))
    parser.add_argument("--key-id", required=True)
    parser.add_argument("--private-key-file")
    parser.add_argument("--component-version", default=COMPONENT_VERSION)
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser("build")
    build.add_argument("--build-root", required=True)

    package = subparsers.add_parser("package")
    package.add_argument("--bundle-dir", required=True)
    package.add_argument("--output", required=True)

    complete = subparsers.add_parser("all")
    complete.add_argument("--build-root", required=True)
    complete.add_argument("--output", required=True)
    return parser


def main(arguments=None) -> int:
    options = build_parser().parse_args(arguments)
    try:
        require_native_platform(options.platform)
        if options.command == "build":
            bundle = build_native_bundle(
                options.build_root,
                requested_platform=options.platform,
            )
            print(bundle)
            return 0
        private_key = load_signing_key(private_key_file=options.private_key_file)
        if options.command == "package":
            bundle = options.bundle_dir
            self_test_bundle(bundle, requested_platform=options.platform)
        else:
            bundle = build_native_bundle(
                options.build_root,
                requested_platform=options.platform,
            )
        output = package_bundle(
            bundle,
            options.output,
            key_id=options.key_id,
            private_key=private_key,
            component_version=options.component_version,
            requested_platform=options.platform,
        )
        print(output)
        return 0
    except PackageToolError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

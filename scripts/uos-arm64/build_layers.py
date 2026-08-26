#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from integrated_client.online.uos_layers import (
    UOS_LAYER_FORMAT,
    UOS_LAYER_LAYOUT,
    UosLayerError,
    UosLayerStore,
    create_layer_archive,
    read_layer_manifest,
    read_layout,
    validate_bootstrap,
    validate_package_root,
)

VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")


class BuildLayerError(RuntimeError):
    pass


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def load_json(path: Path, label: str) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BuildLayerError(f"无法读取{label}：{path}") from exc
    if not isinstance(value, dict):
        raise BuildLayerError(f"{label}根节点必须是对象")
    return value


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def validate_source_receipt(source_deb: Path, receipt_path: Path, version: str) -> None:
    receipt = load_json(receipt_path, "来源发布收据")
    if receipt.get("schema_version") != 1 or receipt.get("version") != version:
        raise BuildLayerError("来源发布收据版本与 UOS 基线不一致")
    artifacts = receipt.get("artifacts")
    descriptor = artifacts.get("uos_installer") if isinstance(artifacts, dict) else None
    if not isinstance(descriptor, dict):
        raise BuildLayerError("来源发布收据缺少 UOS 完整 DEB")
    expected = {
        "name": source_deb.name,
        "size": source_deb.stat().st_size,
        "sha256": sha256(source_deb),
    }
    if any(descriptor.get(key) != value for key, value in expected.items()):
        raise BuildLayerError("来源发布收据与 UOS 基线 DEB 不一致")


def fallback_report(
    *,
    from_version: str,
    target_version: str,
    source_deb: Path,
    target_deb: Path,
    archive: Path,
    reason: str,
    source_layout_sha256: str = "",
    target_layout_sha256: str = "",
    source_bootstrap_sha256: str = "",
    target_bootstrap_sha256: str = "",
    changed_layers: list[str] | None = None,
    archive_size: int = 0,
    archive_sha256: str = "",
    replay_verified: bool = False,
) -> dict:
    return {
        "schema_version": 1,
        "format": UOS_LAYER_FORMAT,
        "platform": "linux-aarch64",
        "from_version": from_version,
        "target_version": target_version,
        "source_name": source_deb.name,
        "source_size": source_deb.stat().st_size,
        "source_sha256": sha256(source_deb),
        "target_name": target_deb.name,
        "target_size": target_deb.stat().st_size,
        "target_sha256": sha256(target_deb),
        "archive_name": archive.name,
        "archive_size": int(archive_size),
        "archive_sha256": archive_sha256,
        "source_layout_sha256": source_layout_sha256,
        "target_layout_sha256": target_layout_sha256,
        "source_bootstrap_sha256": source_bootstrap_sha256,
        "target_bootstrap_sha256": target_bootstrap_sha256,
        "changed_layers": list(changed_layers or []),
        "replay_verified": bool(replay_verified),
        "eligible": False,
        "fallback_to_full": True,
        "reason": reason,
        "validated_at": utc_now(),
    }


def build(args: argparse.Namespace) -> int:
    source_deb = Path(args.source_deb).expanduser().resolve()
    target_deb = Path(args.target_deb).expanduser().resolve()
    target_root = Path(args.target_root).expanduser().resolve()
    source_receipt = Path(args.source_receipt).expanduser().resolve()
    output = Path(args.output).expanduser().resolve()
    report_path = Path(args.report).expanduser().resolve()
    for version, label in (
        (args.from_version, "来源"),
        (args.target_version, "目标"),
    ):
        if not VERSION_PATTERN.fullmatch(version):
            raise BuildLayerError(f"{label}版本必须使用 x.y.z 格式")
    if not 1 <= args.threshold_percent <= 100:
        raise BuildLayerError("体积门槛必须是 1～100 的整数")
    for path, label in (
        (source_deb, "来源 DEB"),
        (target_deb, "目标 DEB"),
        (source_receipt, "来源发布收据"),
    ):
        if not path.is_file():
            raise BuildLayerError(f"{label}不存在：{path}")
    if not target_root.is_dir():
        raise BuildLayerError(f"目标便携包目录不存在：{target_root}")
    validate_source_receipt(source_deb, source_receipt, args.from_version)
    target_layout = read_layout(target_root, version=args.target_version)
    validate_package_root(target_root, target_layout)
    validate_bootstrap(target_root, target_layout)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.unlink(missing_ok=True)
    report_path.unlink(missing_ok=True)

    with tempfile.TemporaryDirectory(prefix="intdemo-uos-layer-source-") as temporary:
        extracted = Path(temporary)
        try:
            subprocess.run(
                ["dpkg-deb", "--extract", str(source_deb), str(extracted)],
                check=True,
                stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise BuildLayerError("无法解开来源 UOS DEB") from exc
        source_root = extracted / "opt/apps/com.e23aqiu.intdemo/files"
        if not (source_root / UOS_LAYER_LAYOUT).is_file():
            report = fallback_report(
                from_version=args.from_version,
                target_version=args.target_version,
                source_deb=source_deb,
                target_deb=target_deb,
                archive=output,
                reason="source_layout_missing",
                target_layout_sha256=target_layout["layout_sha256"],
                target_bootstrap_sha256=target_layout["bootstrap_sha256"],
            )
            write_json(report_path, report)
            print("来源版本没有分层基线，本次自动仅发布完整 UOS DEB。")
            print(f"分层报告：{report_path}")
            return 0

        source_layout = read_layout(source_root, version=args.from_version)
        validate_package_root(source_root, source_layout)
        validate_bootstrap(source_root, source_layout)
        if (
            source_layout["bootstrap_sha256"]
            != target_layout["bootstrap_sha256"]
        ):
            report = fallback_report(
                from_version=args.from_version,
                target_version=args.target_version,
                source_deb=source_deb,
                target_deb=target_deb,
                archive=output,
                reason="bootstrap_changed",
                source_layout_sha256=source_layout["layout_sha256"],
                target_layout_sha256=target_layout["layout_sha256"],
                source_bootstrap_sha256=source_layout["bootstrap_sha256"],
                target_bootstrap_sha256=target_layout["bootstrap_sha256"],
            )
            write_json(report_path, report)
            print("启动器、在线配置或 CA 已变化，本次自动仅发布完整 UOS DEB。")
            print(f"分层报告：{report_path}")
            return 0
        changed_layers = [
            name
            for name in ("app", "runtime", "browser")
            if source_layout["layers"][name] != target_layout["layers"][name]
        ]
        if not changed_layers:
            report = fallback_report(
                from_version=args.from_version,
                target_version=args.target_version,
                source_deb=source_deb,
                target_deb=target_deb,
                archive=output,
                reason="no_layer_changes",
                source_layout_sha256=source_layout["layout_sha256"],
                target_layout_sha256=target_layout["layout_sha256"],
                source_bootstrap_sha256=source_layout["bootstrap_sha256"],
                target_bootstrap_sha256=target_layout["bootstrap_sha256"],
            )
            write_json(report_path, report)
            print("三个更新层均无变化，本次自动仅发布完整 UOS DEB。")
            print(f"分层报告：{report_path}")
            return 0
        manifest = create_layer_archive(
            source_root,
            target_root,
            output,
            from_version=args.from_version,
            target_version=args.target_version,
        )
        loaded = read_layer_manifest(
            output,
            from_version=args.from_version,
            target_version=args.target_version,
            source_layout_sha256=source_layout["layout_sha256"],
            target_layout_sha256=target_layout["layout_sha256"],
        )
        if loaded != manifest:
            raise BuildLayerError("生成后的分层包清单发生变化")
        with tempfile.TemporaryDirectory(prefix="intdemo-uos-layer-replay-") as replay:
            store = UosLayerStore(
                replay,
                current_version=args.from_version,
                package_root=source_root,
                active_root=source_root,
            )
            store.stage(
                output,
                from_version=args.from_version,
                target_version=args.target_version,
                source_layout_sha256=source_layout["layout_sha256"],
                target_layout_sha256=target_layout["layout_sha256"],
            )
            validate_package_root(
                store.versions_dir / args.target_version,
                target_layout,
            )

    archive_size = output.stat().st_size
    archive_hash = sha256(output)
    eligible = archive_size * 100 < target_deb.stat().st_size * args.threshold_percent
    report = fallback_report(
        from_version=args.from_version,
        target_version=args.target_version,
        source_deb=source_deb,
        target_deb=target_deb,
        archive=output,
        reason="" if eligible else "size_threshold_exceeded",
        source_layout_sha256=source_layout["layout_sha256"],
        target_layout_sha256=target_layout["layout_sha256"],
        source_bootstrap_sha256=source_layout["bootstrap_sha256"],
        target_bootstrap_sha256=target_layout["bootstrap_sha256"],
        changed_layers=manifest["changed_layers"],
        archive_size=archive_size,
        archive_sha256=archive_hash,
        replay_verified=True,
    )
    report["eligible"] = eligible
    report["fallback_to_full"] = not eligible
    report["threshold_percent"] = args.threshold_percent
    report["ratio_percent"] = round(archive_size * 100 / target_deb.stat().st_size, 2)
    write_json(report_path, report)
    if not eligible:
        output.unlink(missing_ok=True)
        print(
            f"分层包占完整 DEB {report['ratio_percent']:.2f}%，未达到"
            f"小于 {args.threshold_percent}% 门槛，本次自动仅发布完整包。"
        )
    else:
        print(
            f"UOS 分层更新包：{output}（完整 DEB 的 "
            f"{report['ratio_percent']:.2f}%）"
        )
    print(f"分层报告：{report_path}")
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="构建并回放验证 UOS 三层更新包")
    result.add_argument("--source-deb", required=True)
    result.add_argument("--target-deb", required=True)
    result.add_argument("--target-root", required=True)
    result.add_argument("--from-version", required=True)
    result.add_argument("--target-version", required=True)
    result.add_argument("--source-receipt", required=True)
    result.add_argument("--output", required=True)
    result.add_argument("--report", required=True)
    result.add_argument("--threshold-percent", type=int, default=50)
    return result


def main(argv: list[str] | None = None) -> int:
    try:
        return build(parser().parse_args(argv))
    except (BuildLayerError, UosLayerError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile
from hashlib import sha256 as bytes_sha256
from pathlib import Path
from unittest.mock import patch

from release_publisher import release_tasks as tasks

REPO_ROOT = Path(__file__).resolve().parents[1]
COMMIT = "a" * 40
OTHER_COMMIT = "b" * 40


def snapshot_payload(version: str) -> dict:
    content = b"installed application"
    return {
        "schema_version": 1,
        "version": version,
        "generated_at": "2026-08-05T00:00:00Z",
        "files": [
            {
                "path": "IntDemo.exe",
                "size": len(content),
                "sha256": bytes_sha256(content).hexdigest(),
            }
        ],
    }


def write_snapshot(path: Path, version: str) -> None:
    tasks.write_json(path, snapshot_payload(version))


def create_result_archive(
    root: Path,
    *,
    version: str = "1.2.3",
    commit: str = COMMIT,
    base_url: str = "https://api.example.com",
    channel: str = "test",
    build_portable: bool = False,
    tamper_installer: bool = False,
) -> Path:
    staging = root / "result-source"
    artifacts_root = staging / "artifacts"
    artifacts_root.mkdir(parents=True)
    inputs = {"ca_bundle": None, "baseline_snapshot": None}
    request = tasks.validate_request(
        {
            "schema_version": 1,
            "request_id": "20260805000000-abcdef1234",
            "version": version,
            "source_commit": commit,
            "source_branch": "codex/release-test",
            "repository_url": "https://github.com/e23Aqiu/intdemo.git",
            "base_url": base_url,
            "channel": channel,
            "build_portable": build_portable,
            "delta_from_version": "",
            "created_at": "2026-08-05T00:00:00Z",
            "inputs": inputs,
        }
    )
    request_path = staging / tasks.REQUEST_FILE
    tasks.write_json(request_path, request)

    installer = artifacts_root / f"IntDemoOnline-Setup-{version}.exe"
    installer.write_bytes(b"windows installer")
    snapshot = artifacts_root / f"IntDemoOnline-Snapshot-{version}.json"
    write_snapshot(snapshot, version)
    artifacts = {
        "windows_installer": tasks.artifact_descriptor(
            installer,
            f"artifacts/{installer.name}",
        ),
        "windows_snapshot": tasks.artifact_descriptor(
            snapshot,
            f"artifacts/{snapshot.name}",
        ),
    }
    if build_portable:
        portable = artifacts_root / f"IntDemoOnline-Portable-{version}.zip"
        portable.write_bytes(b"windows portable package")
        artifacts["windows_portable"] = tasks.artifact_descriptor(
            portable,
            f"artifacts/{portable.name}",
        )
    result = {
        "schema_version": 1,
        "request_id": request["request_id"],
        "request_sha256": tasks.sha256(request_path),
        "version": version,
        "source_commit": commit,
        "base_url": base_url,
        "channel": channel,
        "delta_from_version": "",
        "build_portable": build_portable,
        "tests_passed": True,
        "built_at": "2026-08-05T00:01:00Z",
        "builder": {"kind": "windows-manual"},
        "artifacts": artifacts,
    }
    tasks.write_json(staging / tasks.RESULT_FILE, result)
    if tamper_installer:
        installer.write_bytes(b"changed after hashing")
    archive = root / "windows-build-result.zip"
    tasks.make_zip(staging, archive)
    return archive


def create_uos_result_archive(
    root: Path,
    *,
    version: str = "1.2.3",
    commit: str = COMMIT,
    base_url: str = "https://api.example.com",
    channel: str = "test",
    ca_hash: str = "",
    tamper_installer: bool = False,
    uos_delta_from_version: str = "",
) -> Path:
    staging = root / "uos-result-source"
    artifacts_root = staging / "artifacts"
    artifacts_root.mkdir(parents=True)
    installer = artifacts_root / f"IntDemo-UOS-arm64-{version}.deb"
    installer.write_bytes(b"uos arm64 installer")
    artifacts = {
        "uos_installer": tasks.artifact_descriptor(
            installer,
            f"artifacts/{installer.name}",
        )
    }
    if uos_delta_from_version:
        patch_name = (
            f"IntDemo-UOS-arm64-Patch-{uos_delta_from_version}"
            f"-to-{version}.intdelta"
        )
        delta = artifacts_root / patch_name
        report = artifacts_root / f"{patch_name}.json"
        delta.write_bytes(b"uos delta")
        report.write_text("{}", encoding="utf-8")
        artifacts["uos_delta"] = tasks.artifact_descriptor(
            delta,
            f"artifacts/{delta.name}",
        )
        artifacts["uos_delta_report"] = tasks.artifact_descriptor(
            report,
            f"artifacts/{report.name}",
        )
    result = tasks.validate_uos_result_payload(
        {
            "schema_version": 1,
            "platform": "linux-aarch64",
            "version": version,
            "source_commit": commit,
            "base_url": base_url,
            "channel": channel,
            "ca_sha256": ca_hash,
            "payload_validated": True,
            "uos_delta_from_version": uos_delta_from_version,
            "built_at": "2026-08-05T00:01:00Z",
            "builder": {"kind": "uos-native", "machine": "aarch64"},
            "artifacts": artifacts,
        }
    )
    tasks.write_json(staging / tasks.UOS_RESULT_FILE, result)
    if tamper_installer:
        installer.write_bytes(b"changed after hashing")
    archive = root / "uos-build-result.zip"
    tasks.make_zip(staging, archive)
    return archive


class ReleaseTasksTests(unittest.TestCase):
    def test_targeted_manifest_lists_only_explicit_source_versions(self):
        windows = {"name": "client.exe", "sha256": "1" * 64, "size": 200}
        uos = {"name": "client.deb", "sha256": "2" * 64, "size": 300}

        manifest = tasks.prepare_update_manifest(
            version="1.2.1",
            channel="test",
            source_commit=COMMIT,
            notes="targeted test",
            mandatory=False,
            windows=windows,
            uos=uos,
            delta=None,
            delta_from_version="",
            eligible_client_versions=("1.1.0", "1.1.1"),
        )

        self.assertEqual(
            manifest["eligible_client_versions"],
            ["1.1.0", "1.1.1"],
        )
        with self.assertRaisesRegex(tasks.ReleaseTaskError, "低于目标版本"):
            tasks.prepare_update_manifest(
                version="1.2.1",
                channel="test",
                source_commit=COMMIT,
                notes="invalid target",
                mandatory=False,
                windows=windows,
                uos=uos,
                delta=None,
                delta_from_version="",
                eligible_client_versions=("1.2.1",),
            )

    def test_targeted_publish_requires_server_capability(self):
        class CapabilityResponse:
            status = 200

            def __init__(self, capabilities):
                self.capabilities = capabilities

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps(
                    {
                        "schema_version": 1,
                        "capabilities": self.capabilities,
                    }
                ).encode("utf-8")

        with (
            patch.object(
                tasks.urllib.request,
                "urlopen",
                return_value=CapabilityResponse([]),
            ),
            self.assertRaisesRegex(tasks.ReleaseTaskError, "必须先升级服务器"),
        ):
            tasks.assert_platform_server_support(
                "https://api.example.com",
                "test",
                None,
                require_source_version_targeting=True,
            )

        with (
            patch.object(
                tasks.urllib.request,
                "urlopen",
                return_value=CapabilityResponse(
                    [tasks.SOURCE_VERSION_TARGETING_CAPABILITY]
                ),
            ),
            patch.object(
                tasks,
                "request_manifest",
                return_value=(
                    200,
                    {},
                    {"x-intdemo-platform": "linux-aarch64"},
                ),
            ),
        ):
            tasks.assert_platform_server_support(
                "https://api.example.com",
                "test",
                None,
                require_source_version_targeting=True,
            )

    def test_windows_builder_accepts_exact_detached_commit(self):
        with patch.object(
            tasks,
            "git",
            side_effect=["HEAD", COMMIT, ""],
        ):
            self.assertEqual(
                tasks.source_state(Path("windows-checkout"), allow_detached=True),
                ("HEAD", COMMIT),
            )

        with (
            patch.object(tasks, "git", return_value="HEAD"),
            self.assertRaisesRegex(tasks.ReleaseTaskError, "具名 Git 分支"),
        ):
            tasks.source_state(Path("uos-publisher"))

    def test_external_targets_cannot_be_parsed_as_command_options(self):
        self.assertIsNone(tasks.REMOTE_HOST_PATTERN.fullmatch("-V"))
        with self.assertRaisesRegex(tasks.ReleaseTaskError, "远程名称"):
            tasks.create_windows_request(
                Path("unused"),
                version="1.2.3",
                base_url="https://api.example.com",
                channel="test",
                ca_bundle="",
                delta_from_version="",
                build_portable=False,
                github_remote="--upload-pack",
            )

    def test_request_export_binds_source_config_ca_and_real_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ca = root / "root.crt"
            ca.write_bytes(b"public root certificate")
            baseline = root / "dist" / "release-snapshots" / "1.2.2.json"
            write_snapshot(baseline, "1.2.2")
            with (
                patch.object(tasks, "source_state", return_value=("release/v1", COMMIT)),
                patch.object(tasks, "current_version", return_value="1.2.3"),
                patch.object(
                    tasks,
                    "git",
                    return_value="https://github.com/e23Aqiu/intdemo.git",
                ),
            ):
                archive = tasks.create_windows_request(
                    root,
                    version="1.2.3",
                    base_url="https://121.1.2.3",
                    channel="stable",
                    ca_bundle=str(ca),
                    delta_from_version="1.2.2",
                    build_portable=True,
                    github_remote="origin",
                )

            with tempfile.TemporaryDirectory() as extracted_directory:
                extracted = Path(extracted_directory)
                tasks.extract_zip_safely(archive, extracted)
                request, _path, _digest = tasks.load_request_from_directory(extracted)
            self.assertEqual(request["source_commit"], COMMIT)
            self.assertEqual(request["source_branch"], "release/v1")
            self.assertEqual(request["base_url"], "https://121.1.2.3")
            self.assertEqual(request["channel"], "stable")
            self.assertTrue(request["build_portable"])
            self.assertEqual(request["delta_from_version"], "1.2.2")
            self.assertEqual(
                request["inputs"]["ca_bundle"]["sha256"],
                tasks.sha256(ca),
            )
            self.assertEqual(
                request["inputs"]["baseline_snapshot"]["sha256"],
                tasks.sha256(baseline),
            )

    def test_request_rejects_missing_delta_snapshot_without_empty_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch.object(tasks, "source_state", return_value=("release/v1", COMMIT)),
                patch.object(tasks, "current_version", return_value="1.2.3"),
                patch.object(
                    tasks,
                    "git",
                    return_value="https://github.com/e23Aqiu/intdemo.git",
                ),
                self.assertRaisesRegex(tasks.ReleaseTaskError, "基线快照"),
            ):
                tasks.create_windows_request(
                    root,
                    version="1.2.3",
                    base_url="https://api.example.com",
                    channel="test",
                    ca_bundle="",
                    delta_from_version="1.2.2",
                    build_portable=False,
                    github_remote="origin",
                )
            self.assertFalse((root / "dist" / "windows-build-requests").exists())

    def test_zip_extraction_rejects_traversal_and_symlink_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            unsafe_archives = []
            traversal = root / "traversal.zip"
            with zipfile.ZipFile(traversal, "w") as archive:
                archive.writestr("../outside.txt", b"unsafe")
            unsafe_archives.append(traversal)
            symlink = root / "symlink.zip"
            with zipfile.ZipFile(symlink, "w") as archive:
                info = zipfile.ZipInfo("link")
                info.create_system = 3
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
                archive.writestr(info, "target")
            unsafe_archives.append(symlink)

            for archive in unsafe_archives:
                with self.subTest(archive=archive.name), self.assertRaisesRegex(
                    tasks.ReleaseTaskError,
                    "不安全路径",
                ):
                    tasks.extract_zip_safely(archive, root / archive.stem)
            self.assertFalse((root.parent / "outside.txt").exists())

    def test_windows_result_portable_mode_is_detected_from_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            installer_only = create_result_archive(root / "installer-only")
            with_portable = create_result_archive(
                root / "with-portable",
                build_portable=True,
            )

            self.assertFalse(
                tasks.detect_windows_result_portable(installer_only)
            )
            self.assertTrue(tasks.detect_windows_result_portable(with_portable))

    def test_windows_result_portable_detection_rejects_missing_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = create_result_archive(root / "source", build_portable=True)
            invalid = root / "missing-portable.zip"
            with (
                zipfile.ZipFile(source) as source_archive,
                zipfile.ZipFile(invalid, "w") as target_archive,
            ):
                for info in source_archive.infolist():
                    if info.filename.endswith("Portable-1.2.3.zip"):
                        continue
                    target_archive.writestr(info, source_archive.read(info))

            with self.assertRaisesRegex(tasks.ReleaseTaskError, "缺少构建产物"):
                tasks.detect_windows_result_portable(invalid)

    def test_result_import_verifies_request_snapshot_and_artifact_hashes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = create_result_archive(root)
            with (
                patch.object(tasks, "source_state", return_value=("release/v1", COMMIT)),
                patch.object(tasks, "current_version", return_value="1.2.3"),
            ):
                receipt_path = tasks.import_windows_result(
                    root,
                    result_archive=archive,
                    version="1.2.3",
                    base_url="https://api.example.com",
                    channel="test",
                    ca_bundle="",
                    delta_from_version="",
                    build_portable=False,
                )
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            self.assertEqual(receipt["source_commit"], COMMIT)
            self.assertEqual(
                set(receipt["artifacts"]),
                {"windows_installer", "windows_snapshot"},
            )
            for descriptor in receipt["artifacts"].values():
                path = root / descriptor["path"]
                self.assertEqual(tasks.sha256(path), descriptor["sha256"])

            tampered_root = root / "tampered"
            tampered_root.mkdir()
            tampered_archive = create_result_archive(
                tampered_root,
                tamper_installer=True,
            )
            with (
                patch.object(tasks, "source_state", return_value=("release/v1", COMMIT)),
                patch.object(tasks, "current_version", return_value="1.2.3"),
                self.assertRaisesRegex(tasks.ReleaseTaskError, "大小|SHA-256"),
            ):
                tasks.import_windows_result(
                    tampered_root,
                    result_archive=tampered_archive,
                    version="1.2.3",
                    base_url="https://api.example.com",
                    channel="test",
                    ca_bundle="",
                    delta_from_version="",
                    build_portable=False,
                )

    def test_result_request_must_match_current_release_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = create_result_archive(root)
            ca = root / "different.crt"
            ca.write_bytes(b"different certificate")
            cases = (
                (COMMIT, "https://other.example.com", "test", "", "base_url"),
                (COMMIT, "https://api.example.com", "stable", "", "channel"),
                (COMMIT, "https://api.example.com", "test", str(ca), "CA"),
                (OTHER_COMMIT, "https://api.example.com", "test", "", "source_commit"),
            )
            for commit, base_url, channel, ca_bundle, expected in cases:
                with (
                    self.subTest(expected=expected),
                    patch.object(
                        tasks,
                        "source_state",
                        return_value=("release/v1", commit),
                    ),
                    patch.object(tasks, "current_version", return_value="1.2.3"),
                    self.assertRaisesRegex(tasks.ReleaseTaskError, expected),
                ):
                    tasks.import_windows_result(
                        root,
                        result_archive=archive,
                        version="1.2.3",
                        base_url=base_url,
                        channel=channel,
                        ca_bundle=ca_bundle,
                        delta_from_version="",
                        build_portable=False,
                    )

    def test_uos_receipt_checks_build_info_package_config_and_ca(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = (
                root
                / "dist"
                / "uos-arm64"
                / "package"
                / "IntDemo-UOS-arm64-1.2.3"
            )
            certificate = package / "certs" / "root.crt"
            certificate.parent.mkdir(parents=True)
            certificate.write_bytes(b"public root certificate")
            source_ca = root / "root.crt"
            source_ca.write_bytes(certificate.read_bytes())
            (package / "build-info.txt").write_text(
                f"version=1.2.3\ngit_commit={COMMIT}\n",
                encoding="utf-8",
            )
            tasks.write_json(
                package / "client-online.json",
                {
                    "base_url": "https://121.1.2.3",
                    "channel": "stable",
                    "ca_bundle": "certs/root.crt",
                },
            )
            deb = root / "dist" / "uos-arm64" / "IntDemo-UOS-arm64-1.2.3.deb"
            deb.write_bytes(b"deb package")
            with (
                patch.object(tasks, "source_state", return_value=("release/v1", COMMIT)),
                patch.object(tasks, "current_version", return_value="1.2.3"),
                patch.object(tasks, "validate_uos_deb_payload") as validate_deb,
            ):
                receipt_path = tasks.record_uos_result(
                    root,
                    version="1.2.3",
                    base_url="https://121.1.2.3",
                    channel="stable",
                    ca_bundle=str(source_ca),
                )
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            self.assertEqual(receipt["platform"], "linux-aarch64")
            self.assertEqual(receipt["ca_sha256"], tasks.sha256(source_ca))
            validate_deb.assert_called_once()

            config = json.loads(
                (package / "client-online.json").read_text(encoding="utf-8")
            )
            config["channel"] = "test"
            tasks.write_json(package / "client-online.json", config)
            with (
                patch.object(tasks, "source_state", return_value=("release/v1", COMMIT)),
                patch.object(tasks, "current_version", return_value="1.2.3"),
                patch.object(tasks, "validate_uos_deb_payload"),
                self.assertRaisesRegex(tasks.ReleaseTaskError, "通道"),
            ):
                tasks.record_uos_result(
                    root,
                    version="1.2.3",
                    base_url="https://121.1.2.3",
                    channel="stable",
                    ca_bundle=str(source_ca),
                )

    def test_uos_result_archive_can_be_imported_on_either_platform(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = create_uos_result_archive(root)
            with (
                patch.object(tasks, "source_state", return_value=("release/v1", COMMIT)),
                patch.object(tasks, "current_version", return_value="1.2.3"),
                patch.object(tasks.shutil, "which", return_value=None),
            ):
                receipt_path = tasks.import_uos_result(
                    root,
                    result_archive=archive,
                    version="1.2.3",
                    base_url="https://api.example.com",
                    channel="test",
                    ca_bundle="",
                )

            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            installer = root / receipt["artifacts"]["uos_installer"]["path"]
            self.assertEqual(receipt["platform"], "linux-aarch64")
            self.assertEqual(
                tasks.sha256(installer),
                receipt["artifacts"]["uos_installer"]["sha256"],
            )
            self.assertTrue(installer.with_suffix(".deb.sha256").is_file())

    def test_uos_result_auto_detects_delta_source_for_windows_import(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = create_uos_result_archive(
                root,
                uos_delta_from_version="1.2.2",
            )

            source = tasks.detect_uos_result_delta_source(archive)

            self.assertEqual(source, "1.2.2")

    def test_uos_published_base_round_trips_between_windows_and_uos(self):
        with (
            tempfile.TemporaryDirectory() as source_directory,
            tempfile.TemporaryDirectory() as target_directory,
        ):
            source_root = Path(source_directory)
            target_root = Path(target_directory)
            version = "1.2.1"
            deb = (
                source_root
                / "dist/update-release/files"
                / f"IntDemo-UOS-arm64-{version}.deb"
            )
            receipt = (
                source_root
                / "dist/release-results"
                / version
                / "publish-receipt.json"
            )
            deb.parent.mkdir(parents=True)
            receipt.parent.mkdir(parents=True)
            deb.write_bytes(b"real published uos deb")
            tasks.write_json(
                receipt,
                {
                    "schema_version": 1,
                    "version": version,
                    "source_commit": COMMIT,
                    "artifacts": {
                        "uos_installer": {
                            "name": deb.name,
                            "size": deb.stat().st_size,
                            "sha256": tasks.sha256(deb),
                        }
                    },
                },
            )
            archive = source_root / "uos-delta-base.zip"

            tasks.export_uos_delta_base(
                source_root,
                version=version,
                output=archive,
            )
            self.assertEqual(
                tasks.detect_uos_delta_base_version(archive),
                version,
            )
            imported = tasks.import_uos_delta_base(
                target_root,
                base_archive=archive,
            )

            self.assertEqual(imported, version)
            imported_deb = (
                target_root / "dist/update-release/files" / deb.name
            )
            imported_receipt = (
                target_root
                / "dist/release-results"
                / version
                / "publish-receipt.json"
            )
            self.assertEqual(imported_deb.read_bytes(), deb.read_bytes())
            self.assertEqual(tasks.sha256(imported_receipt), tasks.sha256(receipt))

            tampered_staging = target_root / "tampered-staging"
            tasks.extract_zip_safely(archive, tampered_staging)
            tampered_deb = next((tampered_staging / "artifacts").glob("*.deb"))
            tampered_deb.write_bytes(b"tampered transfer")
            tampered_archive = target_root / "tampered-uos-delta-base.zip"
            tasks.make_zip(tampered_staging, tampered_archive)
            with self.assertRaisesRegex(tasks.ReleaseTaskError, "大小|SHA-256"):
                tasks.import_uos_delta_base(
                    target_root / "tampered-import",
                    base_archive=tampered_archive,
                )

            imported_deb.write_bytes(b"different local baseline")
            with self.assertRaisesRegex(tasks.ReleaseTaskError, "拒绝覆盖"):
                tasks.import_uos_delta_base(
                    target_root,
                    base_archive=archive,
                )

            with self.assertRaisesRegex(tasks.ReleaseTaskError, "版本不受支持"):
                tasks.validate_uos_delta_base_payload(
                    {"schema_version": "invalid"}
                )

    def test_uos_result_import_rejects_tampering_and_config_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tampered = create_uos_result_archive(root, tamper_installer=True)
            with (
                patch.object(tasks, "source_state", return_value=("release/v1", COMMIT)),
                patch.object(tasks, "current_version", return_value="1.2.3"),
                self.assertRaisesRegex(tasks.ReleaseTaskError, "大小|SHA-256"),
            ):
                tasks.import_uos_result(
                    root,
                    result_archive=tampered,
                    version="1.2.3",
                    base_url="https://api.example.com",
                    channel="test",
                    ca_bundle="",
                )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = create_uos_result_archive(root)
            with (
                patch.object(tasks, "source_state", return_value=("release/v1", COMMIT)),
                patch.object(tasks, "current_version", return_value="1.2.3"),
                self.assertRaisesRegex(tasks.ReleaseTaskError, "base_url"),
            ):
                tasks.import_uos_result(
                    root,
                    result_archive=archive,
                    version="1.2.3",
                    base_url="https://other.example.com",
                    channel="test",
                    ca_bundle="",
                )

    def test_uos_result_export_contains_validated_deb_and_checksum(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            installer = root / "dist" / "uos-arm64" / "IntDemo-UOS-arm64-1.2.3.deb"
            installer.parent.mkdir(parents=True)
            installer.write_bytes(b"validated uos installer")
            receipt_path = root / "dist" / "uos-build-results" / "1.2.3" / "validated-result.json"
            tasks.write_json(
                receipt_path,
                {
                    "schema_version": 1,
                    "platform": "linux-aarch64",
                    "version": "1.2.3",
                    "source_commit": COMMIT,
                    "base_url": "https://api.example.com",
                    "channel": "test",
                    "ca_sha256": "",
                    "validated_at": "2026-08-05T00:01:00Z",
                    "artifacts": {
                        "uos_installer": tasks.receipt_artifact(root, installer),
                    },
                },
            )
            output = root / "uos-result.zip"
            with patch.object(tasks, "record_uos_result", return_value=receipt_path):
                archive = tasks.export_uos_result(
                    root,
                    version="1.2.3",
                    base_url="https://api.example.com",
                    channel="test",
                    ca_bundle="",
                    output=output,
                )

            self.assertEqual(archive, output.resolve())
            self.assertTrue(output.with_suffix(".zip.sha256").is_file())
            with tempfile.TemporaryDirectory() as extracted_directory:
                extracted = Path(extracted_directory)
                tasks.extract_zip_safely(output, extracted)
                result = tasks.validate_uos_result_payload(
                    json.loads(
                        (extracted / tasks.UOS_RESULT_FILE).read_text(encoding="utf-8")
                    )
                )
                packaged = tasks.verify_file(
                    extracted,
                    result["artifacts"]["uos_installer"],
                    "UOS 安装包",
                )
            self.assertEqual(packaged.name, "IntDemo-UOS-arm64-1.2.3.deb")

    def test_dual_manifest_keeps_legacy_windows_fields(self):
        windows = {"name": "setup.exe", "size": 10, "sha256": "1" * 64}
        uos = {"name": "client.deb", "size": 20, "sha256": "2" * 64}
        delta = {"name": "patch.exe", "size": 5, "sha256": "3" * 64}
        manifest = tasks.prepare_update_manifest(
            version="1.2.3",
            channel="stable",
            source_commit=COMMIT,
            notes="release",
            mandatory=True,
            windows=windows,
            uos=uos,
            delta=delta,
            delta_from_version="1.2.2",
        )
        self.assertEqual(manifest["installer_path"], "/updates/files/setup.exe")
        self.assertEqual(manifest["sha256"], windows["sha256"])
        self.assertEqual(
            set(manifest["platforms"]),
            {"windows-x86_64", "linux-aarch64"},
        )
        self.assertEqual(
            manifest["platforms"]["windows-x86_64"]["deltas"],
            manifest["deltas"],
        )
        self.assertNotIn("deltas", manifest["platforms"]["linux-aarch64"])

    def test_dual_manifest_can_publish_capability_gated_uos_delta(self):
        windows = {"name": "setup.exe", "size": 10, "sha256": "1" * 64}
        uos = {"name": "client.deb", "size": 200, "sha256": "2" * 64}
        uos_delta = {
            "name": "client.intdelta",
            "size": 80,
            "sha256": "3" * 64,
            "base_size": 190,
            "base_sha256": "4" * 64,
        }

        manifest = tasks.prepare_update_manifest(
            version="1.2.3",
            channel="stable",
            source_commit=COMMIT,
            notes="release",
            mandatory=False,
            windows=windows,
            uos=uos,
            delta=None,
            delta_from_version="",
            uos_delta=uos_delta,
            uos_delta_from_version="1.2.2",
        )

        delta = manifest["platforms"]["linux-aarch64"]["deltas"][0]
        self.assertEqual(delta["format"], "uos-deb-xdelta-v1")
        self.assertEqual(delta["algorithm"], "xdelta3")
        self.assertEqual(delta["from_version"], "1.2.2")
        self.assertEqual(delta["base_sha256"], "4" * 64)
        self.assertEqual(delta["base_size"], 190)
        self.assertEqual(delta["target_sha256"], uos["sha256"])
        self.assertEqual(delta["target_size"], uos["size"])
        self.assertEqual(delta["installer_path"], "/updates/files/client.intdelta")

    def test_uos_delta_candidate_requires_real_released_base_and_replay_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = (
                root
                / "dist/update-release/files/IntDemo-UOS-arm64-1.2.2.deb"
            )
            target = root / "dist/uos-arm64/IntDemo-UOS-arm64-1.2.3.deb"
            patch_file = (
                root
                / "dist/uos-arm64/IntDemo-UOS-arm64-Patch-"
                "1.2.2-to-1.2.3.intdelta"
            )
            receipt_path = (
                root / "dist/release-results/1.2.2/publish-receipt.json"
            )
            source.parent.mkdir(parents=True)
            target.parent.mkdir(parents=True)
            receipt_path.parent.mkdir(parents=True)
            source.write_bytes(b"s" * 90)
            target.write_bytes(b"t" * 100)
            patch_file.write_bytes(b"p" * 40)
            receipt_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "version": "1.2.2",
                        "artifacts": {
                            "uos_installer": {
                                "name": source.name,
                                "size": source.stat().st_size,
                                "sha256": tasks.sha256(source),
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            report = {
                "schema_version": 1,
                "format": "uos-deb-xdelta-v1",
                "algorithm": "xdelta3",
                "from_version": "1.2.2",
                "target_version": "1.2.3",
                "base_name": source.name,
                "base_size": source.stat().st_size,
                "base_sha256": tasks.sha256(source),
                "target_name": target.name,
                "target_size": target.stat().st_size,
                "target_sha256": tasks.sha256(target),
                "patch_name": patch_file.name,
                "patch_size": patch_file.stat().st_size,
                "patch_sha256": tasks.sha256(patch_file),
                "ratio_percent": 40.0,
                "threshold_percent": 50,
                "eligible": True,
                "byte_identical": True,
            }
            patch_file.with_suffix(".intdelta.json").write_text(
                json.dumps(report),
                encoding="utf-8",
            )

            patch_path, report_path, loaded = tasks.validate_uos_delta_candidate(
                root,
                from_version="1.2.2",
                target_version="1.2.3",
                target_deb=target,
            )

            self.assertEqual(patch_path, patch_file)
            self.assertEqual(report_path, patch_file.with_suffix(".intdelta.json"))
            self.assertEqual(loaded["base_sha256"], tasks.sha256(source))

            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            receipt["version"] = "1.2.1"
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
            with self.assertRaisesRegex(tasks.ReleaseTaskError, "收据版本"):
                tasks.validate_uos_delta_candidate(
                    root,
                    from_version="1.2.2",
                    target_version="1.2.3",
                    target_deb=target,
                )
            receipt["version"] = "1.2.2"
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

            source.write_bytes(b"tampered")
            with self.assertRaisesRegex(tasks.ReleaseTaskError, "真实发布 DEB"):
                tasks.validate_uos_delta_candidate(
                    root,
                    from_version="1.2.2",
                    target_version="1.2.3",
                    target_deb=target,
                )

    def test_remote_guards_refuse_equal_or_higher_versions(self):
        for current in ("1.2.3", "1.2.4"):
            with self.subTest(current=current), self.assertRaisesRegex(
                tasks.ReleaseTaskError,
                "不能覆盖或降级",
            ):
                tasks.remote_manifest_guard({"version": current}, "1.2.3")
        self.assertTrue(
            tasks.remote_manifest_guard(
                {"paused": True, "paused_version": "1.2.2"},
                "1.2.3",
            )
        )

    def test_ssh_options_enable_keepalive_and_optional_identity(self):
        identity = Path("release-key.pem")

        options = tasks.ssh_options(identity)

        for setting in tasks.SSH_CONNECTION_OPTIONS:
            self.assertIn(setting, options)
        self.assertIn("BatchMode=yes", options)
        self.assertEqual(options[-2:], ["-i", str(identity)])
        self.assertNotIn("-i", tasks.ssh_options(None))

    def test_run_command_turns_process_timeout_into_release_error(self):
        arguments = ["ssh", "release-server", "wc -c /tmp/file"]
        with (
            patch.object(
                tasks.subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired(arguments, 3),
            ),
            self.assertRaisesRegex(
                tasks.ReleaseTaskError,
                r"命令执行超时（3 秒）",
            ),
        ):
            tasks.run_command(
                arguments,
                cwd=Path("repository"),
                capture=True,
                timeout=3,
            )

    def test_remote_queries_use_operation_specific_timeouts(self):
        with patch.object(tasks, "capture_command", return_value="123") as capture:
            self.assertEqual(
                tasks.remote_file_size(
                    Path("repository"),
                    "release-server",
                    "/opt/intdemo/updates/setup.exe.part",
                    None,
                ),
                123,
            )
        self.assertEqual(
            capture.call_args.kwargs["timeout"],
            tasks.SSH_PROGRESS_QUERY_TIMEOUT_SECONDS,
        )

        with patch.object(tasks, "capture_command", return_value="a" * 64) as capture:
            self.assertEqual(
                tasks.remote_hash(
                    Path("repository"),
                    "release-server",
                    "/opt/intdemo/updates/setup.exe.part",
                    None,
                ),
                "a" * 64,
            )
        self.assertEqual(
            capture.call_args.kwargs["timeout"],
            tasks.SSH_HASH_TIMEOUT_SECONDS,
        )

    def test_release_incoming_path_is_stable_for_the_same_artifacts(self):
        first = {
            "windows": {
                "name": "setup.exe",
                "path": Path("C:/first/setup.exe"),
                "size": 10,
                "sha256": "1" * 64,
            },
            "uos": {
                "name": "setup.deb",
                "path": Path("C:/first/setup.deb"),
                "size": 20,
                "sha256": "2" * 64,
            },
        }
        second = {
            "uos": {**first["uos"], "path": Path("D:/other/setup.deb")},
            "windows": {
                **first["windows"],
                "path": Path("D:/other/setup.exe"),
            },
        }

        first_path = tasks.release_incoming_path(
            "/opt/intdemo/updates",
            "1.2.3",
            COMMIT,
            first,
        )
        second_path = tasks.release_incoming_path(
            "/opt/intdemo/updates",
            "1.2.3",
            COMMIT,
            second,
        )

        self.assertEqual(first_path, second_path)
        changed = {**second, "windows": {**second["windows"], "size": 11}}
        self.assertNotEqual(
            first_path,
            tasks.release_incoming_path(
                "/opt/intdemo/updates",
                "1.2.3",
                COMMIT,
                changed,
            ),
        )

    def test_sftp_upload_uses_reput_batch_command(self):
        class CompletedProcess:
            @staticmethod
            def wait(timeout=None):
                return 0

            @staticmethod
            def terminate():
                return None

            @staticmethod
            def kill():
                return None

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / "setup.exe"
            artifact.write_bytes(b"installer")
            observed: dict[str, object] = {}

            def start_process(arguments, **kwargs):
                batch_index = arguments.index("-b") + 1
                observed["arguments"] = arguments
                observed["batch"] = Path(arguments[batch_index]).read_text(
                    encoding="utf-8"
                )
                return CompletedProcess()

            with patch.object(tasks.subprocess, "Popen", side_effect=start_process):
                tasks.sftp_reput_once(
                    root,
                    host="release-server",
                    local_path=artifact,
                    remote_path="/opt/intdemo/updates/setup.exe.part",
                    identity_file=None,
                    progress_interval=0.01,
                )

        self.assertTrue(str(observed["batch"]).startswith("reput "))
        self.assertIn("setup.exe.part", str(observed["batch"]))
        self.assertIn("ServerAliveInterval=15", observed["arguments"])

    def test_sftp_upload_recovers_when_progress_query_times_out(self):
        class CompletedAfterProbe:
            def __init__(self):
                self.wait_count = 0

            def wait(self, timeout=None):
                self.wait_count += 1
                if self.wait_count == 1:
                    raise subprocess.TimeoutExpired(["sftp"], timeout)
                return 0

            @staticmethod
            def terminate():
                return None

            @staticmethod
            def kill():
                return None

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / "setup.exe"
            artifact.write_bytes(b"installer")
            process = CompletedAfterProbe()
            with (
                patch.object(tasks.subprocess, "Popen", return_value=process),
                patch.object(
                    tasks,
                    "remote_file_size",
                    side_effect=tasks.ReleaseTaskError("SSH progress query timed out"),
                ) as remote_size,
                patch("builtins.print") as output,
            ):
                tasks.sftp_reput_once(
                    root,
                    host="release-server",
                    local_path=artifact,
                    remote_path="/opt/intdemo/updates/setup.exe.part",
                    identity_file=None,
                    progress_interval=0.01,
                )

        remote_size.assert_called_once()
        self.assertEqual(process.wait_count, 2)
        self.assertIn("自动重试", output.call_args.args[0])

    def test_sftp_upload_terminates_when_progress_stays_unavailable(self):
        class StalledProcess:
            def __init__(self):
                self.terminated = False

            def wait(self, timeout=None):
                if self.terminated:
                    return 1
                raise subprocess.TimeoutExpired(["sftp"], timeout)

            def terminate(self):
                self.terminated = True

            @staticmethod
            def kill():
                return None

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / "setup.exe"
            artifact.write_bytes(b"installer")
            process = StalledProcess()
            with (
                patch.object(tasks.subprocess, "Popen", return_value=process),
                patch.object(
                    tasks,
                    "remote_file_size",
                    side_effect=tasks.ReleaseTaskError("progress unavailable"),
                ) as remote_size,
                patch.object(
                    tasks.time,
                    "monotonic",
                    side_effect=[0.0, 0.5, 1.5],
                ),
                patch("builtins.print"),
                self.assertRaisesRegex(
                    tasks.ReleaseTaskError,
                    r"连续 1 秒无可确认上传进度",
                ),
            ):
                tasks.sftp_reput_once(
                    root,
                    host="release-server",
                    local_path=artifact,
                    remote_path="/opt/intdemo/updates/setup.exe.part",
                    identity_file=None,
                    progress_interval=0.01,
                    stall_timeout=1.0,
                )

        self.assertTrue(process.terminated)
        self.assertEqual(remote_size.call_count, 2)

    def test_sftp_upload_resets_stall_timer_when_remote_size_grows(self):
        class CompletedAfterProgress:
            def __init__(self):
                self.wait_count = 0

            def wait(self, timeout=None):
                self.wait_count += 1
                if self.wait_count <= 2:
                    raise subprocess.TimeoutExpired(["sftp"], timeout)
                return 0

            @staticmethod
            def terminate():
                return None

            @staticmethod
            def kill():
                return None

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / "setup.exe"
            artifact.write_bytes(b"installer")
            process = CompletedAfterProgress()
            with (
                patch.object(tasks.subprocess, "Popen", return_value=process),
                patch.object(tasks, "remote_file_size", side_effect=[1, 2]),
                patch.object(
                    tasks.time,
                    "monotonic",
                    side_effect=[0.0, 0.5, 0.5, 1.2, 1.2],
                ),
                patch("builtins.print"),
            ):
                tasks.sftp_reput_once(
                    root,
                    host="release-server",
                    local_path=artifact,
                    remote_path="/opt/intdemo/updates/setup.exe.part",
                    identity_file=None,
                    progress_interval=0.01,
                    stall_timeout=1.0,
                )

        self.assertEqual(process.wait_count, 3)

    def test_resumable_upload_continues_an_existing_partial_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / "setup.exe"
            artifact.write_bytes(b"0123456789")
            expected_hash = tasks.sha256(artifact)
            state = {"size": 4}

            def finish_upload(*args, **kwargs):
                state["size"] = artifact.stat().st_size

            with (
                patch.object(tasks, "ensure_remote_partial"),
                patch.object(
                    tasks,
                    "remote_file_size",
                    side_effect=lambda *args, **kwargs: state["size"],
                ),
                patch.object(tasks, "remote_hash", return_value=expected_hash),
                patch.object(
                    tasks,
                    "sftp_reput_once",
                    side_effect=finish_upload,
                ) as reput,
                patch.object(tasks, "truncate_remote_file") as truncate,
                patch("builtins.print"),
            ):
                tasks.upload_file_resumable(
                    root,
                    host="release-server",
                    local_path=artifact,
                    remote_path="/opt/intdemo/updates/setup.exe.part",
                    expected_sha256=expected_hash,
                    identity_file=None,
                    max_attempts=1,
                )

        reput.assert_called_once()
        truncate.assert_not_called()

    def test_resumable_upload_retries_after_a_disconnect(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / "setup.exe"
            artifact.write_bytes(b"0123456789")
            expected_hash = tasks.sha256(artifact)
            state = {"size": 0, "attempt": 0}

            def upload_once(*args, **kwargs):
                state["attempt"] += 1
                if state["attempt"] == 1:
                    state["size"] = 4
                    raise tasks.ReleaseTaskError("connection lost")
                state["size"] = artifact.stat().st_size

            with (
                patch.object(tasks, "ensure_remote_partial"),
                patch.object(
                    tasks,
                    "remote_file_size",
                    side_effect=lambda *args, **kwargs: state["size"],
                ),
                patch.object(tasks, "remote_hash", return_value=expected_hash),
                patch.object(
                    tasks,
                    "sftp_reput_once",
                    side_effect=upload_once,
                ) as reput,
                patch.object(tasks.time, "sleep") as sleep,
                patch("builtins.print"),
            ):
                tasks.upload_file_resumable(
                    root,
                    host="release-server",
                    local_path=artifact,
                    remote_path="/opt/intdemo/updates/setup.exe.part",
                    expected_sha256=expected_hash,
                    identity_file=None,
                    max_attempts=2,
                    retry_delays=(0,),
                )

        self.assertEqual(reput.call_count, 2)
        sleep.assert_called_once_with(0)

    def test_resumable_upload_reuses_a_complete_verified_partial(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / "setup.exe"
            artifact.write_bytes(b"installer")
            expected_hash = tasks.sha256(artifact)
            with (
                patch.object(tasks, "ensure_remote_partial"),
                patch.object(
                    tasks,
                    "remote_file_size",
                    return_value=artifact.stat().st_size,
                ),
                patch.object(tasks, "remote_hash", return_value=expected_hash),
                patch.object(tasks, "sftp_reput_once") as reput,
                patch("builtins.print"),
            ):
                tasks.upload_file_resumable(
                    root,
                    host="release-server",
                    local_path=artifact,
                    remote_path="/opt/intdemo/updates/setup.exe.part",
                    expected_sha256=expected_hash,
                    identity_file=None,
                )

        reput.assert_not_called()

    def test_resumable_upload_restarts_a_complete_invalid_partial(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / "setup.exe"
            artifact.write_bytes(b"installer")
            expected_hash = tasks.sha256(artifact)
            state = {"size": artifact.stat().st_size}

            def truncate_file(*args, **kwargs):
                state["size"] = 0

            def finish_upload(*args, **kwargs):
                state["size"] = artifact.stat().st_size

            with (
                patch.object(tasks, "ensure_remote_partial"),
                patch.object(
                    tasks,
                    "remote_file_size",
                    side_effect=lambda *args, **kwargs: state["size"],
                ),
                patch.object(
                    tasks,
                    "remote_hash",
                    side_effect=["f" * 64, expected_hash],
                ),
                patch.object(
                    tasks,
                    "truncate_remote_file",
                    side_effect=truncate_file,
                ) as truncate,
                patch.object(
                    tasks,
                    "sftp_reput_once",
                    side_effect=finish_upload,
                ) as reput,
                patch("builtins.print"),
            ):
                tasks.upload_file_resumable(
                    root,
                    host="release-server",
                    local_path=artifact,
                    remote_path="/opt/intdemo/updates/setup.exe.part",
                    expected_sha256=expected_hash,
                    identity_file=None,
                    max_attempts=1,
                )

        truncate.assert_called_once()
        reput.assert_called_once()

    def test_resumable_upload_keeps_partial_after_all_retries_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / "setup.exe"
            artifact.write_bytes(b"0123456789")
            state = {"size": 3}
            with (
                patch.object(tasks, "ensure_remote_partial"),
                patch.object(
                    tasks,
                    "remote_file_size",
                    side_effect=lambda *args, **kwargs: state["size"],
                ),
                patch.object(
                    tasks,
                    "sftp_reput_once",
                    side_effect=tasks.ReleaseTaskError("connection lost"),
                ) as reput,
                patch.object(tasks.time, "sleep"),
                patch("builtins.print"),
                self.assertRaisesRegex(
                    tasks.ReleaseTaskError,
                    "重新点击发布会从当前进度继续",
                ),
            ):
                tasks.upload_file_resumable(
                    root,
                    host="release-server",
                    local_path=artifact,
                    remote_path="/opt/intdemo/updates/setup.exe.part",
                    expected_sha256=tasks.sha256(artifact),
                    identity_file=None,
                    max_attempts=3,
                    retry_delays=(0,),
                )

        self.assertEqual(reput.call_count, 3)

    def test_remote_publish_refuses_same_name_with_different_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / "setup.exe"
            artifact.write_bytes(b"new installer")
            manifest_path = root / "stable.json"
            tasks.write_json(manifest_path, {"version": "1.2.3"})
            descriptor = {
                "name": artifact.name,
                "path": artifact,
                "size": artifact.stat().st_size,
                "sha256": tasks.sha256(artifact),
            }
            with (
                patch.object(tasks, "run_command") as run_command,
                patch.object(tasks, "ssh_capture", return_value=""),
                patch.object(
                    tasks,
                    "remote_hash",
                    side_effect=["", "", "f" * 64],
                ),
                self.assertRaisesRegex(tasks.ReleaseTaskError, "拒绝覆盖"),
            ):
                tasks.publish_remote(
                    root,
                    version="1.2.3",
                    source_commit=COMMIT,
                    channel="stable",
                    manifest_path=manifest_path,
                    manifest={"version": "1.2.3"},
                    artifacts={"windows_installer": descriptor},
                    host="release-server",
                    remote_path="/opt/intdemo/updates",
                    identity_file=None,
                )
            self.assertEqual(run_command.call_count, 1)

    def test_remote_publish_uses_resumable_uploads_for_artifact_and_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artifact = root / "setup.exe"
            artifact.write_bytes(b"new installer")
            manifest = {"version": "1.2.3"}
            manifest_path = root / "stable.json"
            tasks.write_json(manifest_path, manifest)
            descriptor = {
                "name": artifact.name,
                "path": artifact,
                "size": artifact.stat().st_size,
                "sha256": tasks.sha256(artifact),
            }
            manifest_hash = tasks.sha256(manifest_path)
            with (
                patch.object(tasks, "run_command") as run_command,
                patch.object(tasks, "ssh_capture", return_value=""),
                patch.object(
                    tasks,
                    "remote_hash",
                    side_effect=["", "", "", manifest_hash],
                ),
                patch.object(tasks, "upload_file_resumable") as upload,
                patch.object(tasks, "load_remote_json", return_value=manifest),
            ):
                paused = tasks.publish_remote(
                    root,
                    version="1.2.3",
                    source_commit=COMMIT,
                    channel="stable",
                    manifest_path=manifest_path,
                    manifest=manifest,
                    artifacts={"windows_installer": descriptor},
                    host="release-server",
                    remote_path="/opt/intdemo/updates",
                    identity_file=None,
                )

        self.assertFalse(paused)
        self.assertEqual(upload.call_count, 2)
        artifact_upload = upload.call_args_list[0].kwargs
        manifest_upload = upload.call_args_list[1].kwargs
        self.assertTrue(artifact_upload["remote_path"].endswith("setup.exe.part"))
        self.assertTrue(manifest_upload["remote_path"].endswith("stable.json.part"))
        self.assertEqual(run_command.call_count, 2)
        self.assertFalse(
            any(call.args[0][0] == "scp" for call in run_command.call_args_list)
        )

    def test_github_run_can_be_identified_by_tag_display_title(self):
        tag = "intdemo-windows/v1.2.3-request"
        run = {
            "id": 123,
            "name": tasks.WINDOWS_WORKFLOW_NAME,
            "event": "push",
            "head_branch": None,
            "display_title": f"Windows release · {tag}",
        }
        with patch.object(tasks, "workflow_runs", return_value=[run]):
            self.assertEqual(
                tasks.wait_for_windows_run(REPO_ROOT, "owner/repo", tag, COMMIT, 1),
                run,
            )

    def test_github_fallback_exit_and_build_scripts_do_not_call_app_server(self):
        self.assertEqual(tasks.GITHUB_UNAVAILABLE_EXIT, 20)
        workflow = (REPO_ROOT / ".github/workflows/windows-release.yml").read_text(
            encoding="utf-8"
        )
        helper = (REPO_ROOT / "scripts/build-windows-request.ps1").read_text(
            encoding="utf-8"
        )
        combined = (workflow + helper).casefold()
        for marker in ("invoke-restmethod", "invoke-webrequest", "/api/v1/"):
            self.assertNotIn(marker, combined)

        result = subprocess.run(
            [
                sys.executable,
                "-S",
                str(REPO_ROOT / "release_publisher/release_tasks.py"),
                "--help",
            ],
            cwd=REPO_ROOT,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env={**os.environ, "PYTHONUTF8": "1"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("windows-github", result.stdout)


if __name__ == "__main__":
    unittest.main()

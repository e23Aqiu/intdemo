import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from integrated_client.trainer_component import (
    STATUS_AVAILABLE,
    STATUS_DAMAGED,
    STATUS_INCOMPATIBLE,
    STATUS_NOT_INSTALLED,
    TRAINER_COMPONENT_ID,
    TrainerBusyError,
    TrainerCompatibilityError,
    TrainerComponentError,
    TrainerComponentManager,
    TrainerPackageError,
    TrainerSelfTestError,
)


_LOCK_HOLDER_SCRIPT = """
import os
import sys
import time
from pathlib import Path

from integrated_client.trainer_component import TrainerComponentManager

manager = TrainerComponentManager(
    data_dir=Path(sys.argv[1]),
    platform_key="windows-x86_64",
    current_version="1.1.0",
)
with manager.training_lock():
    Path(sys.argv[2]).write_text(str(os.getpid()), encoding="ascii")
    while not Path(sys.argv[3]).exists():
        time.sleep(0.02)
    os._exit(23)
"""


def _wait_for_file(path, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            return True
        time.sleep(0.02)
    return False


class TrainerPackageFactory:
    def manifest(
        self,
        files,
        *,
        version="1.0.0",
        platform="windows-x86_64",
        entrypoint="bin/intdemo-trainer.exe",
        **overrides,
    ):
        entries = [
            {
                "path": name,
                "size": len(value),
                "sha256": hashlib.sha256(value).hexdigest(),
            }
            for name, value in files
        ]
        payload = {
            "schema_version": 1,
            "component_id": TRAINER_COMPONENT_ID,
            "version": version,
            "platform": platform,
            "protocol_version": 1,
            "min_client_version": "1.1.0",
            "entrypoint": entrypoint,
            "capabilities": {
                "numeric": "tiny-cnn-four-head-v1",
                "click": "tiny-cnn-character-v1",
            },
            "files": entries,
        }
        payload.update(overrides)
        return payload

    def package(self, path, files=None, manifest=None, extra=(), members=None):
        files = files or [("bin/intdemo-trainer.exe", b"trainer")]
        manifest = manifest or self.manifest(files)
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            if members is None:
                archive.writestr(
                    "manifest.json",
                    json.dumps(manifest, ensure_ascii=False, separators=(",", ":")),
                )
                for name, value in files:
                    archive.writestr(name, value)
                for name, value in extra:
                    archive.writestr(name, value)
            else:
                for member, value in members:
                    archive.writestr(member, value)
        return path


class TrainerComponentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.factory = TrainerPackageFactory()

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def successful_runner(command, **_kwargs):
        component_root = Path(command[0]).parents[1]
        manifest = json.loads((component_root / "manifest.json").read_text("utf-8"))
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(
                {
                    "ok": True,
                    "protocol_version": 1,
                    "component_version": manifest["version"],
                }
            ),
            stderr="",
        )

    def manager(self, **kwargs):
        kwargs.setdefault("runner", self.successful_runner)
        return TrainerComponentManager(
            data_dir=self.root / "data",
            platform_key="windows-x86_64",
            current_version="1.1.0",
            **kwargs,
        )

    def package(self, name="trainer.inttrainer", **kwargs):
        return self.factory.package(self.root / name, **kwargs)

    def test_install_status_command_preference_self_test_and_uninstall(self):
        manager = self.manager()
        package = self.package()

        self.assertEqual(manager.status().code, STATUS_NOT_INSTALLED)
        result = manager.install(package)

        self.assertEqual(result.status.code, STATUS_AVAILABLE)
        self.assertEqual(result.status.version, "1.0.0")
        self.assertEqual(result.status.platform, "windows-x86_64")
        self.assertEqual(result.status.numeric_method, "tiny-cnn-four-head-v1")
        expected_size = sum(
            path.stat().st_size
            for path in manager.component_root.rglob("*")
            if path.is_file() and not path.is_symlink()
        )
        self.assertEqual(result.status.installed_size, expected_size)
        self.assertTrue(manager.active_executable().is_file())
        command = manager.command_for(
            self.root / "samples.zip",
            "numeric",
            self.root / "output",
        )
        self.assertEqual(command[1:4], ["train", "--protocol-version", "1"])
        self.assertIn("--dataset", command)
        with patch.dict(
            os.environ,
            {"INTDEMO_TRAINER_SIGNING_PRIVATE_KEY": "never-forward"},
        ):
            environment = manager.training_environment()
        self.assertEqual(environment["INTDEMO_TRAINER_PROTOCOL_VERSION"], "1")
        self.assertNotIn("INTDEMO_TRAINER_SIGNING_PRIVATE_KEY", environment)
        outputs = manager.training_output_paths(self.root / "output")
        self.assertEqual(outputs["model"].name, "candidate.onnx")

        self.assertEqual(manager.preferred_mode(), "standard")
        self.assertEqual(manager.set_preferred_mode("enhanced"), "enhanced")
        self.assertEqual(manager.preferred_mode(), "enhanced")
        self.assertEqual(manager.training_epochs(), 24)
        self.assertEqual(manager.set_training_epochs(37), 37)
        self.assertEqual(self.manager().training_epochs(), 37)
        with self.assertRaises(ValueError):
            manager.set_training_epochs(0)
        with self.assertRaises(ValueError):
            manager.set_training_epochs(True)
        with self.assertRaises(ValueError):
            manager.set_training_epochs(201)
        self.assertEqual(manager.self_test().code, STATUS_AVAILABLE)

        released = manager.uninstall()
        self.assertGreater(released, 0)
        self.assertEqual(manager.status().code, STATUS_NOT_INSTALLED)
        self.assertEqual(manager.preferred_mode(), "standard")
        self.assertEqual(manager.training_epochs(), 37)
        self.assertTrue((self.root / "samples.zip").parent.exists())

    def test_training_output_cannot_overwrite_component(self):
        manager = self.manager()
        manager.install(self.package())
        with self.assertRaisesRegex(ValueError, "安装目录"):
            manager.command_for(
                self.root / "samples.zip",
                "numeric",
                manager.component_root / "training-output",
            )

    def test_cannot_select_enhanced_mode_without_component(self):
        with self.assertRaises(TrainerComponentError):
            self.manager().set_preferred_mode("enhanced")

    def test_status_rejects_link_like_component_root_without_reading_it(self):
        manager = self.manager()
        manager.component_root.mkdir(parents=True)
        real_is_link_like = manager._is_link_like

        with patch.object(
            manager,
            "_is_link_like",
            side_effect=lambda path: (
                Path(path) == manager.component_root or real_is_link_like(Path(path))
            ),
        ), patch.object(
            Path,
            "read_bytes",
            side_effect=AssertionError("linked component root must not be read"),
        ):
            status = manager.status(verify_files=False)

        self.assertEqual(status.code, STATUS_DAMAGED)
        self.assertIn("链接", status.message)

    def test_install_rejects_link_like_component_root(self):
        manager = self.manager()
        manager.component_root.mkdir(parents=True)
        real_is_link_like = manager._is_link_like

        with patch.object(
            manager,
            "_is_link_like",
            side_effect=lambda path: (
                Path(path) == manager.component_root or real_is_link_like(Path(path))
            ),
        ):
            with self.assertRaisesRegex(TrainerComponentError, "链接"):
                manager.install(self.package())

    def test_status_rejects_link_like_active_record_without_reading_it(self):
        manager = self.manager()
        manager.install(self.package())
        real_is_link_like = manager._is_link_like
        real_read_bytes = Path.read_bytes

        def reject_active_record(path):
            if Path(path) == manager.active_path:
                raise AssertionError("linked active record must not be read")
            return real_read_bytes(path)

        with patch.object(
            manager,
            "_is_link_like",
            side_effect=lambda path: (
                Path(path) == manager.active_path or real_is_link_like(Path(path))
            ),
        ), patch.object(Path, "read_bytes", reject_active_record):
            status = manager.status(verify_files=False)

        self.assertEqual(status.code, STATUS_DAMAGED)
        self.assertIn("链接", status.message)
        self.assertTrue(status.maintenance_required)
        self.assertFalse(status.cleanup_available)

    def test_install_rejects_link_like_versions_root_before_reading_package(self):
        manager = self.manager()
        manager.versions_root.mkdir(parents=True)
        real_is_link_like = manager._is_link_like

        with patch.object(
            manager,
            "_is_link_like",
            side_effect=lambda path: (
                Path(path) == manager.versions_root or real_is_link_like(Path(path))
            ),
        ), patch.object(
            manager,
            "_read_archive",
            side_effect=AssertionError("package must not be read"),
        ):
            with self.assertRaisesRegex(TrainerComponentError, "版本根目录"):
                manager.install(self.package())

    def test_component_root_file_blocks_install_and_uninstall(self):
        manager = self.manager()
        manager.component_root.parent.mkdir(parents=True)
        manager.component_root.write_bytes(b"not-a-directory")

        with self.assertRaisesRegex(TrainerComponentError, "不是目录"):
            manager.install(self.package())
        with self.assertRaisesRegex(TrainerComponentError, "类型无效"):
            manager.uninstall()
        self.assertEqual(manager.component_root.read_bytes(), b"not-a-directory")

    def test_linux_arm64_package_uses_same_management_protocol(self):
        files = [("bin/intdemo-trainer", b"linux-trainer")]
        manifest = self.factory.manifest(
            files,
            platform="linux-aarch64",
            entrypoint="bin/intdemo-trainer",
        )
        package = self.package(
            "linux.inttrainer",
            files=files,
            manifest=manifest,
        )
        manager = TrainerComponentManager(
            data_dir=self.root / "linux-data",
            platform_key="linux-aarch64",
            current_version="1.1.0",
            runner=self.successful_runner,
        )

        self.assertEqual(manager.install(package).status.code, STATUS_AVAILABLE)
        command = manager.command_for(
            self.root / "samples.zip",
            "click",
            self.root / "linux-output",
        )
        self.assertEqual(command[1], "train")
        self.assertIn("click", command)
        self.assertTrue(manager.active_executable().is_file())

    def test_install_does_not_require_a_trust_file(self):
        manager = TrainerComponentManager(
            data_dir=self.root / "data",
            platform_key="windows-x86_64",
            current_version="1.1.0",
            runner=self.successful_runner,
        )
        self.assertEqual(manager.install(self.package()).status.code, STATUS_AVAILABLE)
        self.assertGreater(manager.uninstall(), 0)
        self.assertEqual(manager.status().code, STATUS_NOT_INSTALLED)

    def test_accepts_valid_unsigned_manifest(self):
        files = [("bin/intdemo-trainer.exe", b"trainer")]
        manifest = self.factory.manifest(files)
        manifest["version"] = "1.0.1"
        result = self.manager().install(
            self.package(manifest=manifest, files=files)
        )
        self.assertEqual(result.status.version, "1.0.1")

    def test_rejects_tampered_file(self):
        declared = [("bin/intdemo-trainer.exe", b"trainer")]
        manifest = self.factory.manifest(declared)
        actual = [("bin/intdemo-trainer.exe", b"tampered")]
        with self.assertRaises(TrainerPackageError):
            self.manager().install(self.package(manifest=manifest, files=actual))

    def test_rejects_wrong_platform(self):
        files = [("bin/intdemo-trainer", b"trainer")]
        manifest = self.factory.manifest(
            files,
            platform="linux-aarch64",
            entrypoint="bin/intdemo-trainer",
        )
        with self.assertRaises(TrainerCompatibilityError):
            self.manager().install(self.package(manifest=manifest, files=files))

    def test_rejects_incompatible_client_version(self):
        files = [("bin/intdemo-trainer.exe", b"trainer")]
        manifest = self.factory.manifest(files, min_client_version="1.2.0")
        with self.assertRaises(TrainerCompatibilityError):
            self.manager().install(self.package(manifest=manifest, files=files))

    def test_rejects_backslash_traversal(self):
        files = [("bin/intdemo-trainer.exe", b"trainer")]
        manifest = self.factory.manifest(files)
        malicious_members = [
            (
                "manifest.json",
                json.dumps(manifest, separators=(",", ":")).encode(),
            ),
            ("bin/intdemo-trainer.exe", b"trainer"),
            ("..\\outside.dll", b"evil"),
        ]
        with self.assertRaises(TrainerPackageError):
            self.manager().install(
                self.factory.package(
                    self.root / "traversal.inttrainer",
                    members=malicious_members,
                )
            )
        self.assertFalse((self.root / "outside.dll").exists())

    def test_rejects_duplicate_normalized_paths(self):
        files = [("bin/intdemo-trainer.exe", b"trainer")]
        manifest = self.factory.manifest(files)
        members = [
            ("manifest.json", json.dumps(manifest).encode()),
            ("bin/intdemo-trainer.exe", b"trainer"),
            ("bin\\INTDEMO-TRAINER.exe", b"trainer"),
        ]
        with self.assertRaisesRegex(TrainerPackageError, "重复"):
            self.manager().install(
                self.factory.package(
                    self.root / "duplicate.inttrainer",
                    members=members,
                )
            )

    def test_rejects_symlink_member(self):
        files = [("bin/intdemo-trainer.exe", b"trainer")]
        manifest = self.factory.manifest(files)
        package = self.root / "symlink.inttrainer"
        with zipfile.ZipFile(package, "w") as archive:
            archive.writestr("manifest.json", json.dumps(manifest))
            info = zipfile.ZipInfo("bin/intdemo-trainer.exe")
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, b"target")
        with self.assertRaisesRegex(TrainerPackageError, "符号链接"):
            self.manager().install(package)

    def test_rejects_unlisted_extra_file(self):
        with self.assertRaisesRegex(TrainerPackageError, "未登记"):
            self.manager().install(
                self.package(extra=[("bin/injected.dll", b"evil")])
            )

    def test_rejects_size_mismatch_before_extraction(self):
        files = [("bin/intdemo-trainer.exe", b"trainer")]
        manifest = self.factory.manifest(files)
        manifest["files"][0]["size"] += 1
        with self.assertRaisesRegex(TrainerPackageError, "大小"):
            self.manager().install(self.package(files=files, manifest=manifest))

    def test_failed_self_test_keeps_previous_active_version(self):
        manager = self.manager()
        manager.install(self.package("one.inttrainer"))

        files = [("bin/intdemo-trainer.exe", b"trainer-v2")]
        manifest = self.factory.manifest(files, version="2.0.0")
        package = self.package("two.inttrainer", files=files, manifest=manifest)

        def failed_runner(command, **_kwargs):
            return subprocess.CompletedProcess(command, 9, stdout="", stderr="boom")

        manager.runner = failed_runner
        with self.assertRaises(TrainerSelfTestError):
            manager.install(package)
        self.assertEqual(manager.status().version, "1.0.0")

    def test_successful_upgrade_removes_inactive_version(self):
        manager = self.manager()
        manager.install(self.package("one.inttrainer"))
        files = [("bin/intdemo-trainer.exe", b"trainer-v2")]
        manifest = self.factory.manifest(files, version="2.0.0")
        manager.install(
            self.package("two.inttrainer", files=files, manifest=manifest)
        )

        self.assertEqual(manager.status().version, "2.0.0")
        versions = list(manager.versions_root.iterdir())
        self.assertEqual(len(versions), 1)

    def test_successive_upgrades_keep_only_the_active_version(self):
        manager = self.manager()
        manager.install(self.package("one.inttrainer"))

        for version in ("2.0.0", "3.0.0"):
            files = [("bin/intdemo-trainer.exe", f"trainer-{version}".encode())]
            manifest = self.factory.manifest(files, version=version)
            result = manager.install(
                self.package(
                    f"{version}.inttrainer",
                    files=files,
                    manifest=manifest,
                )
            )

        self.assertEqual(manager.status().version, "3.0.0")
        versions = [
            path
            for path in manager.versions_root.iterdir()
            if path.is_dir() and not path.is_symlink()
        ]
        self.assertEqual(len(versions), 1)
        expected_size = sum(
            path.stat().st_size
            for path in manager.component_root.rglob("*")
            if path.is_file() and not path.is_symlink()
        )
        self.assertEqual(result.status.installed_size, expected_size)

    def test_status_cleans_interrupted_install_and_orphan_version(self):
        manager = self.manager()
        manager.install(self.package())
        active_name = manager.active_executable().parents[1].name
        installing = manager.component_root / ".installing-AbCdEf12"
        installing.mkdir()
        (installing / "partial.bin").write_bytes(b"partial")
        orphan = (
            manager.versions_root
            / "2.0.0-windows-x86_64-0123456789ab-deadbeef"
        )
        orphan.mkdir()
        (orphan / "orphan.bin").write_bytes(b"orphan")

        status = manager.status(verify_files=False)

        self.assertEqual(status.code, STATUS_AVAILABLE)
        self.assertFalse(status.maintenance_required)
        self.assertFalse(installing.exists())
        self.assertFalse(orphan.exists())
        self.assertEqual(
            [path.name for path in manager.versions_root.iterdir()],
            [active_name],
        )

    def test_busy_status_reports_residual_without_mutating_it(self):
        manager = self.manager()
        manager.install(self.package())
        installing = manager.component_root / ".installing-AbCdEf12"
        installing.mkdir()
        (installing / "partial.bin").write_bytes(b"partial")

        with manager.training_lock():
            status = manager.status(verify_files=False)

        self.assertTrue(installing.exists())
        self.assertTrue(status.maintenance_required)
        self.assertTrue(status.cleanup_available)
        self.assertIn("本次未清理", status.maintenance_message)
        self.assertGreaterEqual(status.installed_size, len(b"partial"))
        self.assertFalse(manager.status(verify_files=False).maintenance_required)
        self.assertFalse(installing.exists())

    def test_status_lock_error_without_residual_does_not_request_maintenance(self):
        manager = self.manager()
        manager.install(self.package())

        with patch.object(
            manager,
            "_operation_lock",
            side_effect=PermissionError("lock directory is read-only"),
        ):
            status = manager.status(verify_files=False)

        self.assertEqual(status.code, STATUS_AVAILABLE)
        self.assertFalse(status.maintenance_required)

    def test_cleanup_failure_is_reported_and_retried(self):
        manager = self.manager()
        manager.install(self.package())
        installing = manager.component_root / ".installing-AbCdEf12"
        installing.mkdir()
        (installing / "partial.bin").write_bytes(b"partial")
        real_rmtree = shutil.rmtree

        def deny_target(path, *args, **kwargs):
            if Path(path) == installing:
                raise PermissionError("directory busy")
            return real_rmtree(path, *args, **kwargs)

        with patch(
            "integrated_client.trainer_component.shutil.rmtree",
            side_effect=deny_target,
        ):
            status = manager.status(verify_files=False)

        self.assertTrue(installing.exists())
        self.assertTrue(status.maintenance_required)
        self.assertTrue(status.cleanup_available)
        self.assertIn("directory busy", status.maintenance_message)
        self.assertFalse(manager.status(verify_files=False).maintenance_required)
        self.assertFalse(installing.exists())

    def test_status_recovers_confirmed_interrupted_uninstall(self):
        manager = self.manager()
        manager.install(self.package())
        released_size = manager._component_storage_size()
        token = "a" * 32
        removal = manager.component_root.parent / f".trainer-removing-{token}"
        marker = removal.with_suffix(".json")
        manager._atomic_write_json(
            marker,
            {
                "schema_version": 1,
                "token": token,
                "component": "trainer",
                "directory": removal.name,
            },
        )
        os.replace(manager.component_root, removal)

        status = manager.status(verify_files=False)

        self.assertEqual(status.code, STATUS_NOT_INSTALLED)
        self.assertFalse(status.maintenance_required)
        self.assertEqual(status.installed_size, 0)
        self.assertFalse(removal.exists())
        self.assertFalse(marker.exists())
        self.assertGreater(released_size, 0)

    def test_confirmed_uninstall_residual_survives_busy_status_then_cleans(self):
        manager = self.manager()
        manager.install(self.package())
        token = "c" * 32
        removal = manager.component_root.parent / f".trainer-removing-{token}"
        marker = removal.with_suffix(".json")
        manager._atomic_write_json(
            marker,
            {
                "schema_version": 1,
                "token": token,
                "component": "trainer",
                "directory": removal.name,
            },
        )
        os.replace(manager.component_root, removal)

        with manager.training_lock():
            status = manager.status(verify_files=False)

        self.assertEqual(status.code, STATUS_NOT_INSTALLED)
        self.assertTrue(status.maintenance_required)
        self.assertTrue(status.cleanup_available)
        self.assertGreater(status.installed_size, 0)
        self.assertTrue(removal.exists())
        self.assertTrue(marker.exists())

        released = manager.uninstall()
        self.assertGreater(released, 0)
        self.assertFalse(removal.exists())
        self.assertFalse(marker.exists())

    def test_similar_unconfirmed_removal_directory_is_never_deleted(self):
        manager = self.manager()
        components = manager.component_root.parent
        components.mkdir(parents=True)
        token = "b" * 32
        unconfirmed = components / f".trainer-removing-{token}"
        unconfirmed.mkdir()
        sentinel = unconfirmed / "keep.bin"
        sentinel.write_bytes(b"keep")

        status = manager.status(verify_files=False)

        self.assertEqual(status.code, STATUS_NOT_INSTALLED)
        self.assertFalse(status.maintenance_required)
        self.assertEqual(status.installed_size, 0)
        self.assertEqual(sentinel.read_bytes(), b"keep")

    def test_invalid_active_record_prevents_orphan_version_cleanup(self):
        manager = self.manager()
        manager.versions_root.mkdir(parents=True)
        version = (
            manager.versions_root
            / "1.0.0-windows-x86_64-0123456789ab-deadbeef"
        )
        version.mkdir()
        sentinel = version / "keep.bin"
        sentinel.write_bytes(b"keep")
        manager.active_path.write_text("{}", encoding="utf-8")

        status = manager.status(verify_files=False)

        self.assertEqual(status.code, STATUS_DAMAGED)
        self.assertTrue(status.maintenance_required)
        self.assertFalse(status.cleanup_available)
        self.assertEqual(sentinel.read_bytes(), b"keep")

    def test_training_lock_blocks_install_uninstall_and_self_test(self):
        manager = self.manager()
        package = self.package()
        manager.install(package)
        with manager.training_lock():
            with self.assertRaises(TrainerBusyError):
                manager.install(package)
            with self.assertRaises(TrainerBusyError):
                manager.uninstall()
            with self.assertRaises(TrainerBusyError):
                manager.self_test()
        manager.uninstall()

    def test_cross_process_lock_survives_live_pid_and_recovers_after_crash(self):
        manager = self.manager()
        package = self.package()
        manager.install(package)
        ready_path = self.root / "lock-holder-ready"
        crash_path = self.root / "crash-lock-holder"
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                _LOCK_HOLDER_SCRIPT,
                str(manager.data_dir),
                str(ready_path),
                str(crash_path),
            ],
            cwd=Path(__file__).resolve().parents[1],
        )
        try:
            self.assertTrue(_wait_for_file(ready_path), "锁持有进程未按时就绪")
            self.assertIsNone(process.poll())

            with self.assertRaises(TrainerBusyError):
                manager.install(package)
            with self.assertRaises(TrainerBusyError):
                manager.self_test()
            with self.assertRaises(TrainerBusyError):
                manager.uninstall()
            with self.assertRaises(TrainerBusyError), manager.training_lock():
                self.fail("不应抢占仍存活进程的训练锁")
        finally:
            crash_path.touch()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)

        self.assertTrue(manager.training_lock_path.is_file())
        stale_metadata = json.loads(
            manager.training_lock_path.read_text(encoding="utf-8")
        )
        self.assertEqual(
            stale_metadata["pid"],
            int(ready_path.read_text(encoding="ascii")),
        )
        self.assertEqual(stale_metadata["operation"], "training")
        with manager.training_lock():
            pass
        recovered_metadata = json.loads(
            manager.training_lock_path.read_text(encoding="utf-8")
        )
        self.assertEqual(recovered_metadata["pid"], os.getpid())
        self.assertEqual(recovered_metadata["operation"], "training")
        self.assertEqual(manager.self_test().code, STATUS_AVAILABLE)

    def test_install_holds_lock_for_the_complete_lifecycle_operation(self):
        started = threading.Event()
        release = threading.Event()
        errors = []

        def blocking_runner(command, **kwargs):
            started.set()
            if not release.wait(10):
                raise TimeoutError("测试未释放安装自检")
            return self.successful_runner(command, **kwargs)

        manager = self.manager(runner=blocking_runner)
        package = self.package()

        def install():
            try:
                manager.install(package)
            except BaseException as exc:  # noqa: BLE001 - thread test boundary
                errors.append(exc)

        worker = threading.Thread(target=install)
        worker.start()
        try:
            self.assertTrue(started.wait(10), "安装自检未按时开始")
            with self.assertRaises(TrainerBusyError), manager.training_lock():
                self.fail("安装过程中不应启动强化训练")
            with self.assertRaises(TrainerBusyError):
                manager.uninstall()
        finally:
            release.set()
            worker.join(timeout=10)

        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(manager.status().code, STATUS_AVAILABLE)

    def test_self_test_holds_lock_for_the_complete_lifecycle_operation(self):
        manager = self.manager()
        manager.install(self.package())
        started = threading.Event()
        release = threading.Event()
        errors = []

        def blocking_runner(command, **kwargs):
            started.set()
            if not release.wait(10):
                raise TimeoutError("测试未释放组件自检")
            return self.successful_runner(command, **kwargs)

        manager.runner = blocking_runner

        def self_test():
            try:
                manager.self_test()
            except BaseException as exc:  # noqa: BLE001 - thread test boundary
                errors.append(exc)

        worker = threading.Thread(target=self_test)
        worker.start()
        try:
            self.assertTrue(started.wait(10), "组件自检未按时开始")
            with self.assertRaises(TrainerBusyError), manager.training_lock():
                self.fail("组件自检过程中不应启动强化训练")
            with self.assertRaises(TrainerBusyError):
                manager.uninstall()
        finally:
            release.set()
            worker.join(timeout=10)

        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(manager.status().code, STATUS_AVAILABLE)

    def test_uninstall_holds_lock_for_the_complete_lifecycle_operation(self):
        manager = self.manager()
        manager.install(self.package())
        started = threading.Event()
        release = threading.Event()
        errors = []
        real_rmtree = shutil.rmtree

        def blocking_rmtree(path, *args, **kwargs):
            started.set()
            if not release.wait(10):
                raise TimeoutError("测试未释放组件卸载")
            return real_rmtree(path, *args, **kwargs)

        def uninstall():
            try:
                manager.uninstall()
            except BaseException as exc:  # noqa: BLE001 - thread test boundary
                errors.append(exc)

        with patch(
            "integrated_client.trainer_component.shutil.rmtree",
            side_effect=blocking_rmtree,
        ):
            worker = threading.Thread(target=uninstall)
            worker.start()
            try:
                self.assertTrue(started.wait(10), "组件卸载未按时开始")
                with self.assertRaises(
                    TrainerBusyError
                ), manager.training_lock():
                    self.fail("组件卸载过程中不应启动强化训练")
            finally:
                release.set()
                worker.join(timeout=10)

        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(manager.status().code, STATUS_NOT_INSTALLED)

    def test_uninstall_failure_leaves_confirmed_residual_for_retry(self):
        manager = self.manager()
        manager.install(self.package())
        real_rmtree = shutil.rmtree
        failed = {"value": False}

        def fail_once(path, *args, **kwargs):
            if (
                not failed["value"]
                and Path(path).name.startswith(".trainer-removing-")
            ):
                failed["value"] = True
                raise PermissionError("component executable is busy")
            return real_rmtree(path, *args, **kwargs)

        with patch(
            "integrated_client.trainer_component.shutil.rmtree",
            side_effect=fail_once,
        ):
            with self.assertRaisesRegex(PermissionError, "busy"):
                manager.uninstall()

        self.assertFalse(manager.component_root.exists())
        status = manager.status(verify_files=False)
        self.assertEqual(status.code, STATUS_NOT_INSTALLED)
        self.assertFalse(status.maintenance_required)
        residuals = list(manager.component_root.parent.glob(".trainer-removing-*"))
        self.assertEqual(residuals, [])

    def test_uninstall_preserves_samples_and_models_outside_component_root(self):
        manager = self.manager()
        manager.install(self.package())
        sample = manager.data_dir / "captcha_samples" / "one.png"
        model = manager.data_dir / "captcha_models" / "candidate.onnx"
        sample.parent.mkdir(parents=True)
        model.parent.mkdir(parents=True)
        sample.write_bytes(b"sample")
        model.write_bytes(b"model")

        manager.uninstall()

        self.assertEqual(sample.read_bytes(), b"sample")
        self.assertEqual(model.read_bytes(), b"model")

    def test_damaged_component_is_reported(self):
        manager = self.manager()
        manager.install(self.package())
        manager.active_executable().write_bytes(b"corrupt")
        status = manager.status()
        self.assertEqual(status.code, STATUS_DAMAGED)
        self.assertTrue(status.message)

if __name__ == "__main__":
    unittest.main()

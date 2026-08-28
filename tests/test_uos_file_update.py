from __future__ import annotations

import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

from integrated_client.online.config import OnlineConfig
from integrated_client.online.uos_file_update import (
    UOS_FILE_LAYOUT,
    UOS_FILE_UPDATE_FORMAT,
    UosFileStore,
    UosFileUpdateError,
    create_file_archive,
    read_file_manifest,
    validate_file_package_root,
    write_file_layout,
)
from integrated_client.online.uos_layers import write_layout
from integrated_client.online.update import UpdateClient


class UosFileUpdateTests(unittest.TestCase):
    @staticmethod
    def _package(
        root: Path,
        version: str,
        *,
        app_text: str,
        include_old: bool = False,
        include_asset: bool = False,
    ) -> Path:
        package = root / version
        (package / "app/_internal/assets").mkdir(parents=True)
        (package / "browser").mkdir()
        (package / "certs").mkdir()
        launcher = package / "intdemo-client"
        launcher.write_text("stable-launcher", encoding="utf-8")
        launcher.chmod(0o755)
        (package / "client-online.json").write_text(
            '{"base_url":"https://updates.example.com"}',
            encoding="utf-8",
        )
        executable = package / "app/intdemo-client"
        executable.write_text(app_text, encoding="utf-8")
        executable.chmod(0o755)
        (package / "app/_internal/runtime.bin").write_bytes(b"stable-runtime")
        if include_old:
            (package / "app/_internal/old.bin").write_bytes(b"remove-me")
        if include_asset:
            (package / "app/_internal/assets/new-feature.svg").write_text(
                "<svg/>",
                encoding="utf-8",
            )
        browser = package / "browser/chrome"
        browser.write_bytes(b"stable-browser")
        browser.chmod(0o755)
        write_layout(package, version)
        write_file_layout(package, version)
        return package

    def test_archive_contains_only_changed_files_and_records_deletions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._package(
                root,
                "1.2.3",
                app_text="old-app",
                include_old=True,
            )
            target = self._package(
                root,
                "1.2.4",
                app_text="new-app",
                include_asset=True,
            )
            archive = root / "IntDemo-UOS-arm64-Files-1.2.3-to-1.2.4.intlayer"

            manifest = create_file_archive(
                source,
                target,
                archive,
                from_version="1.2.3",
                target_version="1.2.4",
            )

            self.assertEqual(manifest["format"], UOS_FILE_UPDATE_FORMAT)
            self.assertEqual(
                manifest["changed_paths"],
                ["app/_internal/assets/new-feature.svg", "app/intdemo-client"],
            )
            self.assertEqual(manifest["deleted_paths"], ["app/_internal/old.bin"])
            with zipfile.ZipFile(archive) as package:
                self.assertEqual(
                    set(package.namelist()),
                    {
                        "manifest.json",
                        "payload/app/_internal/assets/new-feature.svg",
                        "payload/app/intdemo-client",
                    },
                )

            store = UosFileStore(
                root / "data",
                current_version="1.2.3",
                package_root=source,
            )
            store.stage(
                archive,
                from_version="1.2.3",
                target_version="1.2.4",
                source_layout_sha256=manifest["source_layout_sha256"],
                target_layout_sha256=manifest["target_layout"]["layout_sha256"],
            )
            installed = store.versions_dir / "1.2.4"
            validate_file_package_root(installed, manifest["target_layout"])
            self.assertFalse((installed / "app/_internal/old.bin").exists())
            self.assertEqual(
                (installed / "app/_internal/assets/new-feature.svg").read_text(
                    encoding="utf-8"
                ),
                "<svg/>",
            )
            self.assertEqual(
                (installed / "browser/chrome").read_bytes(),
                b"stable-browser",
            )

    def test_tampered_source_baseline_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._package(root, "1.2.3", app_text="old-app")
            target = self._package(root, "1.2.4", app_text="new-app")
            archive = root / "IntDemo-UOS-arm64-Files-1.2.3-to-1.2.4.intlayer"
            manifest = create_file_archive(
                source,
                target,
                archive,
                from_version="1.2.3",
                target_version="1.2.4",
            )
            (source / "app/_internal/runtime.bin").write_bytes(b"tampered")
            store = UosFileStore(
                root / "data",
                current_version="1.2.3",
                package_root=source,
            )

            with self.assertRaises(UosFileUpdateError):
                store.stage(
                    archive,
                    from_version="1.2.3",
                    target_version="1.2.4",
                    source_layout_sha256=manifest["source_layout_sha256"],
                    target_layout_sha256=manifest["target_layout"]["layout_sha256"],
                )

    def test_root_owned_seed_can_be_reused_without_copying_unchanged_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._package(root, "1.2.3", app_text="old-app")
            target = self._package(root, "1.2.4", app_text="new-app")
            archive = root / "IntDemo-UOS-arm64-Files-1.2.3-to-1.2.4.intlayer"
            manifest = create_file_archive(
                source,
                target,
                archive,
                from_version="1.2.3",
                target_version="1.2.4",
            )
            store = UosFileStore(
                root / "data",
                current_version="1.2.3",
                package_root=source,
            )

            with patch(
                "integrated_client.online.uos_file_update.os.link",
                side_effect=PermissionError,
            ):
                store.stage(
                    archive,
                    from_version="1.2.3",
                    target_version="1.2.4",
                    source_layout_sha256=manifest["source_layout_sha256"],
                    target_layout_sha256=manifest["target_layout"]["layout_sha256"],
                )

            installed = store.versions_dir / "1.2.4"
            if os.name == "posix":
                self.assertTrue((installed / "browser/chrome").is_symlink())
            validate_file_package_root(installed, manifest["target_layout"])
            store._layer_store._write_text_atomic(
                store.pending_attempted_path,
                "1.2.4",
            )
            self.assertTrue(store._layer_store.confirm_pending("1.2.4"))

    def test_manifest_rejects_wrong_target_layout_hash(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._package(root, "1.2.3", app_text="old-app")
            target = self._package(root, "1.2.4", app_text="new-app")
            archive = root / "IntDemo-UOS-arm64-Files-1.2.3-to-1.2.4.intlayer"
            create_file_archive(
                source,
                target,
                archive,
                from_version="1.2.3",
                target_version="1.2.4",
            )

            with self.assertRaises(UosFileUpdateError):
                read_file_manifest(archive, target_layout_sha256="0" * 64)

    def test_update_client_prefers_file_update_with_matching_baseline(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._package(root, "1.2.3", app_text="old-app")
            target = self._package(root, "1.2.4", app_text="new-app")
            archive = root / "IntDemo-UOS-arm64-Files-1.2.3-to-1.2.4.intlayer"
            manifest = create_file_archive(
                source,
                target,
                archive,
                from_version="1.2.3",
                target_version="1.2.4",
            )
            response = Mock(status_code=200)
            response.raise_for_status.return_value = None
            response.json.return_value = {
                "schema_version": 1,
                "channel": "test",
                "version": "1.2.4",
                "platforms": {
                    "linux-aarch64": {
                        "full": {
                            "installer_path": (
                                "/updates/files/IntDemo-UOS-arm64-1.2.4.deb"
                            ),
                            "sha256": "a" * 64,
                            "size": 1000,
                        },
                        "file_updates": [
                            {
                                "format": UOS_FILE_UPDATE_FORMAT,
                                "from_version": "1.2.3",
                                "source_layout_sha256": manifest[
                                    "source_layout_sha256"
                                ],
                                "target_layout_sha256": manifest[
                                    "target_layout"
                                ]["layout_sha256"],
                                "installer_path": f"/updates/files/{archive.name}",
                                "sha256": "b" * 64,
                                "size": archive.stat().st_size,
                            }
                        ],
                    }
                },
            }
            session = Mock()
            session.headers = {}
            session.get.return_value = response
            client = UpdateClient(
                OnlineConfig(base_url="https://updates.example.com"),
                session=session,
                current_version="1.2.3",
                platform_key="linux-aarch64",
            )
            client.uos_file_store = UosFileStore(
                root / "data",
                current_version="1.2.3",
                package_root=source,
            )

            update = client.check()

            self.assertIsNotNone(update)
            self.assertTrue(update.is_file_update)
            self.assertTrue(update.is_layered)
            self.assertEqual(update.installer_name, archive.name)
            self.assertIn(UOS_FILE_UPDATE_FORMAT, session.headers["X-IntDemo-Update-Capabilities"])

            (source / UOS_FILE_LAYOUT).unlink()
            fallback = client.check()
            self.assertEqual(fallback.package_kind, "full")

    def test_successful_confirmation_cleans_old_versions_and_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._package(root, "1.2.3", app_text="old-app")
            target = self._package(root, "1.2.4", app_text="new-app")
            archive = root / "IntDemo-UOS-arm64-Files-1.2.3-to-1.2.4.intlayer"
            manifest = create_file_archive(
                source,
                target,
                archive,
                from_version="1.2.3",
                target_version="1.2.4",
            )
            store = UosFileStore(
                root / "data",
                current_version="1.2.3",
                package_root=source,
            )
            cached_archive = store.archive_path(
                archive.name,
                from_version="1.2.3",
                target_version="1.2.4",
            )
            cached_archive.write_bytes(archive.read_bytes())
            store.stage(
                cached_archive,
                from_version="1.2.3",
                target_version="1.2.4",
                source_layout_sha256=manifest["source_layout_sha256"],
                target_layout_sha256=manifest["target_layout"]["layout_sha256"],
            )
            stale = store.versions_dir / "1.2.1"
            stale.mkdir()
            previous = store.versions_dir / "1.2.3"
            previous.mkdir()
            store._layer_store._write_text_atomic(store.current_version_path, "1.2.3")
            store._layer_store._write_text_atomic(store.pending_attempted_path, "1.2.4")

            self.assertTrue(store._layer_store.confirm_pending("1.2.4"))
            self.assertFalse(stale.exists())
            self.assertFalse(previous.exists())
            self.assertFalse(cached_archive.exists())


if __name__ == "__main__":
    unittest.main()

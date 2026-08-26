from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock

from integrated_client.online.config import OnlineConfig
from integrated_client.online.uos_layers import (
    UOS_LAYER_FORMAT,
    UOS_LAYER_LAYOUT,
    UosLayerError,
    UosLayerStore,
    create_layer_archive,
    read_layer_manifest,
    validate_package_root,
    write_layout,
)
from integrated_client.online.update import UpdateClient


class UosLayerTests(unittest.TestCase):
    @staticmethod
    def _package(root: Path, version: str, *, app_text: str) -> Path:
        package = root / version
        (package / "app/_internal").mkdir(parents=True)
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
        browser = package / "browser/chrome"
        browser.write_bytes(b"stable-browser")
        browser.chmod(0o755)
        write_layout(package, version)
        return package

    def test_app_only_archive_reuses_stable_layers_and_confirms(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._package(root, "1.2.2", app_text="old-app")
            target = self._package(root, "1.2.3", app_text="new-app")
            archive = root / "IntDemo-UOS-arm64-Layers-1.2.2-to-1.2.3.intlayer"

            manifest = create_layer_archive(
                source,
                target,
                archive,
                from_version="1.2.2",
                target_version="1.2.3",
            )

            self.assertEqual(manifest["format"], UOS_LAYER_FORMAT)
            self.assertEqual(manifest["changed_layers"], ["app"])
            with zipfile.ZipFile(archive) as package:
                self.assertEqual(
                    set(package.namelist()),
                    {"manifest.json", "payload/app/intdemo-client"},
                )

            data_dir = root / "data"
            store = UosLayerStore(
                data_dir,
                current_version="1.2.2",
                package_root=source,
            )
            prepared = store.stage(
                archive,
                from_version="1.2.2",
                target_version="1.2.3",
                source_layout_sha256=manifest["source_layout_sha256"],
                target_layout_sha256=manifest["target_layout"]["layout_sha256"],
            )

            self.assertEqual(prepared, archive.resolve())
            installed = store.versions_dir / "1.2.3"
            validate_package_root(installed, manifest["target_layout"])
            self.assertEqual(
                (installed / "app/intdemo-client").read_text(encoding="utf-8"),
                "new-app",
            )
            self.assertEqual(
                (installed / "app/_internal/runtime.bin").read_bytes(),
                b"stable-runtime",
            )
            self.assertEqual(
                store.pending_version_path.read_text(encoding="ascii").strip(),
                "1.2.3",
            )
            self.assertFalse(store.confirm_pending("1.2.3"))
            store._write_text_atomic(store.pending_attempted_path, "1.2.3")
            self.assertTrue(store.confirm_pending("1.2.3"))
            self.assertEqual(
                store.current_version_path.read_text(encoding="ascii").strip(),
                "1.2.3",
            )

    def test_source_layout_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._package(root, "1.2.2", app_text="old-app")
            target = self._package(root, "1.2.3", app_text="new-app")
            archive = root / "IntDemo-UOS-arm64-Layers-1.2.2-to-1.2.3.intlayer"
            manifest = create_layer_archive(
                source,
                target,
                archive,
                from_version="1.2.2",
                target_version="1.2.3",
            )
            (source / "app/_internal/runtime.bin").write_bytes(b"tampered")
            store = UosLayerStore(
                root / "data",
                current_version="1.2.2",
                package_root=source,
            )

            with self.assertRaises(UosLayerError):
                store.stage(
                    archive,
                    from_version="1.2.2",
                    target_version="1.2.3",
                    source_layout_sha256=manifest["source_layout_sha256"],
                    target_layout_sha256=manifest["target_layout"]["layout_sha256"],
                )

    def test_manifest_rejects_wrong_target_layout_hash(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._package(root, "1.2.2", app_text="old-app")
            target = self._package(root, "1.2.3", app_text="new-app")
            archive = root / "IntDemo-UOS-arm64-Layers-1.2.2-to-1.2.3.intlayer"
            create_layer_archive(
                source,
                target,
                archive,
                from_version="1.2.2",
                target_version="1.2.3",
            )

            with self.assertRaises(UosLayerError):
                read_layer_manifest(
                    archive,
                    target_layout_sha256="0" * 64,
                )

    def test_layout_self_hash_detects_edits(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = self._package(root, "1.2.2", app_text="old-app")
            layout_path = package / UOS_LAYER_LAYOUT
            payload = json.loads(layout_path.read_text(encoding="utf-8"))
            payload["layers"]["app"]["size"] += 1
            layout_path.write_text(json.dumps(payload), encoding="utf-8")
            store = UosLayerStore(
                root / "data",
                current_version="1.2.2",
                package_root=package,
            )
            self.assertIsNone(
                store.source_root(
                    version="1.2.2",
                    layout_sha256=payload["layout_sha256"],
                )
            )

    def test_update_client_selects_layer_only_with_matching_local_layout(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._package(root, "1.2.2", app_text="old-app")
            target = self._package(root, "1.2.3", app_text="new-app")
            archive = root / "IntDemo-UOS-arm64-Layers-1.2.2-to-1.2.3.intlayer"
            manifest = create_layer_archive(
                source,
                target,
                archive,
                from_version="1.2.2",
                target_version="1.2.3",
            )
            response = Mock(status_code=200)
            response.raise_for_status.return_value = None
            response.json.return_value = {
                "schema_version": 1,
                "channel": "test",
                "version": "1.2.3",
                "platforms": {
                    "linux-aarch64": {
                        "full": {
                            "installer_path": (
                                "/updates/files/IntDemo-UOS-arm64-1.2.3.deb"
                            ),
                            "sha256": "a" * 64,
                            "size": 1000,
                        },
                        "layered_updates": [
                            {
                                "format": UOS_LAYER_FORMAT,
                                "from_version": "1.2.2",
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
                current_version="1.2.2",
                platform_key="linux-aarch64",
            )
            client.uos_layer_store = UosLayerStore(
                root / "data",
                current_version="1.2.2",
                package_root=source,
            )

            update = client.check()

            self.assertIsNotNone(update)
            self.assertTrue(update.is_layered)
            self.assertEqual(update.installer_name, archive.name)
            self.assertEqual(update.full_installer_name, "IntDemo-UOS-arm64-1.2.3.deb")

            (source / UOS_LAYER_LAYOUT).unlink()
            fallback = client.check()
            self.assertFalse(fallback.is_layered)
            self.assertEqual(fallback.package_kind, "full")

    def test_bootstrap_change_requires_a_full_deb(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._package(root, "1.2.2", app_text="old-app")
            target = self._package(root, "1.2.3", app_text="new-app")
            (target / "client-online.json").write_text(
                '{"base_url":"https://new.example.com"}',
                encoding="utf-8",
            )
            write_layout(target, "1.2.3")
            archive = (
                root / "IntDemo-UOS-arm64-Layers-1.2.2-to-1.2.3.intlayer"
            )

            with self.assertRaisesRegex(UosLayerError, "完整 DEB"):
                create_layer_archive(
                    source,
                    target,
                    archive,
                    from_version="1.2.2",
                    target_version="1.2.3",
                )


if __name__ == "__main__":
    unittest.main()

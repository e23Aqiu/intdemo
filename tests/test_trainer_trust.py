from __future__ import annotations

import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from integrated_client.trainer_component import TrainerSignatureError
from integrated_client.trainer_trust import (
    TRAINER_TRUST_ENVIRONMENT,
    TrainerTrustConfigurationError,
    create_trainer_component_manager,
    load_trainer_trusted_public_keys,
    trainer_trust_candidates,
)


def _document(*, key_id="trainer-release-1", public_key=b"k" * 32):
    return {
        "schema_version": 1,
        "keys": {key_id: base64.b64encode(public_key).decode("ascii")},
    }


def _write(path: Path, payload) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class TrainerTrustTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_explicit_environment_file_loads_raw_ed25519_key(self):
        trust_file = _write(self.root / "trainer-trust.json", _document())
        with patch.dict(
            os.environ,
            {TRAINER_TRUST_ENVIRONMENT: str(trust_file)},
            clear=True,
        ):
            keys = load_trainer_trusted_public_keys()

        self.assertEqual(keys, {"trainer-release-1": b"k" * 32})

    def test_explicit_missing_or_invalid_file_never_falls_back(self):
        package_file = _write(self.root / "package" / "trainer-trust.json", _document())
        missing = self.root / "missing.json"
        with patch.dict(
            os.environ,
            {TRAINER_TRUST_ENVIRONMENT: str(missing)},
            clear=True,
        ), patch(
            "integrated_client.trainer_trust.trainer_trust_candidates",
            return_value=(package_file,),
        ), self.assertRaises(TrainerTrustConfigurationError):
            load_trainer_trusted_public_keys()

        invalid = self.root / "invalid.json"
        invalid.write_text('{"schema_version":1,"keys":{},"extra":true}', "utf-8")
        with self.assertRaises(TrainerTrustConfigurationError):
            load_trainer_trusted_public_keys(invalid)

    def test_schema_key_ids_base64_and_key_length_are_strict(self):
        cases = (
            {"schema_version": True, "keys": {"key": "A" * 44}},
            {"schema_version": 2, "keys": {"key": "A" * 44}},
            {"schema_version": 1, "keys": []},
            {"schema_version": 1, "keys": {"../key": "A" * 44}},
            {"schema_version": 1, "keys": {"key": "not base64!"}},
            _document(public_key=b"short"),
        )
        for index, payload in enumerate(cases):
            with self.subTest(index=index), self.assertRaises(
                TrainerTrustConfigurationError
            ):
                load_trainer_trusted_public_keys(
                    _write(self.root / f"invalid-{index}.json", payload)
                )

    def test_duplicate_json_keys_are_rejected(self):
        trust_file = self.root / "duplicate.json"
        trust_file.write_text(
            '{"schema_version":1,"keys":{"a":"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=","a":"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="}}',
            encoding="utf-8",
        )
        with self.assertRaises(TrainerTrustConfigurationError):
            load_trainer_trusted_public_keys(trust_file)

    def test_deeply_nested_json_is_reported_as_invalid_configuration(self):
        trust_file = self.root / "deeply-nested.json"
        trust_file.write_text("[" * 1_500 + "0" + "]" * 1_500, encoding="utf-8")

        with self.assertRaises(TrainerTrustConfigurationError):
            load_trainer_trusted_public_keys(trust_file)

    def test_implicit_priority_is_executable_linux_config_then_package(self):
        executable = self.root / "bin" / "intdemo-client"
        xdg_root = self.root / "config"
        module_path = self.root / "package" / "trainer_trust.py"
        with (
            patch(
                "integrated_client.trainer_trust.sys.executable",
                str(executable),
            ),
            patch("integrated_client.trainer_trust.sys.platform", "linux"),
            patch.dict(
                os.environ,
                {"XDG_CONFIG_HOME": str(xdg_root)},
                clear=True,
            ),
            patch("integrated_client.trainer_trust.__file__", str(module_path)),
        ):
            candidates = trainer_trust_candidates()

        self.assertEqual(
            candidates,
            (
                executable.parent / "trainer-trust.json",
                xdg_root / "intdemo-client" / "trainer-trust.json",
                module_path.parent / "trainer-trust.json",
            ),
        )

    def test_frozen_uos_package_root_follows_user_config(self):
        executable = self.root / "package" / "app" / "intdemo-client"
        xdg_root = self.root / "config"
        module_path = self.root / "_internal" / "trainer_trust.py"
        with (
            patch(
                "integrated_client.trainer_trust.sys.executable",
                str(executable),
            ),
            patch("integrated_client.trainer_trust.sys.platform", "linux"),
            patch.dict(
                os.environ,
                {"XDG_CONFIG_HOME": str(xdg_root)},
                clear=True,
            ),
            patch("integrated_client.trainer_trust.__file__", str(module_path)),
        ):
            candidates = trainer_trust_candidates()

        self.assertEqual(candidates[0], executable.parent / "trainer-trust.json")
        self.assertEqual(
            candidates[1],
            xdg_root / "intdemo-client" / "trainer-trust.json",
        )
        self.assertEqual(
            candidates[2],
            executable.parent.parent / "trainer-trust.json",
        )

    def test_first_existing_implicit_file_is_authoritative(self):
        first = _write(self.root / "first.json", {"schema_version": 1, "keys": {}})
        second = _write(self.root / "second.json", _document())
        with patch.dict(os.environ, {}, clear=True), patch(
            "integrated_client.trainer_trust.trainer_trust_candidates",
            return_value=(first, second),
        ), self.assertRaises(TrainerTrustConfigurationError):
            load_trainer_trusted_public_keys()

    def test_missing_implicit_file_builds_manager_without_verifier(self):
        with patch.dict(os.environ, {}, clear=True), patch(
            "integrated_client.trainer_trust.trainer_trust_candidates",
            return_value=(self.root / "missing.json",),
        ):
            manager = create_trainer_component_manager(
                data_dir=self.root / "data",
                platform_key="windows-x86_64",
                current_version="1.1.0",
            )

        self.assertIsNone(manager.signature_verifier)
        self.assertEqual(manager.uninstall(), 0)
        with self.assertRaises(TrainerSignatureError):
            manager._verify_signature({})

    def test_factory_uses_configured_dedicated_verifier(self):
        trust_file = _write(self.root / "trainer-trust.json", _document())
        manager = create_trainer_component_manager(
            trust_file=trust_file,
            data_dir=self.root / "data",
            platform_key="windows-x86_64",
            current_version="1.1.0",
        )
        self.assertIsNotNone(manager.signature_verifier)

    def test_packaging_scripts_accept_and_copy_only_public_trust_file(self):
        repository = Path(__file__).resolve().parents[1]
        installer = (repository / "scripts/build-installer.ps1").read_text("utf-8")
        portable = (repository / "scripts/build-portable.ps1").read_text("utf-8")
        releases = (repository / "scripts/build-releases.ps1").read_text("utf-8")
        wrapper = (repository / "scripts/build-online-test.ps1").read_text("utf-8")
        for script in (installer, portable, releases, wrapper):
            self.assertIn("TrainerTrustFile", script)
            self.assertNotIn("private_key", script.casefold())
        self.assertIn('Join-Path $stage "trainer-trust.json"', installer)
        self.assertIn('Join-Path $stage "trainer-trust.json"', portable)
        self.assertIn(
            "$portableArguments.TrainerTrustFile = $TrainerTrustFile",
            releases,
        )
        self.assertIn(
            "$installerArguments.TrainerTrustFile = $TrainerTrustFile",
            releases,
        )
        self.assertIn("$arguments.TrainerTrustFile = $TrainerTrustFile", wrapper)

        ignore = (repository / ".gitignore").read_text("utf-8")
        self.assertIn("trainer-trust.json", ignore)
        example = repository / "packaging/trainer/trainer-trust.example.json"
        self.assertTrue(example.is_file())
        self.assertEqual(json.loads(example.read_text("utf-8"))["schema_version"], 1)


if __name__ == "__main__":
    unittest.main()

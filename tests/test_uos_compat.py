import base64
import os
import runpy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from integrated_client import browser, platform_support
from integrated_client.online.config import OnlineConfig
from integrated_client.online.secure import (
    SecretServiceProtector,
    SecureStorageUnavailable,
)


class UosCompatibilityTests(unittest.TestCase):
    def test_explicit_chromium_path_has_priority(self):
        with tempfile.TemporaryDirectory() as temporary:
            executable = Path(temporary) / "uos-browser"
            executable.write_bytes(b"browser")
            with patch.dict(
                os.environ,
                {"INTDEMO_CHROMIUM_PATH": str(executable)},
                clear=False,
            ):
                self.assertEqual(
                    browser.get_builtin_chromium_path(),
                    str(executable.resolve()),
                )

    def test_invalid_explicit_chromium_path_is_actionable(self):
        with patch.dict(
            os.environ,
            {"INTDEMO_CHROMIUM_PATH": "/missing/uos-browser"},
            clear=False,
        ):
            with self.assertRaisesRegex(RuntimeError, "INTDEMO_CHROMIUM_PATH"):
                browser.get_builtin_chromium_path()

    def test_linux_prefers_system_chromium_over_playwright_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            executable = Path(temporary) / "chromium"
            executable.write_bytes(b"browser")
            playwright = Mock()
            playwright.chromium.executable_path = "/missing/playwright/chromium"

            def which(command):
                return str(executable) if command == "chromium" else None

            with patch.dict(os.environ, {}, clear=True), patch.object(
                browser.sys, "platform", "linux"
            ), patch.object(browser.shutil, "which", side_effect=which):
                self.assertEqual(
                    browser.get_builtin_chromium_path(playwright),
                    str(executable.resolve()),
                )

    def test_linux_prefers_verified_managed_chromium(self):
        with tempfile.TemporaryDirectory() as temporary:
            managed = Path(temporary) / "managed" / "chrome"
            managed.parent.mkdir()
            managed.write_bytes(b"managed-browser")
            system = Path(temporary) / "system-chromium"
            system.write_bytes(b"system-browser")
            playwright = Mock()
            playwright.chromium.executable_path = str(managed)

            with patch.dict(
                os.environ,
                {"PLAYWRIGHT_BROWSERS_PATH": temporary},
                clear=True,
            ), patch.object(
                browser.sys, "platform", "linux"
            ), patch.object(
                browser.shutil, "which", return_value=str(system)
            ):
                self.assertEqual(
                    browser.get_builtin_chromium_path(playwright),
                    str(managed.resolve()),
                )

    def test_wayland_uses_xwayland_defaults_when_display_is_available(self):
        environment = {
            "XDG_SESSION_TYPE": "wayland",
            "DISPLAY": ":0",
        }
        with patch.dict(os.environ, environment, clear=True), patch.object(
            platform_support.sys, "platform", "linux"
        ):
            self.assertEqual(
                platform_support.chromium_launch_args(),
                ["--disable-dev-shm-usage", "--ozone-platform=x11"],
            )

    def test_self_update_is_limited_to_windows_installer_platform(self):
        with patch.object(platform_support.os, "name", "posix"):
            self.assertFalse(platform_support.supports_self_update())
        with patch.object(platform_support.os, "name", "nt"):
            self.assertTrue(platform_support.supports_self_update())

    def test_secret_service_protector_round_trip(self):
        encoded_key = base64.b64encode(b"k" * 32).decode("ascii")
        runner = Mock(
            return_value=Mock(
                returncode=0,
                stdout=encoded_key + "\n",
                stderr="",
            )
        )
        with patch(
            "integrated_client.online.secure.sys.platform",
            "linux",
        ):
            protector = SecretServiceProtector(
                command="/usr/bin/secret-tool",
                runner=runner,
            )
        plaintext = b"refresh-token-and-offline-entitlement"
        ciphertext = protector.protect(plaintext)
        self.assertNotEqual(ciphertext, plaintext)
        self.assertEqual(protector.unprotect(ciphertext), plaintext)
        with self.assertRaises(SecureStorageUnavailable):
            protector.unprotect(ciphertext[:-1] + bytes([ciphertext[-1] ^ 1]))
        self.assertEqual(runner.call_args.args[0][1], "lookup")

    def test_secret_service_creates_missing_master_key(self):
        calls = []

        def runner(arguments, **kwargs):
            calls.append((arguments, kwargs.get("input")))
            if arguments[1] == "lookup":
                return Mock(returncode=1, stdout="", stderr="")
            return Mock(returncode=0, stdout="", stderr="")

        with patch(
            "integrated_client.online.secure.sys.platform",
            "linux",
        ):
            protector = SecretServiceProtector(
                command="/usr/bin/secret-tool",
                runner=runner,
            )
        self.assertEqual(len(protector._key), 32)
        self.assertEqual(calls[1][0][1], "store")
        self.assertTrue(calls[1][1].endswith("\n"))

    def test_uos_build_files_keep_pyqt_out_of_pip_requirements(self):
        requirements = (
            Path(__file__).resolve().parents[1] / "requirements-uos-arm64.txt"
        ).read_text(encoding="utf-8")
        self.assertNotIn("PyQt5==", requirements)
        self.assertIn("opencv-python-headless", requirements)
        self.assertNotIn("\nopencv-python==", requirements)

    def test_uos_build_bundles_project_managed_chromium(self):
        root = Path(__file__).resolve().parents[1]
        prepare_script = (root / "scripts/uos-arm64/prepare-env.sh").read_text(
            encoding="utf-8"
        )
        build_script = (root / "scripts/uos-arm64/build.sh").read_text(
            encoding="utf-8"
        )
        launcher = (root / "packaging/uos-arm64/intdemo-client").read_text(
            encoding="utf-8"
        )
        self.assertIn("python -m playwright install chromium", prepare_script)
        self.assertIn('cp -a "$browser_source_dir/."', build_script)
        self.assertIn('browser/chrome', build_script)
        self.assertIn('bundled_browser="$package_root/browser/chrome"', launcher)
        self.assertIn("INTDEMO_CHROMIUM_PATH", launcher)

    def test_uos_preflight_recognizes_aarch64_elf(self):
        root = Path(__file__).resolve().parents[1]
        preflight = runpy.run_path(
            str(root / "scripts/uos-arm64/preflight.py")
        )
        header = bytearray(20)
        header[:4] = b"\x7fELF"
        header[5] = 1
        header[18:20] = (183).to_bytes(2, byteorder="little")
        with tempfile.TemporaryDirectory() as temporary:
            executable = Path(temporary) / "chrome"
            executable.write_bytes(header)
            self.assertEqual(preflight["_elf_machine"](executable), 183)

    def test_linux_online_config_uses_xdg_config_home(self):
        with tempfile.TemporaryDirectory() as temporary:
            config_dir = Path(temporary) / "intdemo-client"
            config_dir.mkdir()
            config_path = config_dir / "client-online.json"
            config_path.write_text(
                '{"base_url":"https://api.example.com","channel":"test"}',
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {"XDG_CONFIG_HOME": temporary},
                clear=True,
            ), patch(
                "integrated_client.online.config.sys.platform",
                "linux",
            ), patch(
                "integrated_client.online.config.sys.executable",
                str(Path(temporary) / "missing" / "intdemo-client"),
            ), patch(
                "integrated_client.online.config.Path.cwd",
                return_value=Path(temporary) / "working-copy",
            ):
                loaded = OnlineConfig.load()
            self.assertEqual(loaded.base_url, "https://api.example.com")


if __name__ == "__main__":
    unittest.main()

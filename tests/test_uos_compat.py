import base64
import hashlib
import json
import os
import runpy
import ssl
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
            executable.chmod(0o755)
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
            executable.chmod(0o755)
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
            managed.chmod(0o755)
            system = Path(temporary) / "system-chromium"
            system.write_bytes(b"system-browser")
            system.chmod(0o755)
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

    def test_uos_injected_wayland_qt_platform_falls_back_to_xcb(self):
        environment = {
            "XDG_SESSION_TYPE": "wayland",
            "DISPLAY": ":1",
            "QT_QPA_PLATFORM": "wayland",
        }
        with patch.dict(os.environ, environment, clear=True), patch.object(
            platform_support, "is_linux_arm64", return_value=True
        ):
            platform_support.configure_desktop_environment()
            self.assertEqual(os.environ["QT_QPA_PLATFORM"], "xcb")

    def test_uos_qt_offscreen_platform_is_preserved(self):
        environment = {
            "XDG_SESSION_TYPE": "wayland",
            "DISPLAY": ":1",
            "QT_QPA_PLATFORM": "offscreen",
        }
        with patch.dict(os.environ, environment, clear=True), patch.object(
            platform_support, "is_linux_arm64", return_value=True
        ):
            platform_support.configure_desktop_environment()
            self.assertEqual(os.environ["QT_QPA_PLATFORM"], "offscreen")

    def test_uos_explicit_native_wayland_override_wins(self):
        environment = {
            "XDG_SESSION_TYPE": "wayland",
            "DISPLAY": ":1",
            "QT_QPA_PLATFORM": "xcb",
            "INTDEMO_QT_QPA_PLATFORM": "wayland",
        }
        with patch.dict(os.environ, environment, clear=True), patch.object(
            platform_support, "is_linux_arm64", return_value=True
        ):
            platform_support.configure_desktop_environment()
            self.assertEqual(os.environ["QT_QPA_PLATFORM"], "wayland")

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

    def test_uos_guide_uses_legacy_compatible_git_checkout(self):
        guide = (
            Path(__file__).resolve().parents[1] / "docs/UOS_ARM64.md"
        ).read_text(encoding="utf-8")
        self.assertIn("git checkout codex/uos-arm64-compat", guide)
        self.assertNotIn("git switch", guide)

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
        self.assertIn("launcher.log", launcher)
        self.assertIn("GIO_LAUNCHED_DESKTOP_FILE", launcher)
        self.assertIn("INTDEMO_QT_QPA_PLATFORM", launcher)
        self.assertIn("effective_qt_platform", launcher)
        self.assertIn('2>>"$launcher_log"', launcher)

    def test_uos_build_generates_native_arm64_deb(self):
        root = Path(__file__).resolve().parents[1]
        packaging = root / "packaging/uos-arm64/deb"
        deb_builder = (root / "scripts/uos-arm64/build-deb.sh").read_text(
            encoding="utf-8"
        )
        build_script = (root / "scripts/uos-arm64/build.sh").read_text(
            encoding="utf-8"
        )
        control = (packaging / "control.in").read_text(encoding="utf-8")
        info = json.loads(
            (packaging / "info.json.in")
            .read_text(encoding="utf-8")
            .replace("@UOS_VERSION@", "0.2.8.0")
        )
        desktop = (
            packaging / "com.e23aqiu.intdemo.desktop"
        ).read_text(encoding="utf-8")

        self.assertIn('app_id="com.e23aqiu.intdemo"', deb_builder)
        self.assertIn('desktop_file_name="$app_id.desktop"', deb_builder)
        self.assertIn('app_root="$deb_root/opt/apps/$app_id"', deb_builder)
        self.assertIn("dpkg-deb --root-owner-group", deb_builder)
        self.assertIn("fakeroot dpkg-deb --build", deb_builder)
        self.assertIn("DEBIAN/md5sums", deb_builder)
        self.assertIn("trap cleanup_deb_build EXIT", deb_builder)
        self.assertIn(
            'cp "$repo_root/packaging/uos-arm64/intdemo-client"',
            deb_builder,
        )
        self.assertIn('packaging/uos-arm64/deb/README.txt', deb_builder)
        self.assertNotIn("$HOME/.local", deb_builder)
        self.assertNotIn("postinst", deb_builder.lower())
        self.assertIn('bash "$script_dir/build-deb.sh"', build_script)
        self.assertIn("--skip-deb", build_script)
        self.assertIn("Package: com.e23aqiu.intdemo", control)
        self.assertIn("Architecture: arm64", control)
        self.assertIn("libsecret-tools", control)
        self.assertEqual(info["appid"], "com.e23aqiu.intdemo")
        self.assertEqual(info["version"], "0.2.8.0")
        self.assertEqual(info["arch"], ["arm64"])
        self.assertTrue(info["permissions"]["clipboard"])
        self.assertIn(
            "Exec=/opt/apps/com.e23aqiu.intdemo/files/intdemo-client",
            desktop,
        )
        self.assertIn("Name=逃费车辆智能查询平台（UOS）", desktop)
        self.assertIn("StartupNotify=false", desktop)
        self.assertFalse((packaging / "com.e23aqiu.intdemo.uos.desktop").exists())
        self.assertEqual(
            (packaging / "com.e23aqiu.intdemo.desktop").stem,
            info["appid"],
        )
        self.assertIn(
            "entries/applications/$desktop_file_name",
            deb_builder,
        )

    def test_uos_build_bundles_compatible_cpp_runtime(self):
        root = Path(__file__).resolve().parents[1]
        environment = (root / "environment-uos-arm64.yml").read_text(
            encoding="utf-8"
        )
        build_script = (root / "scripts/uos-arm64/build.sh").read_text(
            encoding="utf-8"
        )
        entrypoint = (root / "main.py").read_text(encoding="utf-8")
        self.assertIn("libgcc-ng>=12", environment)
        self.assertIn("libstdcxx-ng>=12", environment)
        self.assertIn("libstdc++.so.6 libgcc_s.so.1", build_script)
        self.assertIn("GLIBCXX_3.4.26", build_script)
        self.assertIn('qt_libstdcxx_real="$(readlink -f', build_script)
        self.assertLess(
            build_script.index('rm -f -- "$artifact" "$artifact.sha256"'),
            build_script.index("pyinstaller --noconfirm --clean"),
        )
        self.assertIn('"$package_root/intdemo-client" --self-check', build_script)
        self.assertIn('runtime_self_check = "--self-check" in sys.argv', entrypoint)

    def test_uos_package_contains_verified_online_service_config(self):
        root = Path(__file__).resolve().parents[1]
        packaging = root / "packaging/uos-arm64"
        config = json.loads(
            (packaging / "client-online.json").read_text(encoding="utf-8")
        )
        certificate_pem = (
            packaging / "certs/intdemo-caddy-root.crt"
        ).read_text(encoding="ascii")
        certificate_der = ssl.PEM_cert_to_DER_cert(certificate_pem)
        build_script = (root / "scripts/uos-arm64/build.sh").read_text(
            encoding="utf-8"
        )
        launcher = (packaging / "intdemo-client").read_text(encoding="utf-8")
        installer = (packaging / "install-user.sh").read_text(encoding="utf-8")

        self.assertEqual(config["base_url"], "https://43.138.177.65")
        self.assertEqual(
            config["ca_bundle"],
            "certs/intdemo-caddy-root.crt",
        )
        self.assertEqual(
            hashlib.sha256(certificate_der).hexdigest(),
            "1ba425e2184fe9bab3e90620773ec9bc89aaf292f84c00cf54b5a0901c4bf69f",
        )
        self.assertIn("INTDEMO_CONNECTION_CONFIG", launcher)
        self.assertLess(
            launcher.index('if [[ -f "$user_connection_config" ]]'),
            launcher.index('elif [[ -f "$bundled_connection_config" ]]'),
        )
        self.assertIn("检查打包后的在线配置", build_script)
        self.assertIn('cp "$repo_root/packaging/uos-arm64/client-online.json"', build_script)
        self.assertIn('if [[ ! -f "$user_config"', installer)
        self.assertIn("保留现有在线配置", installer)

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

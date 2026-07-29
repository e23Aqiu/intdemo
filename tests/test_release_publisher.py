from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from PyQt5.QtWidgets import QApplication

from release_publisher.connection_control import ConnectionControlClient
from release_publisher.core import (
    PublisherSettings,
    ReleaseOptions,
    SettingsStore,
    build_release_plan,
    project_version,
    project_version_mismatches,
    set_project_version,
    validate_release_options,
)
from release_publisher.ui import ReleasePublisherWindow

REPO_ROOT = Path(__file__).resolve().parents[1]


class ReleasePublisherCoreTests(unittest.TestCase):
    def test_current_project_version_fields_are_consistent(self):
        version = project_version(REPO_ROOT)

        self.assertRegex(version, r"^\d+\.\d+\.\d+$")
        self.assertEqual(project_version_mismatches(REPO_ROOT, version), [])

    def test_settings_are_saved_outside_the_repository_format(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "publisher-settings.json"
            store = SettingsStore(path)
            settings = PublisherSettings(
                base_url="https://api.example.com",
                ca_bundle="C:/certs/root.crt",
                inno_compiler="C:/Inno/ISCC.exe",
                remote_host="intdemo-test",
                remote_path="/opt/intdemo/deploy/updates",
                identity_file="C:/keys/release",
                channel="stable",
                build_portable=False,
            )

            store.save(settings)

            self.assertEqual(SettingsStore(path).load(), settings)
            self.assertNotIn("mandatory", path.read_text(encoding="utf-8"))

    def test_release_plan_reuses_existing_powershell_scripts(self):
        options = ReleaseOptions(
            repo_root=REPO_ROOT,
            version="0.2.5",
            base_url="https://api.example.com",
            notes="发布器测试",
            delta_from_version="0.2.4",
            mandatory=True,
            remote_host="intdemo-test",
            identity_file="C:/keys/release",
        )

        steps = build_release_plan(
            options,
            include_tests=True,
            include_build=True,
            include_publish=True,
        )

        self.assertEqual(
            [step.key for step in steps],
            [
                "compile_client",
                "test_client",
                "test_server",
                "build_packages",
                "publish",
                "snapshot",
            ],
        )
        build_step = steps[3]
        self.assertTrue(
            any(
                Path(argument).name == "build-releases.ps1"
                for argument in build_step.arguments
            )
        )
        self.assertIn("-DeltaFromVersion", build_step.arguments)
        publish_step = steps[4]
        self.assertTrue(
            any(
                Path(argument).name == "publish-update.ps1"
                for argument in publish_step.arguments
            )
        )
        self.assertIn("-Mandatory", publish_step.arguments)
        self.assertIn("-RemoteHost", publish_step.arguments)

    def test_version_sync_updates_all_authoritative_fields(self):
        current = project_version(REPO_ROOT)
        major, minor, patch = (int(part) for part in current.split("."))
        target = f"{major}.{minor}.{patch + 1}"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = [
                "integrated_client/config.py",
                "integrated_client/__init__.py",
                "server/app/__init__.py",
                "server/app/main.py",
                "server/app/schemas.py",
                "server/pyproject.toml",
                "installer/intdemo.iss",
                "installer/version_info.txt",
                "docker-compose.yml",
                "scripts/build-installer.ps1",
                "scripts/build-portable.ps1",
                "scripts/build-releases.ps1",
                "scripts/build-online-test.ps1",
            ]
            for relative in paths:
                source = REPO_ROOT / relative
                destination = root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(source.read_bytes())

            changed = set_project_version(
                root,
                target,
                require_clean=False,
            )

            self.assertEqual(len(changed), len(paths))
            self.assertEqual(project_version(root), target)
            self.assertEqual(project_version_mismatches(root, target), [])
            version_info = (
                root / "installer" / "version_info.txt"
            ).read_text(encoding="utf-8")
            self.assertIn(
                f"filevers=({major}, {minor}, {patch + 1}, 0)",
                version_info,
            )
            self.assertIn(f"ProductVersion', u'{target}'", version_info)

    def test_pipeline_validation_requires_notes_and_safe_version_order(self):
        options = ReleaseOptions(
            repo_root=REPO_ROOT,
            version="0.2.5",
            base_url="https://api.example.com",
            notes="",
            delta_from_version="0.2.5",
        )

        errors = validate_release_options(options, for_pipeline=True)

        self.assertIn("更新说明不能为空", errors)
        self.assertIn("增量来源版本必须低于目标版本", errors)

    def test_connection_control_client_uses_transient_admin_session(self):
        session = Mock()
        session.headers = {}

        def response(status_code, payload):
            result = Mock()
            result.status_code = status_code
            result.json.return_value = payload
            return result

        session.request.side_effect = [
            response(
                200,
                {
                    "access_token": "access",
                    "refresh_token": "refresh",
                    "account": {
                        "role": "admin",
                        "must_change_password": False,
                    },
                },
            ),
            response(
                200,
                {
                    "global_blocked": False,
                    "clients": [
                        {
                            "id": "device-id",
                            "username": "station",
                            "blocked": False,
                        }
                    ],
                },
            ),
            response(200, {"affected_count": 1}),
        ]
        client = ConnectionControlClient(
            "https://api.example.com",
            session=session,
            client_version="0.2.6",
            device_uid="00000000-0000-0000-0000-000000000123",
        )

        client.login("admin", "Secret!234")
        state = client.list_clients()
        result = client.disconnect_device("device-id")

        self.assertFalse(state["global_blocked"])
        self.assertEqual(result["affected_count"], 1)
        login_call = session.request.call_args_list[0]
        self.assertEqual(login_call.args[:2], ("POST", "https://api.example.com/api/v1/auth/login"))
        self.assertTrue(login_call.kwargs["json"]["control_client"])
        self.assertEqual(
            login_call.kwargs["json"]["device_name"],
            "IntDemo 打包器连接测试控制台",
        )
        self.assertFalse(hasattr(client, "password"))
        self.assertEqual(
            session.request.call_args_list[2].args[1],
            "https://api.example.com/api/v1/admin/connection-test/devices/device-id/disconnect",
        )

    @unittest.skipUnless(shutil.which("powershell.exe"), "requires Windows PowerShell")
    def test_combined_build_routes_delta_only_to_installer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "build-releases.ps1").write_bytes(
                (REPO_ROOT / "scripts" / "build-releases.ps1").read_bytes()
            )
            (root / "build-portable.ps1").write_text(
                """
param(
    [Parameter(Mandatory = $true)][string]$BaseUrl,
    [string]$CaBundle = "",
    [string]$Version = ""
)
[IO.File]::WriteAllText(
    (Join-Path $PSScriptRoot "portable-result.txt"),
    "$BaseUrl|$Version"
)
""".strip(),
                encoding="utf-8",
            )
            (root / "build-installer.ps1").write_text(
                """
param(
    [Parameter(Mandatory = $true)][string]$BaseUrl,
    [string]$CaBundle = "",
    [string]$Version = "",
    [string]$DeltaFromVersion = "",
    [string]$InnoCompiler = ""
)
[IO.File]::WriteAllText(
    (Join-Path $PSScriptRoot "installer-result.txt"),
    "$BaseUrl|$Version|$DeltaFromVersion"
)
""".strip(),
                encoding="utf-8",
            )

            result = subprocess.run(
                [
                    "powershell.exe",
                    "-NoLogo",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(root / "build-releases.ps1"),
                    "-BaseUrl",
                    "https://api.example.com",
                    "-Version",
                    "1.2.3",
                    "-DeltaFromVersion",
                    "1.2.2",
                ],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )

            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            self.assertEqual(
                (root / "portable-result.txt").read_text(encoding="utf-8"),
                "https://api.example.com|1.2.3",
            )
            self.assertEqual(
                (root / "installer-result.txt").read_text(encoding="utf-8"),
                "https://api.example.com|1.2.3|1.2.2",
            )


class ReleasePublisherUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def test_window_exposes_safe_release_workflow(self):
        window = ReleasePublisherWindow(REPO_ROOT)

        self.assertEqual(window.windowTitle(), "IntDemo 专用打包发布器")
        self.assertEqual(window.current_version_value.text(), f"v{project_version(REPO_ROOT)}")
        self.assertEqual(window.channel_combo.currentData(), "test")
        self.assertFalse(window.mandatory_check.isChecked())
        self.assertFalse(window.cancel_button.isEnabled())
        self.assertIn("测试", window.pipeline_button.text())
        self.assertIn("发布", window.pipeline_button.text())
        self.assertEqual(window.control_username_edit.text(), "admin")
        self.assertEqual(window.control_password_edit.text(), "")
        self.assertIn("断开全部", window.disconnect_all_button.text())
        self.assertIn("恢复全部", window.restore_all_button.text())
        self.assertFalse(window.disconnect_client_button.isEnabled())
        self.assertFalse(window.restore_client_button.isEnabled())
        window._render_connection_clients(
            {
                "global_blocked": False,
                "clients": [
                    {
                        "id": "device-1",
                        "username": "luogang",
                        "display_name": "萝岗中心站",
                        "name": "演示电脑",
                        "client_version": "0.2.5",
                        "connected": True,
                        "blocked": False,
                    }
                ],
            }
        )
        self.assertEqual(window.connection_client_combo.count(), 1)
        self.assertIn("萝岗中心站", window.connection_client_combo.currentText())
        self.assertTrue(window.disconnect_client_button.isEnabled())
        self.assertFalse(window.restore_client_button.isEnabled())
        encoded = "中文日志".encode()
        self.assertEqual(window._decode_output(encoded[:2]), "")
        self.assertEqual(
            window._decode_output(encoded[2:], final=True),
            "中文日志",
        )

        window.deleteLater()


if __name__ == "__main__":
    unittest.main()

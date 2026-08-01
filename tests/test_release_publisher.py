from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PyQt5.QtWidgets import QApplication, QMessageBox

from integrated_client.config import APP_NAME
from release_publisher.connection_control import ConnectionControlClient
from release_publisher.core import (
    CommandStep,
    GitPushPlan,
    PublisherSettings,
    ReleaseOptions,
    SettingsStore,
    build_git_commit_steps,
    build_git_push_plan,
    build_pause_distribution_steps,
    build_release_plan,
    git_status,
    project_version,
    project_version_mismatches,
    set_project_version,
    validate_pause_distribution_options,
    validate_release_options,
)
from release_publisher.ui import ReleasePublisherWindow

REPO_ROOT = Path(__file__).resolve().parents[1]


class ReleasePublisherCoreTests(unittest.TestCase):
    def test_current_project_product_name_fields_are_consistent(self):
        expected_name = "逃费车辆智能查询平台"
        installer = (REPO_ROOT / "installer" / "intdemo.iss").read_text(
            encoding="utf-8"
        )
        version_info = (REPO_ROOT / "installer" / "version_info.txt").read_text(
            encoding="utf-8"
        )

        self.assertEqual(APP_NAME, expected_name)
        self.assertIn(f'#define MyAppName "{expected_name}"', installer)
        self.assertIn(f'#define MyAppExeName "{expected_name}.exe"', installer)
        self.assertIn(
            f"StringStruct(u'FileDescription', u'{expected_name}')",
            version_info,
        )
        self.assertIn(
            f"StringStruct(u'OriginalFilename', u'{expected_name}.exe')",
            version_info,
        )
        self.assertIn(
            f"StringStruct(u'ProductName', u'{expected_name}')",
            version_info,
        )

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

    def test_pause_distribution_plan_only_targets_the_selected_remote_channel(self):
        options = ReleaseOptions(
            repo_root=REPO_ROOT,
            version="not-used",
            base_url="not-used",
            notes="",
            channel="stable",
            remote_host="intdemo-prod",
            remote_path="/opt/intdemo/deploy/updates",
            identity_file="C:/keys/release",
        )

        steps = build_pause_distribution_steps(options)

        self.assertEqual([step.key for step in steps], ["pause_distribution"])
        step = steps[0]
        self.assertTrue(
            any(
                Path(argument).name == "pause-update.ps1"
                for argument in step.arguments
            )
        )
        self.assertIn("-Channel", step.arguments)
        self.assertIn("stable", step.arguments)
        self.assertIn("-RemoteHost", step.arguments)
        self.assertIn("intdemo-prod", step.arguments)
        self.assertIn("-IdentityFile", step.arguments)

    def test_pause_distribution_validation_is_remote_only_and_rejects_root(self):
        valid = ReleaseOptions(
            repo_root=REPO_ROOT,
            version="not-used",
            base_url="not-used",
            notes="",
            remote_host="intdemo-test",
        )
        missing_host = ReleaseOptions(
            repo_root=REPO_ROOT,
            version="not-used",
            base_url="not-used",
            notes="",
            remote_path="/",
        )

        with patch("release_publisher.core.shutil.which", return_value="tool"):
            self.assertEqual(validate_pause_distribution_options(valid), [])
            errors = validate_pause_distribution_options(missing_host)

        self.assertIn("暂停分发必须填写 SSH 主机", errors)
        self.assertIn("远程更新目录必须是安全的绝对 Linux 路径", errors)

    @unittest.skipUnless(shutil.which("powershell.exe"), "requires Windows PowerShell")
    def test_pause_and_publish_scripts_have_valid_powershell_syntax(self):
        scripts = [
            REPO_ROOT / "scripts" / "pause-update.ps1",
            REPO_ROOT / "scripts" / "publish-update.ps1",
        ]
        for script in scripts:
            with self.subTest(script=script.name):
                escaped_path = str(script).replace("'", "''")
                parser = (
                    "$tokens=$null;$errors=$null;"
                    "[System.Management.Automation.Language.Parser]::"
                    f"ParseFile('{escaped_path}',[ref]$tokens,[ref]$errors)"
                    ">$null;"
                    "if($errors.Count){$errors|% Message;exit 1}"
                )
                result = subprocess.run(
                    [
                        "powershell.exe",
                        "-NoLogo",
                        "-NoProfile",
                        "-Command",
                        parser,
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
                self.assertEqual(
                    result.returncode,
                    0,
                    result.stderr or result.stdout,
                )

    @unittest.skipUnless(shutil.which("powershell.exe"), "requires Windows PowerShell")
    def test_pause_script_archives_then_atomically_installs_compatible_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path = root / "manifest.json"
            marker_path = root / "marker.json"
            command_path = root / "remote-command.txt"
            manifest_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "channel": "test",
                        "version": "1.2.3",
                        "installer_path": (
                            "/updates/files/IntDemoOnline-Setup-1.2.3.exe"
                        ),
                    }
                ),
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment.update(
                {
                    "INTDEMO_FAKE_MANIFEST": str(manifest_path),
                    "INTDEMO_FAKE_MARKER": str(marker_path),
                    "INTDEMO_FAKE_COMMAND": str(command_path),
                    "INTDEMO_FAKE_HASH": "a" * 64,
                    "INTDEMO_PAUSE_SCRIPT": str(
                        REPO_ROOT / "scripts" / "pause-update.ps1"
                    ),
                }
            )
            harness = """
function global:ssh {
    $command = [string]$args[-1]
    $global:LASTEXITCODE = 0
    if ($command.StartsWith("if [ -f")) {
        Write-Output $env:INTDEMO_FAKE_HASH
        return
    }
    if ($command.StartsWith("cat ")) {
        Get-Content -Raw -LiteralPath $env:INTDEMO_FAKE_MANIFEST
        return
    }
    if ($command.StartsWith("mkdir ")) {
        return
    }
    if ($command.StartsWith("echo ")) {
        [IO.File]::WriteAllText($env:INTDEMO_FAKE_COMMAND, $command)
        return
    }
    throw "Unexpected fake ssh command: $command"
}
function global:scp {
    Copy-Item -LiteralPath ([string]$args[0]) -Destination $env:INTDEMO_FAKE_MARKER
    $global:LASTEXITCODE = 0
}
& $env:INTDEMO_PAUSE_SCRIPT `
    -Channel test `
    -RemoteHost intdemo-test `
    -RemotePath /opt/intdemo/deploy/updates
""".strip()

            result = subprocess.run(
                [
                    "powershell.exe",
                    "-NoLogo",
                    "-NoProfile",
                    "-Command",
                    harness,
                ],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=environment,
            )

            self.assertEqual(
                result.returncode,
                0,
                result.stderr or result.stdout,
            )
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            self.assertEqual(marker["version"], "0.0.0")
            self.assertTrue(marker["paused"])
            self.assertEqual(marker["paused_version"], "1.2.3")
            remote_command = command_path.read_text(encoding="utf-8")
            self.assertIn("/updates-paused/test-1.2.3-", remote_command)
            self.assertIn(" && cp ", remote_command)
            self.assertIn(" && mv ", remote_command)
            self.assertIn("Paused version: 1.2.3", result.stdout)

    @unittest.skipUnless(shutil.which("powershell.exe"), "requires Windows PowerShell")
    def test_publish_script_keeps_version_guard_while_channel_is_paused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scripts = root / "scripts"
            scripts.mkdir()
            publish_script = scripts / "publish-update.ps1"
            publish_script.write_bytes(
                (REPO_ROOT / "scripts" / "publish-update.ps1").read_bytes()
            )
            installer = root / "installer.exe"
            installer.write_bytes(b"test-installer")
            environment = os.environ.copy()
            environment.update(
                {
                    "INTDEMO_PUBLISH_SCRIPT": str(publish_script),
                    "INTDEMO_TEST_INSTALLER": str(installer),
                }
            )
            harness = """
function global:git {
    $global:LASTEXITCODE = 0
    if ($args -contains "rev-parse") {
        Write-Output (("a" * 40) -join "")
        return
    }
    if ($args -contains "status") {
        return
    }
    throw "Unexpected fake git command"
}
function global:ssh {
    $command = [string]$args[-1]
    $global:LASTEXITCODE = 0
    if ($command.StartsWith("mkdir ")) {
        return
    }
    if ($command.StartsWith("if [ -f") -and $command.Contains("cat ")) {
        Write-Output '{"schema_version":1,"channel":"test","version":"0.0.0","paused":true,"paused_version":"1.2.3"}'
        return
    }
    throw "Unexpected fake ssh command: $command"
}
function global:scp {
    throw "scp must not run when the version guard rejects publishing"
}
& $env:INTDEMO_PUBLISH_SCRIPT `
    -Installer $env:INTDEMO_TEST_INSTALLER `
    -Version 1.2.3 `
    -Notes test `
    -Channel test `
    -RemoteHost intdemo-test `
    -RemotePath /opt/intdemo/deploy/updates
""".strip()

            result = subprocess.run(
                [
                    "powershell.exe",
                    "-NoLogo",
                    "-NoProfile",
                    "-Command",
                    harness,
                ],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=environment,
            )

            self.assertNotEqual(result.returncode, 0)
            output = result.stdout + result.stderr
            self.assertIn("already publishes version 1.2.3", output)
            self.assertNotIn("scp must not run", output)

    def test_git_commit_steps_stage_all_changes_and_create_local_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(
                ["git", "init", "-q"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "IntDemo Test"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.email", "intdemo@example.invalid"],
                cwd=root,
                check=True,
            )
            tracked = root / "tracked.txt"
            tracked.write_text("before\n", encoding="utf-8")
            subprocess.run(["git", "add", "--all"], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-q", "-m", "initial"],
                cwd=root,
                check=True,
            )

            tracked.write_text("after\n", encoding="utf-8")
            (root / "new.txt").write_text("new\n", encoding="utf-8")
            steps = build_git_commit_steps(root, "准备测试版本")

            self.assertEqual(
                [step.key for step in steps],
                ["git_stage", "git_commit"],
            )
            self.assertEqual(steps[0].arguments, ("add", "--all"))
            self.assertEqual(
                steps[1].arguments,
                ("commit", "-m", "准备测试版本"),
            )
            for step in steps:
                subprocess.run(
                    [step.program, *step.arguments],
                    cwd=step.working_directory,
                    check=True,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )

            self.assertEqual(git_status(root), [])
            subject = subprocess.run(
                ["git", "log", "-1", "--pretty=%s"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
            ).stdout.strip()
            self.assertEqual(subject, "准备测试版本")

    def test_git_push_plan_sets_upstream_then_pushes_only_current_branch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            remote = root / "remote.git"
            local = root / "local"
            local.mkdir()
            subprocess.run(
                ["git", "init", "--bare", "-q", str(remote)],
                check=True,
            )
            subprocess.run(["git", "init", "-q"], cwd=local, check=True)
            subprocess.run(
                ["git", "config", "user.name", "IntDemo Test"],
                cwd=local,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.email", "intdemo@example.invalid"],
                cwd=local,
                check=True,
            )
            (local / "tracked.txt").write_text("first\n", encoding="utf-8")
            subprocess.run(["git", "add", "--all"], cwd=local, check=True)
            subprocess.run(
                ["git", "commit", "-q", "-m", "first"],
                cwd=local,
                check=True,
            )
            subprocess.run(
                ["git", "remote", "add", "origin", str(remote)],
                cwd=local,
                check=True,
            )

            first_plan = build_git_push_plan(local)

            self.assertTrue(first_plan.sets_upstream)
            self.assertEqual(first_plan.remote, "origin")
            self.assertEqual(first_plan.ahead_count, 1)
            self.assertIn("--set-upstream", first_plan.step.arguments)
            subprocess.run(
                [first_plan.step.program, *first_plan.step.arguments],
                cwd=first_plan.step.working_directory,
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )

            (local / "tracked.txt").write_text("second\n", encoding="utf-8")
            subprocess.run(["git", "add", "--all"], cwd=local, check=True)
            subprocess.run(
                ["git", "commit", "-q", "-m", "second"],
                cwd=local,
                check=True,
            )
            second_plan = build_git_push_plan(local)

            self.assertFalse(second_plan.sets_upstream)
            self.assertEqual(second_plan.ahead_count, 1)
            self.assertIn("second", second_plan.commits[0])
            self.assertNotIn("--set-upstream", second_plan.step.arguments)
            self.assertEqual(
                second_plan.step.arguments[-1],
                f"HEAD:refs/heads/{second_plan.branch}",
            )
            subprocess.run(
                [second_plan.step.program, *second_plan.step.arguments],
                cwd=second_plan.step.working_directory,
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            self.assertEqual(build_git_push_plan(local).ahead_count, 0)

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
        self.assertEqual(window.pause_distribution_button.text(), "暂停分发")
        self.assertIn("保留安装包", window.pause_distribution_button.toolTip())
        self.assertTrue(window.pause_distribution_button.isEnabled())
        self.assertIn("测试", window.pipeline_button.text())
        self.assertIn("发布", window.pipeline_button.text())
        self.assertEqual(window.commit_changes_button.text(), "提交变更")
        self.assertIn("不会推送", window.commit_changes_button.toolTip())
        self.assertEqual(window.push_button.text(), "推送")
        self.assertIn("不会强制推送", window.push_button.toolTip())
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

    def test_pause_distribution_button_confirms_and_runs_remote_pause(self):
        window = ReleasePublisherWindow(REPO_ROOT)
        window.remote_host_edit.setText("intdemo-test")
        window.remote_path_edit.setText("/opt/intdemo/deploy/updates")
        window.identity_edit.clear()

        with (
            patch(
                "release_publisher.ui.validate_pause_distribution_options",
                return_value=[],
            ),
            patch(
                "release_publisher.ui.QMessageBox.warning",
                return_value=QMessageBox.Yes,
            ) as warning,
            patch.object(window, "_save_settings") as save_settings,
            patch.object(window, "_run_steps") as run_steps,
        ):
            window._run_pause_distribution()

        self.assertIn("已经取得清单", warning.call_args.args[2])
        self.assertIn("安装包不会删除", warning.call_args.args[2])
        save_settings.assert_called_once_with()
        steps = run_steps.call_args.args[0]
        self.assertEqual([step.key for step in steps], ["pause_distribution"])
        self.assertEqual(
            run_steps.call_args.kwargs["completion_message"],
            "test 通道已暂停分发",
        )
        window.deleteLater()

    def test_commit_button_previews_and_runs_local_commit_steps(self):
        window = ReleasePublisherWindow(REPO_ROOT)

        with (
            patch(
                "release_publisher.ui.git_status",
                return_value=[" M tracked.txt", "?? new.txt"],
            ),
            patch(
                "release_publisher.ui.project_version",
                return_value="0.2.5",
            ),
            patch(
                "release_publisher.ui.QInputDialog.getText",
                return_value=("准备 v0.2.5 发布", True),
            ),
            patch(
                "release_publisher.ui.QMessageBox.warning",
                return_value=QMessageBox.Yes,
            ) as warning,
            patch.object(window, "_run_steps") as run_steps,
        ):
            window._commit_changes()

        self.assertIn("git add --all", warning.call_args.args[2])
        steps = run_steps.call_args.args[0]
        self.assertEqual(
            [step.key for step in steps],
            ["git_stage", "git_commit"],
        )
        self.assertEqual(
            run_steps.call_args.kwargs["completion_message"],
            "本地 Git 提交已创建（未推送）",
        )
        window.deleteLater()

    def test_push_button_previews_and_runs_non_force_push(self):
        window = ReleasePublisherWindow(REPO_ROOT)
        plan = GitPushPlan(
            branch="codex/test",
            remote="origin",
            remote_branch="codex/test",
            upstream="origin/codex/test",
            ahead_count=1,
            commits=("abc1234 测试提交",),
            step=CommandStep(
                key="git_push",
                title="推送 Git 分支 codex/test",
                program="git",
                arguments=(
                    "push",
                    "--porcelain",
                    "origin",
                    "HEAD:refs/heads/codex/test",
                ),
                working_directory=REPO_ROOT,
            ),
        )

        with (
            patch(
                "release_publisher.ui.build_git_push_plan",
                return_value=plan,
            ),
            patch(
                "release_publisher.ui.QMessageBox.warning",
                return_value=QMessageBox.Yes,
            ) as warning,
            patch.object(window, "_run_steps") as run_steps,
        ):
            window._push_changes()

        self.assertIn("不会强制推送", warning.call_args.args[2])
        self.assertNotIn("--force", plan.step.arguments)
        self.assertEqual(run_steps.call_args.args[0], [plan.step])
        self.assertEqual(
            run_steps.call_args.kwargs["completion_message"],
            "当前分支已推送到 origin/codex/test",
        )
        window.deleteLater()


if __name__ == "__main__":
    unittest.main()

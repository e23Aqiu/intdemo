import base64
import hashlib
import json
import os
import runpy
import shutil
import ssl
import subprocess
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
from integrated_client.tools.aiqicha_tool import create_browser
from integrated_client.ui import file_dialogs


class UosCompatibilityTests(unittest.TestCase):
    @staticmethod
    def _make_executable(path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"browser")
        try:
            path.chmod(0o755)
        except OSError:
            pass
        return path

    def test_login_system_label_distinguishes_supported_desktops(self):
        self.assertEqual(
            platform_support.login_system_label(
                system="Windows",
                release="10",
                version="10.0.19045",
            ),
            "Win10",
        )
        self.assertEqual(
            platform_support.login_system_label(
                system="Windows",
                release="10",
                version="10.0.22631",
            ),
            "Win11",
        )
        with patch.object(platform_support, "is_uos", return_value=True), patch.object(
            platform_support.platform,
            "system",
            return_value="Linux",
        ):
            self.assertEqual(platform_support.login_system_label(), "统信 UOS")

    def test_windows_compatible_browser_prefers_edge_over_360(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            edge = self._make_executable(
                root / "edge" / "Microsoft" / "Edge" / "Application" / "msedge.exe"
            )
            self._make_executable(
                root / "360" / "360" / "360se6" / "Application" / "360se.exe"
            )
            environment = {
                "ProgramFiles": str(root / "program-files"),
                "ProgramFiles(x86)": str(root / "edge"),
                "LOCALAPPDATA": str(root / "local-app-data"),
            }
            with patch.dict(os.environ, environment, clear=True), patch.object(
                browser.sys, "platform", "win32"
            ), patch.object(browser.shutil, "which", return_value=None):
                path, name = browser.get_compatible_browser_path()
            self.assertEqual(path, str(edge.resolve()))
            self.assertEqual(name, "Microsoft Edge")

    def test_windows_compatible_browser_falls_back_to_360(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            browser_360 = self._make_executable(
                root / "360" / "360se6" / "Application" / "360se.exe"
            )
            environment = {
                "ProgramFiles": str(root / "program-files"),
                "ProgramFiles(x86)": str(root),
                "LOCALAPPDATA": str(root / "local-app-data"),
            }
            with patch.dict(os.environ, environment, clear=True), patch.object(
                browser.sys, "platform", "win32"
            ), patch.object(browser.shutil, "which", return_value=None):
                path, name = browser.get_compatible_browser_path()
            self.assertEqual(path, str(browser_360.resolve()))
            self.assertEqual(name, "360 浏览器")

    def test_windows_compatible_browser_prefers_edge_from_path_over_360(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            edge = self._make_executable(root / "custom-edge" / "msedge.exe")
            self._make_executable(
                root / "program-files" / "360" / "360se6" / "Application" / "360se.exe"
            )

            def which(command):
                return str(edge) if command == "msedge.exe" else None

            with patch.dict(
                os.environ,
                {"ProgramFiles": str(root / "program-files")},
                clear=True,
            ), patch.object(browser.sys, "platform", "win32"), patch.object(
                browser.shutil,
                "which",
                side_effect=which,
            ):
                path, name = browser.get_compatible_browser_path()
            self.assertEqual(path, str(edge.resolve()))
            self.assertEqual(name, "Microsoft Edge")

    def test_windows_compatible_browser_finds_per_user_360(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            browser_360 = self._make_executable(
                root / "roaming" / "360se6" / "Application" / "360se.exe"
            )
            with patch.dict(
                os.environ,
                {"APPDATA": str(root / "roaming")},
                clear=True,
            ), patch.object(browser.sys, "platform", "win32"), patch.object(
                browser.shutil,
                "which",
                return_value=None,
            ):
                path, name = browser.get_compatible_browser_path()
            self.assertEqual(path, str(browser_360.resolve()))
            self.assertEqual(name, "360 浏览器")

    def test_uos_compatible_browser_uses_deepin_browser(self):
        with tempfile.TemporaryDirectory() as temporary:
            executable = self._make_executable(Path(temporary) / "deepin-browser")

            def which(command):
                return str(executable) if command == "deepin-browser" else None

            with patch.dict(os.environ, {}, clear=True), patch.object(
                browser.sys, "platform", "linux"
            ), patch.object(browser.shutil, "which", side_effect=which):
                path, name = browser.get_compatible_browser_path()
            self.assertEqual(path, str(executable.resolve()))
            self.assertEqual(name, "统信 Deepin 浏览器")

    def test_uos_compatible_browser_resolves_desktop_launcher_exec(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = self._make_executable(root / "opt" / "deepin-browser")
            desktop_file = (
                root / "data" / "applications" / "org.deepin.browser.desktop"
            )
            desktop_file.parent.mkdir(parents=True, exist_ok=True)
            desktop_file.write_text(
                "[Desktop Entry]\n"
                "Name=浏览器\n"
                f"TryExec={executable}\n"
                f"Exec=\"{executable}\" %U\n",
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {"XDG_DATA_HOME": str(root / "data")},
                clear=True,
            ), patch.object(browser.sys, "platform", "linux"), patch.object(
                browser.shutil, "which", return_value=None
            ):
                path, name = browser.get_compatible_browser_path()
            self.assertEqual(path, str(executable.resolve()))
            self.assertEqual(name, "统信 Deepin 浏览器")

    def test_compatible_browser_missing_explains_disabling_mode(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(
            browser.sys, "platform", "linux"
        ), patch.object(browser.shutil, "which", return_value=None), patch.object(
            browser,
            "_linux_desktop_file_candidates",
            return_value=(),
        ), self.assertRaisesRegex(
            browser.CompatibleBrowserUnavailableError,
            "关闭.*爱企查兼容模式",
        ):
            browser.get_compatible_browser_path()

    def test_create_browser_uses_selected_compatible_executable(self):
        class Options:
            def __init__(self):
                self.address = ""
                self.arguments = []
                self.existing = False

            def set_argument(self, *_args):
                self.arguments.append(str(_args[0]))
                return self

            def set_user_data_path(self, value):
                self.set_argument(f"--user-data-dir={value}")
                return self

            def set_local_port(self, value):
                self.address = f"127.0.0.1:{value}"
                return self

            def set_user_agent(self, _value):
                self.set_argument(f"--user-agent={_value}")
                return self

            def set_browser_path(self, value):
                self.browser_path = value
                return self

            def existing_only(self):
                self.existing = True
                return self

        page = type(
            "Page",
            (),
            {
                "set": type(
                    "Timeouts",
                    (),
                    {"timeouts": lambda *_args, **_kwargs: None},
                )()
            },
        )()
        options = Options()
        process = Mock()
        with patch(
            "integrated_client.tools.aiqicha_tool.ChromiumOptions",
            return_value=options,
        ), patch(
            "integrated_client.tools.aiqicha_tool.ChromiumPage",
            return_value=page,
        ), patch(
            "integrated_client.tools.aiqicha_tool.get_compatible_browser_path",
            return_value=("/usr/bin/deepin-browser", "统信 Deepin 浏览器"),
        ), patch(
            "integrated_client.tools.aiqicha_tool._available_local_port",
            return_value=19321,
        ), patch(
            "integrated_client.tools.aiqicha_tool.system_application_environment",
            return_value={"PATH": "/usr/bin"},
        ), patch(
            "integrated_client.tools.aiqicha_tool.subprocess.Popen",
            return_value=process,
        ) as popen:
            self.assertIs(create_browser(compatibility_mode=True), page)
        self.assertEqual(options.browser_path, "/usr/bin/deepin-browser")
        self.assertTrue(options.existing)
        command = popen.call_args.args[0]
        self.assertEqual(command[0], "/usr/bin/deepin-browser")
        self.assertIn("--remote-debugging-port=19321", command)
        self.assertEqual(popen.call_args.kwargs["env"], {"PATH": "/usr/bin"})
        temporary_profile = page._intdemo_temporary_profile
        self.assertTrue(Path(temporary_profile.name).is_dir())
        temporary_profile.cleanup()

    def test_system_browser_environment_drops_packaged_runtime_paths(self):
        source = {
            "LD_LIBRARY_PATH": "/opt/intdemo/_internal",
            "LD_LIBRARY_PATH_ORIG": "/usr/lib/aarch64-linux-gnu",
            "QT_PLUGIN_PATH": "/opt/intdemo/_internal/PyQt5/Qt5/plugins",
            "QT_QPA_PLATFORM_PLUGIN_PATH": "/opt/intdemo/_internal/platforms",
            "QT_QPA_PLATFORM": "xcb",
            "QT_IM_MODULE": "fcitx",
            "PATH": "/usr/bin",
        }
        with patch.object(platform_support.sys, "platform", "linux"):
            cleaned = platform_support.system_application_environment(source)

        self.assertEqual(cleaned["LD_LIBRARY_PATH"], "/usr/lib/aarch64-linux-gnu")
        self.assertNotIn("LD_LIBRARY_PATH_ORIG", cleaned)
        self.assertNotIn("QT_PLUGIN_PATH", cleaned)
        self.assertNotIn("QT_QPA_PLATFORM_PLUGIN_PATH", cleaned)
        self.assertNotIn("QT_QPA_PLATFORM", cleaned)
        self.assertEqual(cleaned["QT_IM_MODULE"], "fcitx")
        self.assertEqual(cleaned["PATH"], "/usr/bin")
        self.assertEqual(source["LD_LIBRARY_PATH"], "/opt/intdemo/_internal")

    def test_uos_file_selection_prefers_desktop_portal_chooser(self):
        runner = Mock(
            return_value=Mock(
                returncode=0,
                stdout="/tmp/一.xlsx\n/tmp/二.xlsx\n",
                stderr="",
            )
        )
        with patch.object(file_dialogs, "is_uos", return_value=True), patch.object(
            file_dialogs.shutil,
            "which",
            return_value="/usr/bin/zenity",
        ), patch.object(file_dialogs.subprocess, "run", runner):
            paths, selected_filter = file_dialogs.SystemFileDialog.getOpenFileNames(
                None,
                "选择表格",
                "/tmp",
                "Excel (*.xlsx);;所有文件 (*)",
            )

        self.assertEqual(paths, ["/tmp/一.xlsx", "/tmp/二.xlsx"])
        self.assertEqual(selected_filter, "")
        arguments = runner.call_args.args[0]
        self.assertIn("--multiple", arguments)
        self.assertIn("--modal", arguments)
        self.assertIn("--file-filter=Excel | *.xlsx", arguments)
        self.assertEqual(runner.call_args.kwargs["env"]["GTK_USE_PORTAL"], "1")

    def test_uos_file_selection_attaches_to_parent_and_uses_nested_runner(self):
        parent = Mock()
        parent.window.return_value.winId.return_value = 4082
        with patch.dict(os.environ, {"DISPLAY": ":0"}, clear=True), patch.object(
            file_dialogs,
            "is_uos",
            return_value=True,
        ), patch.object(
            file_dialogs.shutil,
            "which",
            return_value="/usr/bin/zenity",
        ), patch.object(
            file_dialogs,
            "system_application_environment",
            return_value={"DISPLAY": ":0"},
        ), patch.object(
            file_dialogs,
            "_run_process_blocking",
            return_value=(0, "/tmp/result.xlsx\n"),
        ) as run_process:
            selected = file_dialogs._uos_file_selection(
                parent,
                "选择表格",
                "",
                "Excel (*.xlsx)",
            )

        self.assertEqual(selected, ["/tmp/result.xlsx"])
        arguments, environment = run_process.call_args.args
        self.assertIn("--modal", arguments)
        self.assertIn("--attach=4082", arguments)
        self.assertTrue(run_process.call_args.kwargs["nested"])
        self.assertEqual(environment["GDK_BACKEND"], "x11")
        self.assertEqual(environment["GTK_USE_PORTAL"], "0")

    def test_uos_file_selection_without_parent_handle_uses_qt_modal_fallback(self):
        parent = Mock()
        parent.window.return_value.winId.return_value = 0
        with patch.dict(os.environ, {}, clear=True), patch.object(
            file_dialogs,
            "is_uos",
            return_value=True,
        ), patch.object(
            file_dialogs.shutil,
            "which",
            return_value="/usr/bin/zenity",
        ), patch.object(
            file_dialogs,
            "_run_process_blocking",
            return_value=None,
        ) as run_process, patch.object(
            file_dialogs,
            "_qt_uos_file_selection",
            return_value=(["/tmp/result.xlsx"], "Excel (*.xlsx)"),
        ) as qt_dialog:
            selected = file_dialogs.SystemFileDialog.getOpenFileName(
                parent,
                "选择表格",
                "",
                "Excel (*.xlsx)",
            )

        self.assertEqual(selected, ("/tmp/result.xlsx", "Excel (*.xlsx)"))
        run_process.assert_called_once()
        qt_dialog.assert_called_once()

    def test_uos_file_selection_without_parent_handle_still_tries_system_chooser(self):
        parent = Mock()
        parent.window.return_value.winId.return_value = 0
        with patch.dict(os.environ, {}, clear=True), patch.object(
            file_dialogs,
            "is_uos",
            return_value=True,
        ), patch.object(
            file_dialogs,
            "_find_zenity",
            return_value="/usr/bin/zenity",
        ), patch.object(
            file_dialogs,
            "system_application_environment",
            return_value={},
        ), patch.object(
            file_dialogs,
            "_run_process_blocking",
            return_value=(0, "/tmp/result.xlsx\n"),
        ) as run_process, patch.object(
            file_dialogs,
            "_qt_uos_file_selection",
        ) as qt_dialog:
            selected = file_dialogs.SystemFileDialog.getOpenFileName(
                parent,
                "选择表格",
                "",
                "Excel (*.xlsx)",
            )

        self.assertEqual(selected, ("/tmp/result.xlsx", ""))
        arguments, environment = run_process.call_args.args
        self.assertNotIn("--attach=0", arguments)
        self.assertEqual(environment["GTK_USE_PORTAL"], "1")
        qt_dialog.assert_not_called()

    def test_uos_qt_fallback_is_application_modal_and_always_on_top(self):
        dialog = Mock()
        dialog.exec_.return_value = 1
        dialog.selectedFiles.return_value = ["/tmp/result.zip"]
        dialog.selectedNameFilter.return_value = "ZIP (*.zip)"
        dialog.windowFlags.return_value = file_dialogs.Qt.Window
        dialog_class = Mock(return_value=dialog)
        dialog_class.DontUseNativeDialog = file_dialogs.QtFileDialog.DontUseNativeDialog
        dialog_class.ExistingFile = file_dialogs.QtFileDialog.ExistingFile
        dialog_class.ExistingFiles = file_dialogs.QtFileDialog.ExistingFiles
        dialog_class.AnyFile = file_dialogs.QtFileDialog.AnyFile
        dialog_class.AcceptSave = file_dialogs.QtFileDialog.AcceptSave

        with patch.object(file_dialogs, "QtFileDialog", dialog_class):
            selected = file_dialogs._qt_uos_file_selection(
                Mock(),
                "导入验证码数据集",
                "",
                "ZIP (*.zip)",
            )

        self.assertEqual(selected, (["/tmp/result.zip"], "ZIP (*.zip)"))
        dialog.setOption.assert_called_once_with(
            dialog_class.DontUseNativeDialog,
            True,
        )
        dialog.setWindowModality.assert_called_once_with(
            file_dialogs.Qt.ApplicationModal
        )
        dialog.setModal.assert_called_once_with(True)
        dialog.setWindowFlags.assert_called_once_with(
            dialog.windowFlags.return_value
            | file_dialogs.Qt.Dialog
            | file_dialogs.Qt.WindowStaysOnTopHint
        )
        dialog.setFileMode.assert_called_once_with(dialog_class.ExistingFile)

    def test_external_chooser_blocks_and_restores_client_windows(self):
        signal = Mock()
        process = Mock()
        process.finished = signal
        process.errorOccurred = Mock()
        process.waitForStarted.return_value = True
        process.state.return_value = 0
        process.exitStatus.return_value = 0
        process.exitCode.return_value = 0
        process.readAllStandardOutput.return_value = b"/tmp/result.xlsx\n"

        process_class = Mock(return_value=process)
        process_class.NotRunning = 0
        process_class.NormalExit = 0
        window = Mock()
        window.isVisible.return_value = True
        window.isEnabled.return_value = True
        application = Mock()
        application.topLevelWidgets.return_value = [window]
        application_thread = object()
        application.thread.return_value = application_thread
        blocker = Mock()

        with patch.object(
            file_dialogs.QApplication,
            "instance",
            return_value=application,
        ), patch.object(
            file_dialogs.QThread,
            "currentThread",
            return_value=application_thread,
        ), patch.object(
            file_dialogs,
            "QProcess",
            process_class,
        ), patch.object(
            file_dialogs,
            "_ApplicationInputBlocker",
            return_value=blocker,
        ), patch.object(
            file_dialogs.QCoreApplication,
            "sendPostedEvents",
        ):
            result = file_dialogs._run_process_blocking(
                ["/usr/bin/zenity", "--file-selection"],
                {"DISPLAY": ":0"},
            )

        self.assertEqual(result, (0, "/tmp/result.xlsx\n"))
        self.assertEqual(
            [call.args for call in window.setEnabled.call_args_list],
            [(False,), (True,)],
        )
        application.installEventFilter.assert_called_once_with(blocker)
        application.removeEventFilter.assert_called_once_with(blocker)

    def test_file_dialog_input_blocker_discards_delayed_clicks(self):
        blocker = file_dialogs._ApplicationInputBlocker()
        self.assertTrue(
            blocker.eventFilter(
                None,
                file_dialogs.QEvent(file_dialogs.QEvent.MouseButtonPress),
            )
        )
        self.assertTrue(
            blocker.eventFilter(
                None,
                file_dialogs.QEvent(file_dialogs.QEvent.Close),
            )
        )
        self.assertFalse(
            blocker.eventFilter(
                None,
                file_dialogs.QEvent(file_dialogs.QEvent.User),
            )
        )

    def test_non_uos_file_selection_keeps_qt_native_dialog(self):
        with patch.object(file_dialogs, "is_uos", return_value=False), patch.object(
            file_dialogs.QtFileDialog,
            "getOpenFileName",
            return_value=("C:/result.zip", "ZIP (*.zip)"),
        ) as qt_dialog:
            selected = file_dialogs.SystemFileDialog.getOpenFileName(
                None,
                "选择结果",
                "C:/",
                "ZIP (*.zip)",
            )

        self.assertEqual(selected[0], "C:/result.zip")
        qt_dialog.assert_called_once()

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
        ), self.assertRaisesRegex(RuntimeError, "INTDEMO_CHROMIUM_PATH"):
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

    def test_self_update_supports_windows_and_uos_arm64(self):
        with patch.object(
            platform_support,
            "update_platform_key",
            return_value=platform_support.WINDOWS_UPDATE_PLATFORM,
        ):
            self.assertTrue(platform_support.supports_self_update())
        with patch.object(
            platform_support,
            "update_platform_key",
            return_value=platform_support.UOS_UPDATE_PLATFORM,
        ), patch.object(platform_support, "is_uos", return_value=True):
            self.assertTrue(platform_support.supports_self_update())
        with patch.object(
            platform_support,
            "update_platform_key",
            return_value=platform_support.UOS_UPDATE_PLATFORM,
        ), patch.object(platform_support, "is_uos", return_value=False):
            self.assertFalse(platform_support.supports_self_update())

    def test_update_platform_and_uos_deb_install_command(self):
        self.assertEqual(
            platform_support.update_platform_key("linux", "arm64"),
            platform_support.UOS_UPDATE_PLATFORM,
        )
        with patch.object(
            platform_support,
            "update_platform_key",
            return_value=platform_support.UOS_UPDATE_PLATFORM,
        ), patch.object(
            platform_support.shutil,
            "which",
            side_effect=lambda name: (
                "/usr/bin/deepin-deb-installer"
                if name == "deepin-deb-installer"
                else f"/usr/bin/{name}"
            ),
        ):
            program, arguments = platform_support.update_install_command(
                "/tmp/IntDemo-UOS-arm64-1.2.3.deb"
            )
        self.assertEqual(program, "/usr/bin/deepin-deb-installer")
        self.assertEqual(len(arguments), 1)
        self.assertTrue(arguments[0].endswith("IntDemo-UOS-arm64-1.2.3.deb"))

    def test_uos_deb_install_command_falls_back_to_mime_opener(self):
        def which(name):
            return "/usr/bin/xdg-open" if name == "xdg-open" else None

        with patch.object(
            platform_support,
            "update_platform_key",
            return_value=platform_support.UOS_UPDATE_PLATFORM,
        ), patch.object(platform_support.shutil, "which", side_effect=which):
            program, arguments = platform_support.update_install_command(
                "/tmp/IntDemo-UOS-arm64-1.2.3.deb"
            )
        self.assertEqual(program, "/usr/bin/xdg-open")
        self.assertEqual(len(arguments), 1)
        self.assertTrue(arguments[0].endswith("IntDemo-UOS-arm64-1.2.3.deb"))

    def test_uos_deb_install_command_falls_back_to_pkexec_last(self):
        def which(name):
            return {
                "pkexec": "/usr/bin/pkexec",
                "dpkg": "/usr/bin/dpkg",
            }.get(name)

        with patch.object(
            platform_support,
            "update_platform_key",
            return_value=platform_support.UOS_UPDATE_PLATFORM,
        ), patch.object(platform_support.shutil, "which", side_effect=which):
            program, arguments = platform_support.update_install_command(
                "/tmp/IntDemo-UOS-arm64-1.2.3.deb"
            )
        self.assertEqual(program, "/usr/bin/pkexec")
        self.assertEqual(arguments[:2], ["/usr/bin/dpkg", "--install"])
        self.assertTrue(arguments[-1].endswith(".deb"))

    def test_uos_layer_install_command_restarts_through_stable_launcher(self):
        with tempfile.TemporaryDirectory() as directory:
            launcher = self._make_executable(Path(directory) / "intdemo-client")
            with patch.object(
                platform_support,
                "update_platform_key",
                return_value=platform_support.UOS_UPDATE_PLATFORM,
            ), patch.dict(
                os.environ,
                {"INTDEMO_UOS_LAUNCHER": str(launcher)},
                clear=False,
            ):
                program, arguments = platform_support.update_install_command(
                    Path(directory) / "update.intlayer"
                )

        self.assertEqual(program, str(launcher.resolve()))
        self.assertEqual(arguments[0], "--intdemo-apply-layer")
        self.assertEqual(arguments[1], str(os.getpid()))

    @unittest.skipUnless(
        os.name == "posix" and shutil.which("bash"),
        "requires a POSIX bash runtime",
    )
    def test_uos_launcher_trials_once_then_rolls_back_unconfirmed_layer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "package"
            data = root / "data"
            layer = data / "uos-layers/versions/1.2.3"
            output = root / "selected.txt"
            for candidate in (package, layer):
                (candidate / "app/_internal").mkdir(parents=True)
                (candidate / "browser").mkdir()
                app = candidate / "app/intdemo-client"
                app.write_text(
                    "#!/usr/bin/env bash\n"
                    "printf '%s|%s\\n' \"${INTDEMO_LAYER_ROOT:-seed}\" "
                    "\"${INTDEMO_LAYER_PENDING_VERSION:-}\" "
                    "> \"$INTDEMO_TEST_OUTPUT\"\n",
                    encoding="utf-8",
                )
                app.chmod(0o755)
                browser_entry = candidate / "browser/chrome"
                browser_entry.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
                browser_entry.chmod(0o755)
            (package / "build-info.txt").write_text(
                "version=1.2.2\n", encoding="ascii"
            )
            (layer / "layer-layout.json").write_text("{}\n", encoding="ascii")
            launcher = package / "intdemo-client"
            launcher.write_bytes(
                (
                    Path(__file__).resolve().parents[1]
                    / "packaging/uos-arm64/intdemo-client"
                ).read_bytes()
            )
            launcher.chmod(0o755)
            marker_root = data / "uos-layers"
            marker_root.mkdir(parents=True, exist_ok=True)
            (marker_root / "pending-version").write_text(
                "1.2.3\n", encoding="ascii"
            )
            environment = {
                **os.environ,
                "HOME": str(root / "home"),
                "INTDEMO_DATA_DIR": str(data),
                "INTDEMO_TEST_OUTPUT": str(output),
            }

            subprocess.run(
                ["bash", str(launcher)],
                check=True,
                env=environment,
            )
            selected, pending = output.read_text(encoding="utf-8").strip().split("|")
            self.assertEqual(Path(selected), layer)
            self.assertEqual(pending, "1.2.3")
            self.assertTrue((marker_root / "pending-attempted").is_file())

            subprocess.run(
                ["bash", str(launcher)],
                check=True,
                env=environment,
            )
            self.assertEqual(output.read_text(encoding="utf-8").strip(), "seed|")
            self.assertFalse((marker_root / "pending-version").exists())
            self.assertFalse((marker_root / "pending-attempted").exists())

    def test_uos_installer_environment_drops_bundled_runtime_paths(self):
        source = {
            "LD_LIBRARY_PATH": "/opt/intdemo/_internal",
            "LD_LIBRARY_PATH_ORIG": "/usr/lib/aarch64-linux-gnu",
            "QT_PLUGIN_PATH": "/opt/intdemo/_internal/qt5/plugins",
            "QT_QPA_PLATFORM_PLUGIN_PATH": "/opt/intdemo/_internal/platforms",
            "QT_QPA_PLATFORM": "xcb",
            "PATH": "/usr/bin",
        }
        with patch.object(
            platform_support,
            "update_platform_key",
            return_value=platform_support.UOS_UPDATE_PLATFORM,
        ):
            cleaned = platform_support.update_install_environment(source)
        self.assertEqual(cleaned["LD_LIBRARY_PATH"], "/usr/lib/aarch64-linux-gnu")
        self.assertNotIn("LD_LIBRARY_PATH_ORIG", cleaned)
        for variable in (
            "QT_PLUGIN_PATH",
            "QT_QPA_PLATFORM_PLUGIN_PATH",
            "QT_QPA_PLATFORM",
        ):
            self.assertNotIn(variable, cleaned)
        self.assertEqual(cleaned["PATH"], "/usr/bin")

    def test_uos_environment_selects_fcitx_and_isolates_system_qt_plugins(self):
        app_plugin_root = "/opt/intdemo/qt5/plugins"
        environment = {
            "XDG_SESSION_TYPE": "x11",
            "DISPLAY": ":0",
            "QT_PLUGIN_PATH": os.pathsep.join(
                (
                    "/usr/lib/aarch64-linux-gnu/qt5/plugins",
                    app_plugin_root,
                )
            ),
            "QT_QPA_PLATFORM_PLUGIN_PATH": (
                "/usr/lib/aarch64-linux-gnu/qt5/plugins/platforms"
            ),
            "INTDEMO_QT_SYSTEM_PLUGIN_PATH": "/usr/lib/qt5/plugins",
        }
        with patch.dict(os.environ, environment, clear=True), patch.object(
            platform_support, "is_linux_arm64", return_value=True
        ):
            platform_support.configure_desktop_environment()
            self.assertEqual(os.environ["QT_IM_MODULE"], "fcitx")
            self.assertEqual(os.environ["QT_PLUGIN_PATH"], app_plugin_root)
            self.assertNotIn("QT_QPA_PLATFORM_PLUGIN_PATH", os.environ)

    def test_uos_system_qt_plugin_path_detection_preserves_app_paths(self):
        self.assertTrue(
            platform_support._is_system_qt_plugin_path(
                "/usr/lib/aarch64-linux-gnu/qt5/plugins/imageformats"
            )
        )
        self.assertTrue(
            platform_support._is_system_qt_plugin_path(
                "/usr/local/lib/qt5/plugins"
            )
        )
        self.assertFalse(
            platform_support._is_system_qt_plugin_path(
                "/opt/apps/com.e23aqiu.intdemo/files/app/_internal/"
                "PyQt5/Qt5/plugins"
            )
        )

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
        self.assertIn('bundled_browser="$selected_root/browser/chrome"', launcher)
        self.assertIn('INTDEMO_LAYER_PENDING_VERSION', launcher)
        self.assertIn("INTDEMO_CHROMIUM_PATH", launcher)
        self.assertIn("launcher.log", launcher)
        self.assertIn("GIO_LAUNCHED_DESKTOP_FILE", launcher)
        self.assertIn("INTDEMO_QT_QPA_PLATFORM", launcher)
        self.assertIn("effective_qt_platform", launcher)
        self.assertIn("qt_im_module", launcher)
        self.assertIn("QT_IM_MODULE=fcitx", launcher)
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
        self.assertIn('cp "$package_root/client-online.json"', deb_builder)
        self.assertNotIn("trainer-trust.json", deb_builder)
        self.assertIn('cp -a "$package_root/certs/."', deb_builder)
        self.assertNotIn(
            'cp "$repo_root/packaging/uos-arm64/client-online.json"',
            deb_builder,
        )
        self.assertIn('packaging/uos-arm64/deb/README.txt', deb_builder)
        self.assertNotIn("$HOME/.local", deb_builder)
        self.assertNotIn("postinst", deb_builder.lower())
        self.assertIn('bash "$script_dir/build-deb.sh"', build_script)
        self.assertIn("--skip-deb", build_script)
        self.assertNotIn("--trainer-trust-file", build_script)
        self.assertNotIn("trainer-trust.json", build_script)
        self.assertIn("Package: com.e23aqiu.intdemo", control)
        self.assertIn("Architecture: arm64", control)
        self.assertIn("libsecret-tools", control)
        self.assertIn("fcitx-frontend-qt5", control)
        depends = next(
            line for line in control.splitlines() if line.startswith("Depends:")
        )
        self.assertIn("zenity", depends)
        self.assertIn("deepin-deb-installer", control)
        self.assertIn("policykit-1", control)
        self.assertEqual(info["appid"], "com.e23aqiu.intdemo")
        self.assertEqual(info["version"], "0.2.8.0")
        self.assertEqual(info["arch"], ["arm64"])
        self.assertTrue(info["permissions"]["clipboard"])
        self.assertIn(
            "Exec=/opt/apps/com.e23aqiu.intdemo/files/intdemo-client",
            desktop,
        )
        self.assertIn("Name=逃费车辆信息智能查询平台（UOS）", desktop)
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
        spec = (root / "integrated_client_uos_arm64.spec").read_text(
            encoding="utf-8"
        )
        prepare_script = (
            root / "scripts/uos-arm64/prepare-env.sh"
        ).read_text(encoding="utf-8")
        entrypoint = (root / "main.py").read_text(encoding="utf-8")
        self.assertIn("libgcc-ng>=12", environment)
        self.assertIn("libstdcxx-ng>=12", environment)
        platform_code = (root / "integrated_client/platform_support.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("QT_PLUGIN_PATH", platform_code)
        self.assertIn("QT_QPA_PLATFORM_PLUGIN_PATH", platform_code)
        self.assertNotIn('Path("/usr/lib").glob', platform_code)
        self.assertNotIn("INTDEMO_QT_SYSTEM_PLUGIN_PATH", platform_code)
        self.assertIn("Qt Fcitx 输入法插件", build_script)
        self.assertIn("platforminputcontexts", build_script)
        self.assertIn('cp -L -- "$fcitx_input_plugin"', build_script)
        self.assertIn("QT_DEBUG_PLUGINS=1", build_script)
        self.assertIn("Qt 插件隔离检查", build_script)
        self.assertIn("QT_IM_MODULE=compose", build_script)
        self.assertIn("libstdc++.so.6 libgcc_s.so.1", build_script)
        self.assertIn("GLIBCXX_3.4.26", build_script)
        self.assertIn('qt_libstdcxx_real="$(readlink -f', build_script)
        self.assertLess(
            build_script.index('rm -f -- "$artifact" "$artifact.sha256"'),
            build_script.index("pyinstaller --noconfirm --clean"),
        )
        self.assertIn('"$package_root/intdemo-client" --self-check', build_script)
        self.assertIn('runtime_self_check = "--self-check" in sys.argv', entrypoint)
        self.assertIn("app.inputMethod().locale()", entrypoint)
        self.assertIn('binaries.append((str(xdelta3_path), "tools"))', spec)
        self.assertIn('datas.append((str(xdelta_license)', spec)
        self.assertIn("包内 ARM64 补丁引擎检查", build_script)
        self.assertIn("最终用户不需要另行安装", prepare_script)

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

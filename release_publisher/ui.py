from __future__ import annotations

import locale
import os
from collections import deque
from pathlib import Path

from PyQt5.QtCore import QProcess, QProcessEnvironment, Qt, QTimer, QUrl
from PyQt5.QtGui import QDesktopServices, QIcon
from PyQt5.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from integrated_client.ui.file_dialogs import SystemFileDialog as QFileDialog
from integrated_client.ui.loading_dialog import run_with_loading

from .connection_control import (
    ConnectionControlClient,
    ConnectionControlError,
)
from .core import (
    CommandStep,
    PublisherError,
    PublisherSettings,
    ReleaseOptions,
    SettingsStore,
    build_git_commit_steps,
    build_git_mirror_push_plans,
    build_pause_distribution_steps,
    build_release_plan,
    build_uos_result_import_steps,
    build_windows_result_import_steps,
    find_inno_compiler,
    git_remote_url,
    git_status,
    is_native_uos_arm64_builder,
    project_version,
    release_readiness,
    set_project_version,
    validate_pause_distribution_options,
    validate_release_options,
    validate_test_environment,
)
from .release_tasks import ReleaseTaskError, detect_windows_result_portable


class ReleasePublisherWindow(QMainWindow):
    def __init__(self, repo_root: str | Path, parent=None):
        super().__init__(parent)
        self.repo_root = Path(repo_root).resolve()
        self.settings_store = SettingsStore()
        self.process: QProcess | None = None
        self._steps: deque[CommandStep] = deque()
        self._current_step: CommandStep | None = None
        self._cancel_requested = False
        self._completed_steps = 0
        self._total_steps = 0
        self._completion_message = "全部步骤执行成功"
        self._output_buffer = b""
        self._connection_client: ConnectionControlClient | None = None
        self._connection_client_key: tuple[str, ...] | None = None

        self.setWindowTitle("逃费车辆信息智能查询平台 · 双端打包发布器")
        self.setMinimumSize(1120, 720)
        self.resize(1320, 820)
        icon_path = (
            self.repo_root
            / "integrated_client"
            / "ui"
            / "assets"
            / "app-icon.ico"
        )
        if icon_path.is_file():
            self.setWindowIcon(QIcon(str(icon_path)))

        central = QWidget()
        self.setCentralWidget(central)
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(18, 16, 18, 16)
        root_layout.setSpacing(12)

        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title = QLabel("双端打包发布器")
        title.setObjectName("Title")
        subtitle = QLabel(
            "Windows / 统信双主控 · 原生安装包、跨机结果 ZIP、双端发布和快照归档"
        )
        subtitle.setObjectName("Muted")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addLayout(title_box)
        header.addStretch()
        self.repo_label = QLabel(str(self.repo_root))
        self.repo_label.setObjectName("PathBadge")
        self.repo_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        header.addWidget(self.repo_label)
        root_layout.addLayout(header)

        splitter = QSplitter(Qt.Horizontal)
        root_layout.addWidget(splitter, 1)
        splitter.addWidget(self._build_settings_panel())
        splitter.addWidget(self._build_log_panel())
        splitter.setSizes([500, 780])

        root_layout.addWidget(self._build_action_bar())
        self._load_settings()
        self._connect_release_readiness_signals()
        self._platform_selection_changed()
        self._refresh_project_version()
        self._set_busy(False)
        self._apply_style()

    def _build_settings_panel(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        panel = QWidget()
        scroll.setWidget(panel)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 10, 0)
        layout.setSpacing(12)

        version_card, version_form = self._card("版本与发布策略")
        self.current_version_value = QLabel("-")
        self.current_version_value.setObjectName("VersionValue")
        version_form.addRow("项目当前版本", self.current_version_value)
        self.release_readiness_label = QLabel("正在检查待发布产物…")
        self.release_readiness_label.setObjectName("ReleaseReadiness")
        self.release_readiness_label.setWordWrap(True)
        version_form.addRow("发布状态", self.release_readiness_label)
        version_row = QHBoxLayout()
        self.version_edit = QLineEdit()
        self.version_edit.setPlaceholderText("例如 0.2.6")
        self.sync_version_button = QPushButton("同步项目版本号")
        self.sync_version_button.clicked.connect(self._sync_project_version)
        version_row.addWidget(self.version_edit, 1)
        version_row.addWidget(self.sync_version_button)
        version_form.addRow("目标版本", version_row)
        self.delta_edit = QLineEdit()
        self.delta_edit.setPlaceholderText("留空则不构建 Windows 增量包")
        version_form.addRow("Windows 增量来源", self.delta_edit)
        self.uos_delta_edit = QLineEdit()
        self.uos_delta_edit.setPlaceholderText(
            "过渡版本留空；后续填写已发布的 UOS 基线版本"
        )
        version_form.addRow("UOS 增量来源", self.uos_delta_edit)
        self.channel_combo = QComboBox()
        self.channel_combo.addItem("test（当前客户端通道）", "test")
        self.channel_combo.addItem(
            "stable（预留，当前客户端不会检查）",
            "stable",
        )
        version_form.addRow("发布通道", self.channel_combo)
        self.mandatory_check = QCheckBox(
            "强制更新提示（当前为客户端弹窗约束）"
        )
        self.mandatory_check.setToolTip(
            "现有服务端尚未拒绝旧版本；客户端在线检查到更新后不能忽略提示。"
        )
        version_form.addRow("", self.mandatory_check)
        platform_row = QWidget()
        platform_layout = QHBoxLayout(platform_row)
        platform_layout.setContentsMargins(0, 0, 0, 0)
        self.windows_check = QCheckBox("Windows x64")
        self.windows_check.setChecked(True)
        self.windows_check.toggled.connect(self._platform_selection_changed)
        self.uos_check = QCheckBox("统信 UOS ARM64")
        self.uos_check.setChecked(True)
        self.uos_check.toggled.connect(self._platform_selection_changed)
        platform_layout.addWidget(self.windows_check)
        platform_layout.addWidget(self.uos_check)
        platform_layout.addStretch()
        version_form.addRow("构建平台", platform_row)
        self.output_mode_combo = QComboBox()
        self.output_mode_combo.addItem("仅安装包（EXE / DEB）", "native")
        self.output_mode_combo.addItem("仅构建包（发布器结果 ZIP）", "result")
        self.output_mode_combo.addItem("安装包 + 构建包", "both")
        self.output_mode_combo.setToolTip(
            "仅安装包用于直接安装；仅构建包用于带到另一台打包器导入。"
            "构建包内仍包含用于校验的安装包。"
        )
        self.output_mode_combo.currentIndexChanged.connect(
            self._platform_selection_changed
        )
        version_form.addRow("输出类型", self.output_mode_combo)
        self.portable_check = QCheckBox("同时构建便携包")
        self.portable_check.setChecked(False)
        self.portable_check.setToolTip(
            "默认不构建；导入 Windows 构建包时会自动识别并同步此选项"
        )
        version_form.addRow("", self.portable_check)
        layout.addWidget(version_card)

        connection_card, connection_form = self._card("构建连接配置")
        self.base_url_edit = QLineEdit()
        self.base_url_edit.setPlaceholderText("https://api.example.com")
        connection_form.addRow("服务地址", self.base_url_edit)
        self.ca_edit, ca_row = self._path_input(
            "选择 CA 根证书",
            "证书文件 (*.crt *.pem);;所有文件 (*)",
        )
        connection_form.addRow("私有 CA", ca_row)
        self.windows_build_mode_combo = QComboBox()
        self.windows_build_mode_combo.addItem(
            "自动（GitHub 优先，不可用时导出真机任务）",
            "auto",
        )
        self.windows_build_mode_combo.addItem("仅使用 GitHub Actions", "github")
        self.windows_build_mode_combo.addItem("Windows 真机任务包", "manual")
        self.windows_build_mode_combo.currentIndexChanged.connect(
            self._platform_selection_changed
        )
        connection_form.addRow("Windows 构建", self.windows_build_mode_combo)
        self.github_remote_edit = QLineEdit("origin")
        self.github_remote_edit.setPlaceholderText("GitHub Git 远程名称，通常为 origin")
        connection_form.addRow("GitHub 远程", self.github_remote_edit)
        self.github_repo_edit = QLineEdit()
        self.github_repo_edit.setPlaceholderText("可留空自动识别，例如 e23Aqiu/intdemo")
        connection_form.addRow("GitHub 仓库", self.github_repo_edit)
        self.gitee_url_edit = QLineEdit()
        self.gitee_url_edit.setPlaceholderText(
            "例如 https://gitee.com/e23aqiu/intdemo.git"
        )
        connection_form.addRow("Gitee 仓库", self.gitee_url_edit)
        self.inno_edit, inno_row = self._path_input(
            "选择 ISCC.exe",
            "程序文件 (ISCC.exe);;所有文件 (*)",
        )
        connection_form.addRow("Inno Setup", inno_row)
        self.uos_builder_host_edit = QLineEdit()
        self.uos_builder_host_edit.setPlaceholderText(
            "Windows 上填写 UOS ARM64 构建机，例如 uos-builder"
        )
        connection_form.addRow("UOS 构建主机", self.uos_builder_host_edit)
        self.uos_builder_path_edit = QLineEdit("/opt/intdemo")
        self.uos_builder_path_edit.setPlaceholderText(
            "构建机上已克隆且与本机提交一致的仓库绝对路径"
        )
        connection_form.addRow("UOS 仓库目录", self.uos_builder_path_edit)
        layout.addWidget(connection_card)

        control_card, control_form = self._card("客户端断连测试")
        control_hint = QLabel(
            "临时阻断业务 API、同步和 WebSocket，不撤销账号、设备或离线授权。"
            "恢复连接后，客户端会继续同步待发送数据。"
        )
        control_hint.setObjectName("Muted")
        control_hint.setWordWrap(True)
        control_form.addRow(control_hint)
        self.control_username_edit = QLineEdit("admin")
        self.control_username_edit.setPlaceholderText("服务器管理员账号")
        control_form.addRow("管理员账号", self.control_username_edit)
        self.control_password_edit = QLineEdit()
        self.control_password_edit.setEchoMode(QLineEdit.Password)
        self.control_password_edit.setPlaceholderText("仅保留在本次运行内存中")
        control_form.addRow("管理员密码", self.control_password_edit)
        client_row = QWidget()
        client_layout = QHBoxLayout(client_row)
        client_layout.setContentsMargins(0, 0, 0, 0)
        client_layout.setSpacing(6)
        self.connection_client_combo = QComboBox()
        self.connection_client_combo.setMinimumContentsLength(28)
        self.connection_client_combo.currentIndexChanged.connect(
            self._connection_selection_changed
        )
        self.refresh_clients_button = QPushButton("刷新")
        self.refresh_clients_button.clicked.connect(
            self._refresh_connection_clients
        )
        client_layout.addWidget(self.connection_client_combo, 1)
        client_layout.addWidget(self.refresh_clients_button)
        control_form.addRow("指定客户端", client_row)
        selected_actions = QWidget()
        selected_layout = QHBoxLayout(selected_actions)
        selected_layout.setContentsMargins(0, 0, 0, 0)
        selected_layout.setSpacing(6)
        self.disconnect_client_button = QPushButton("断开所选")
        self.disconnect_client_button.setObjectName("DangerButton")
        self.disconnect_client_button.clicked.connect(
            self._disconnect_selected_client
        )
        self.restore_client_button = QPushButton("恢复所选")
        self.restore_client_button.clicked.connect(
            self._restore_selected_client
        )
        selected_layout.addWidget(self.disconnect_client_button)
        selected_layout.addWidget(self.restore_client_button)
        control_form.addRow("", selected_actions)
        all_actions = QWidget()
        all_layout = QHBoxLayout(all_actions)
        all_layout.setContentsMargins(0, 0, 0, 0)
        all_layout.setSpacing(6)
        self.disconnect_all_button = QPushButton("断开全部客户端")
        self.disconnect_all_button.setObjectName("DangerButton")
        self.disconnect_all_button.clicked.connect(
            self._disconnect_all_clients
        )
        self.restore_all_button = QPushButton("恢复全部连接")
        self.restore_all_button.clicked.connect(self._restore_all_clients)
        all_layout.addWidget(self.disconnect_all_button)
        all_layout.addWidget(self.restore_all_button)
        control_form.addRow("", all_actions)
        self.connection_test_status = QLabel("尚未读取服务器客户端状态")
        self.connection_test_status.setObjectName("Muted")
        self.connection_test_status.setWordWrap(True)
        control_form.addRow("当前状态", self.connection_test_status)
        layout.addWidget(control_card)

        remote_card, remote_form = self._card("更新服务器发布")
        self.remote_host_edit = QLineEdit()
        self.remote_host_edit.setPlaceholderText(
            "双端发布必填，例如 intdemo-test"
        )
        remote_form.addRow("SSH 主机", self.remote_host_edit)
        self.remote_path_edit = QLineEdit("/opt/intdemo/deploy/updates")
        remote_form.addRow("服务器目录", self.remote_path_edit)
        self.identity_edit, identity_row = self._path_input(
            "选择 SSH 私钥",
            "所有文件 (*)",
        )
        remote_form.addRow("SSH 私钥", identity_row)
        layout.addWidget(remote_card)

        notes_card, notes_form = self._card("本次更新说明")
        self.notes_edit = QPlainTextEdit()
        self.notes_edit.setPlaceholderText(
            "填写面向客户端用户展示的更新内容；发布时不能为空。"
        )
        self.notes_edit.setMinimumHeight(118)
        notes_form.addRow(self.notes_edit)
        layout.addWidget(notes_card)
        layout.addStretch()
        return scroll

    def _build_log_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("LogCard")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)
        title_row = QHBoxLayout()
        label = QLabel("执行日志")
        label.setObjectName("SectionTitle")
        title_row.addWidget(label)
        title_row.addStretch()
        clear_button = QPushButton("清空")
        clear_button.clicked.connect(lambda: self.log_edit.clear())
        title_row.addWidget(clear_button)
        open_button = QPushButton("打开产物目录")
        open_button.clicked.connect(self._open_output)
        title_row.addWidget(open_button)
        layout.addLayout(title_row)
        self.status_label = QLabel("就绪")
        self.status_label.setObjectName("Status")
        layout.addWidget(self.status_label)
        self.log_edit = QPlainTextEdit()
        self.log_edit.setReadOnly(True)
        self.log_edit.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.log_edit.setObjectName("Log")
        layout.addWidget(self.log_edit, 1)
        return panel

    def _build_action_bar(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("ActionCard")
        actions = QGridLayout(panel)
        actions.setContentsMargins(14, 10, 14, 10)
        actions.setHorizontalSpacing(8)
        actions.setVerticalSpacing(8)

        build_label = QLabel("检查与构建")
        build_label.setObjectName("ActionGroupTitle")
        actions.addWidget(build_label, 0, 0)
        self.preflight_button = QPushButton("环境检查")
        self.preflight_button.clicked.connect(self._preflight)
        actions.addWidget(self.preflight_button, 0, 1)
        self.test_button = QPushButton("运行全部测试")
        self.test_button.clicked.connect(self._run_tests)
        actions.addWidget(self.test_button, 0, 2)
        self.build_button = QPushButton("导出所选产物")
        self.build_button.setToolTip("按当前平台和输出类型生成产物")
        self.build_button.clicked.connect(self._run_build)
        actions.addWidget(self.build_button, 0, 3)

        result_label = QLabel("结果与代码")
        result_label.setObjectName("ActionGroupTitle")
        actions.addWidget(result_label, 1, 0)
        self.import_windows_result_button = QPushButton("导入 Windows 构建包")
        self.import_windows_result_button.setToolTip(
            "校验 Windows 真机结果的版本、源提交、配置与 SHA-256 后纳入本次发布"
        )
        self.import_windows_result_button.clicked.connect(
            self._import_windows_result
        )
        actions.addWidget(self.import_windows_result_button, 1, 1)
        self.import_uos_result_button = QPushButton("导入统信构建包")
        self.import_uos_result_button.setToolTip(
            "校验统信结果的版本、源提交、配置与 SHA-256 后纳入本次发布"
        )
        self.import_uos_result_button.clicked.connect(self._import_uos_result)
        actions.addWidget(self.import_uos_result_button, 1, 2)
        self.commit_changes_button = QPushButton("提交到本地")
        self.commit_changes_button.setToolTip(
            "预览并暂存当前仓库的全部变更，创建本地 Git 提交；不会推送到远程"
        )
        self.commit_changes_button.clicked.connect(self._commit_changes)
        actions.addWidget(self.commit_changes_button, 1, 3)
        self.push_button = QPushButton("推送 GitHub + Gitee")
        self.push_button.setToolTip(
            "预览待推送提交并将当前分支同时推送到 GitHub(origin) 与 Gitee(gitee)；不会强制推送"
        )
        self.push_button.clicked.connect(self._push_changes)
        actions.addWidget(self.push_button, 1, 4, 1, 2)

        publish_label = QLabel("发布控制")
        publish_label.setObjectName("ActionGroupTitle")
        actions.addWidget(publish_label, 2, 0)
        self.pause_distribution_button = QPushButton("暂停分发")
        self.pause_distribution_button.setObjectName("DangerButton")
        self.pause_distribution_button.setToolTip(
            "暂停所选远程通道的新更新检查；保留安装包并归档当前活动清单"
        )
        self.pause_distribution_button.clicked.connect(
            self._run_pause_distribution
        )
        actions.addWidget(self.pause_distribution_button, 2, 1)
        self.publish_button = QPushButton("发布双端更新")
        self.publish_button.setToolTip(
            "大文件使用 SFTP 断点续传；网络中断会自动重试，重新发布也可继续断点"
        )
        self.publish_button.clicked.connect(self._run_publish)
        actions.addWidget(self.publish_button, 2, 2)
        self.pipeline_button = QPushButton("测试 → 双端构建 → 双端发布")
        self.pipeline_button.setObjectName("PrimaryButton")
        self.pipeline_button.clicked.connect(self._run_full_pipeline)
        actions.addWidget(self.pipeline_button, 2, 3, 1, 2)
        self.cancel_button = QPushButton("停止")
        self.cancel_button.setObjectName("DangerButton")
        self.cancel_button.clicked.connect(self._cancel)
        actions.addWidget(self.cancel_button, 2, 5)
        actions.setColumnStretch(6, 1)
        return panel

    @staticmethod
    def _card(title: str) -> tuple[QFrame, QFormLayout]:
        card = QFrame()
        card.setObjectName("Card")
        outer = QVBoxLayout(card)
        outer.setContentsMargins(16, 14, 16, 16)
        outer.setSpacing(10)
        label = QLabel(title)
        label.setObjectName("SectionTitle")
        outer.addWidget(label)
        form = QFormLayout()
        form.setSpacing(10)
        form.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        outer.addLayout(form)
        return card, form

    def _path_input(
        self,
        caption: str,
        file_filter: str,
    ) -> tuple[QLineEdit, QWidget]:
        wrapper = QWidget()
        row = QHBoxLayout(wrapper)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        edit = QLineEdit()
        button = QPushButton("浏览…")
        button.clicked.connect(
            lambda: self._browse_file(edit, caption, file_filter)
        )
        row.addWidget(edit, 1)
        row.addWidget(button)
        return edit, wrapper

    def _browse_file(
        self,
        edit: QLineEdit,
        caption: str,
        file_filter: str,
    ) -> None:
        current = edit.text().strip()
        start = str(Path(current).parent) if current else str(self.repo_root)
        selected, _ = QFileDialog.getOpenFileName(
            self,
            caption,
            start,
            file_filter,
        )
        if selected:
            edit.setText(selected)

    def _load_settings(self) -> None:
        settings = self.settings_store.load()
        self.base_url_edit.setText(settings.base_url)
        self.ca_edit.setText(settings.ca_bundle)
        self.control_username_edit.setText(settings.control_username)
        self.inno_edit.setText(settings.inno_compiler)
        self.windows_check.setChecked(settings.build_windows)
        self.uos_check.setChecked(settings.build_uos)
        mode_index = self.windows_build_mode_combo.findData(
            settings.windows_build_mode
        )
        self.windows_build_mode_combo.setCurrentIndex(max(0, mode_index))
        self.github_remote_edit.setText(settings.github_remote)
        self.github_repo_edit.setText(settings.github_repo)
        self.gitee_url_edit.setText(
            settings.gitee_url or git_remote_url(self.repo_root, "gitee")
        )
        self.uos_builder_host_edit.setText(settings.uos_builder_host)
        self.uos_builder_path_edit.setText(settings.uos_builder_path)
        self.remote_host_edit.setText(settings.remote_host)
        self.remote_path_edit.setText(settings.remote_path)
        self.identity_edit.setText(settings.identity_file)
        index = self.channel_combo.findData(settings.channel)
        self.channel_combo.setCurrentIndex(max(0, index))
        self.portable_check.setChecked(settings.build_portable)
        output_index = self.output_mode_combo.findData(settings.build_output_mode)
        self.output_mode_combo.setCurrentIndex(max(0, output_index))
        if not self.inno_edit.text().strip():
            compiler = find_inno_compiler()
            if compiler is not None:
                self.inno_edit.setText(str(compiler))
        if not self.base_url_edit.text().strip():
            self._load_project_connection_defaults()

    def _load_project_connection_defaults(self) -> None:
        path = (
            self.repo_root
            / "integrated_client"
            / "online"
            / "client-online.json"
        )
        try:
            import json

            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, ValueError, TypeError):
            return
        self.base_url_edit.setText(str(payload.get("base_url") or ""))
        ca_bundle = str(payload.get("ca_bundle") or "")
        if ca_bundle:
            candidate = (path.parent / ca_bundle).resolve()
            if candidate.is_file():
                self.ca_edit.setText(str(candidate))

    def _connect_release_readiness_signals(self) -> None:
        for edit in (
            self.version_edit,
            self.delta_edit,
            self.uos_delta_edit,
            self.base_url_edit,
            self.ca_edit,
        ):
            edit.editingFinished.connect(self._refresh_release_readiness)
        self.channel_combo.currentIndexChanged.connect(
            self._refresh_release_readiness
        )
        self.portable_check.toggled.connect(self._refresh_release_readiness)
        self.output_mode_combo.currentIndexChanged.connect(
            self._refresh_release_readiness
        )

    def _connection_parameters(self) -> tuple[str, str, str, str, str]:
        return (
            self.base_url_edit.text().strip(),
            self.ca_edit.text().strip(),
            self.control_username_edit.text().strip(),
            self.control_password_edit.text(),
            self.current_version_value.text().removeprefix("v") or "developer",
        )

    def _authenticated_connection_client(
        self,
        parameters: tuple[str, str, str, str, str],
    ) -> ConnectionControlClient:
        base_url, ca_bundle, username, password, version = parameters
        if not base_url:
            raise ConnectionControlError("请先填写服务地址")
        if not username or not password:
            raise ConnectionControlError("请输入服务器管理员账号和密码")
        key = (base_url, ca_bundle, username, password, version)
        if self._connection_client is None or self._connection_client_key != key:
            client = ConnectionControlClient(
                base_url,
                ca_bundle=ca_bundle,
                client_version=version,
            )
            client.login(username, password)
            self._connection_client = client
            self._connection_client_key = key
        return self._connection_client

    def _run_connection_request(
        self,
        message: str,
        action=None,
    ) -> dict | None:
        parameters = self._connection_parameters()

        def request():
            client = self._authenticated_connection_client(parameters)
            try:
                result = action(client) if action is not None else None
                state = client.list_clients()
            except ConnectionControlError as exc:
                if exc.status_code != 401:
                    raise
                client.login(parameters[2], parameters[3])
                result = action(client) if action is not None else None
                state = client.list_clients()
            return {"result": result, "state": state}

        try:
            response = run_with_loading(self, message, request)
        except ConnectionControlError as exc:
            self._append_log(f"客户端连接测试失败：{exc}")
            QMessageBox.warning(self, "连接测试操作失败", str(exc))
            return None
        except Exception as exc:  # noqa: BLE001 - developer tool boundary
            self._append_log(f"客户端连接测试异常：{exc}")
            QMessageBox.warning(self, "连接测试操作失败", str(exc))
            return None
        self._render_connection_clients(response.get("state") or {})
        return response

    def _refresh_connection_clients(self) -> None:
        response = self._run_connection_request(
            "正在读取客户端连接状态…",
        )
        if response is None:
            return
        count = len((response.get("state") or {}).get("clients") or [])
        self._append_log(f"已读取 {count} 个可测试客户端的连接状态。")

    def _render_connection_clients(self, state: dict) -> None:
        selected = self._selected_connection_client()
        selected_id = str((selected or {}).get("id") or "")
        clients = list(state.get("clients") or [])
        self.connection_client_combo.blockSignals(True)
        self.connection_client_combo.clear()
        selected_index = -1
        for index, client in enumerate(clients):
            blocked = bool(client.get("blocked"))
            connected = bool(client.get("connected"))
            status = "已断开" if blocked else ("已连接" if connected else "可连接")
            display_name = str(
                client.get("display_name")
                or client.get("username")
                or "未知账号"
            )
            username = str(client.get("username") or "-")
            device_name = str(client.get("name") or "Windows 客户端")
            version = str(client.get("client_version") or "unknown")
            device_uid = str(client.get("device_uid") or "")
            identity = f" · ID {device_uid[-8:]}" if device_uid else ""
            label = (
                f"[{status}] {display_name}（{username}） · "
                f"{device_name} · {version}{identity}"
            )
            self.connection_client_combo.addItem(label, client)
            last_seen = str(client.get("last_seen_at") or "未知")
            self.connection_client_combo.setItemData(
                index,
                f"设备 UID：{device_uid or '未知'}\n最近在线：{last_seen}",
                Qt.ToolTipRole,
            )
            if str(client.get("id") or "") == selected_id:
                selected_index = index
        if clients:
            self.connection_client_combo.setCurrentIndex(
                max(selected_index, 0)
            )
        self.connection_client_combo.blockSignals(False)
        globally_blocked = bool(state.get("global_blocked"))
        blocked_count = sum(bool(item.get("blocked")) for item in clients)
        if globally_blocked:
            summary = (
                f"全部客户端断连测试已启用；{blocked_count}/{len(clients)} "
                "个客户端当前被阻断。"
            )
        elif clients:
            summary = (
                f"共 {len(clients)} 个客户端，当前 {blocked_count} 个已断开。"
            )
        else:
            summary = "服务器暂未登记可测试的业务客户端。"
        self.connection_test_status.setText(summary)
        self._connection_selection_changed()

    def _selected_connection_client(self) -> dict | None:
        value = self.connection_client_combo.currentData()
        return value if isinstance(value, dict) else None

    def _connection_selection_changed(self, _index=-1) -> None:
        client = self._selected_connection_client()
        available = client is not None and self.process is None
        blocked = bool((client or {}).get("blocked"))
        self.disconnect_client_button.setEnabled(available and not blocked)
        self.restore_client_button.setEnabled(available and blocked)

    def _disconnect_selected_client(self) -> None:
        client = self._selected_connection_client()
        if client is None:
            QMessageBox.information(self, "请选择客户端", "请先刷新并选择客户端。")
            return
        label = self.connection_client_combo.currentText()
        reply = QMessageBox.warning(
            self,
            "断开指定客户端",
            f"将临时阻断以下客户端的业务 API、同步和 WebSocket：\n\n"
            f"{label}\n\n"
            "账号、设备授权和本地待同步数据都会保留。确定继续吗？",
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if reply != QMessageBox.Yes:
            return
        device_id = str(client["id"])
        response = self._run_connection_request(
            "正在断开指定客户端…",
            lambda control: control.disconnect_device(device_id),
        )
        if response is not None:
            self._append_log(f"已断开客户端：{label}")

    def _restore_selected_client(self) -> None:
        client = self._selected_connection_client()
        if client is None:
            QMessageBox.information(self, "请选择客户端", "请先刷新并选择客户端。")
            return
        label = self.connection_client_combo.currentText()
        device_id = str(client["id"])
        response = self._run_connection_request(
            "正在恢复指定客户端…",
            lambda control: control.restore_device(device_id),
        )
        if response is not None:
            self._append_log(f"已恢复客户端连接：{label}")

    def _disconnect_all_clients(self) -> None:
        reply = QMessageBox.warning(
            self,
            "断开全部客户端",
            "将临时阻断当前及之后接入的全部业务客户端。\n\n"
            "打包器控制连接会保留，以便随时恢复；账号、设备授权和"
            "本地待同步数据不会被删除。确定继续吗？",
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if reply != QMessageBox.Yes:
            return
        response = self._run_connection_request(
            "正在断开全部客户端…",
            lambda control: control.disconnect_all(),
        )
        if response is not None:
            self._append_log("已启用全部客户端断连测试。")

    def _restore_all_clients(self) -> None:
        response = self._run_connection_request(
            "正在恢复全部客户端连接…",
            lambda control: control.restore_all(),
        )
        if response is not None:
            self._append_log("已恢复全部客户端连接。")

    def _platform_selection_changed(self, _value=None) -> None:
        windows = self.windows_check.isChecked()
        uos = self.uos_check.isChecked()
        native_uos = is_native_uos_arm64_builder()
        windows_mode = str(self.windows_build_mode_combo.currentData())
        if windows and uos:
            platforms = "EXE + DEB"
        elif windows:
            platforms = "EXE"
        elif uos:
            platforms = "DEB"
        else:
            platforms = "所选平台"
        output_mode = str(self.output_mode_combo.currentData())
        mode_label = {
            "native": "仅安装包",
            "result": "仅构建包",
            "both": "安装包 + 构建包",
        }.get(output_mode, "构建")
        manual_windows_request = (
            native_uos and windows and windows_mode == "manual"
        )
        if manual_windows_request:
            if uos:
                self.build_button.setText(
                    f"导出 DEB + Windows 构建请求包（{mode_label}）"
                )
            else:
                self.build_button.setText("导出 Windows 构建请求包")
        else:
            self.build_button.setText(f"导出 {platforms}（{mode_label}）")
        tooltip = {
            "native": "只导出可直接安装的 EXE/DEB，不生成发布器结果 ZIP",
            "result": (
                "只导出可由另一台发布器导入的结果 ZIP；"
                "结果包内仍包含用于校验的安装包"
            ),
            "both": "同时导出可直接安装的 EXE/DEB 和发布器结果 ZIP",
        }.get(output_mode, "按当前平台和输出类型生成产物")
        if manual_windows_request:
            tooltip = (
                "Windows 真机模式只生成构建请求包，不会在统信主机上生成最终 EXE；"
                "请把请求包交给 Windows 真机执行。"
            )
        self.build_button.setToolTip(tooltip)
        self.portable_check.setEnabled(windows)
        self.windows_build_mode_combo.setEnabled(native_uos and windows)
        self.github_remote_edit.setEnabled(windows)
        self.github_repo_edit.setEnabled(
            native_uos and windows and windows_mode != "manual"
        )
        self.inno_edit.setEnabled(not native_uos and windows)
        self.uos_builder_host_edit.setEnabled(not native_uos and uos)
        self.uos_builder_path_edit.setEnabled(not native_uos and uos)
        self.uos_delta_edit.setEnabled(
            uos or bool(self.uos_delta_edit.text().strip())
        )
        if self.process is None:
            self.import_windows_result_button.setEnabled(windows)
            self.import_uos_result_button.setEnabled(uos)

    def _save_settings(self) -> None:
        settings = PublisherSettings(
            base_url=self.base_url_edit.text().strip(),
            ca_bundle=self.ca_edit.text().strip(),
            control_username=self.control_username_edit.text().strip(),
            inno_compiler=self.inno_edit.text().strip(),
            remote_host=self.remote_host_edit.text().strip(),
            remote_path=self.remote_path_edit.text().strip(),
            identity_file=self.identity_edit.text().strip(),
            channel=str(self.channel_combo.currentData()),
            build_portable=self.portable_check.isChecked(),
            build_output_mode=str(self.output_mode_combo.currentData()),
            build_windows=self.windows_check.isChecked(),
            build_uos=self.uos_check.isChecked(),
            windows_build_mode=str(self.windows_build_mode_combo.currentData()),
            github_remote=self.github_remote_edit.text().strip() or "origin",
            github_repo=self.github_repo_edit.text().strip(),
            gitee_url=self.gitee_url_edit.text().strip(),
            uos_builder_host=self.uos_builder_host_edit.text().strip(),
            uos_builder_path=self.uos_builder_path_edit.text().strip(),
        )
        self.settings_store.save(settings)

    def _refresh_project_version(self) -> None:
        try:
            version = project_version(self.repo_root)
        except PublisherError as exc:
            self.current_version_value.setText("读取失败")
            self._append_log(f"错误：{exc}")
            return
        self.current_version_value.setText(f"v{version}")
        if not self.version_edit.text().strip():
            self.version_edit.setText(version)
        self._refresh_release_readiness()

    def _refresh_release_readiness(self, *_args) -> None:
        readiness = release_readiness(self._options())
        self.release_readiness_label.setText(readiness.message)
        self.release_readiness_label.setProperty("state", readiness.state)
        self.release_readiness_label.style().unpolish(
            self.release_readiness_label
        )
        self.release_readiness_label.style().polish(self.release_readiness_label)

    def _options(self) -> ReleaseOptions:
        return ReleaseOptions(
            repo_root=self.repo_root,
            version=self.version_edit.text().strip(),
            base_url=self.base_url_edit.text().strip(),
            notes=self.notes_edit.toPlainText().strip(),
            ca_bundle=self.ca_edit.text().strip(),
            delta_from_version=self.delta_edit.text().strip(),
            uos_delta_from_version=self.uos_delta_edit.text().strip(),
            channel=str(self.channel_combo.currentData()),
            mandatory=self.mandatory_check.isChecked(),
            build_portable=self.portable_check.isChecked(),
            build_output_mode=str(self.output_mode_combo.currentData()),
            inno_compiler=self.inno_edit.text().strip(),
            remote_host=self.remote_host_edit.text().strip(),
            remote_path=self.remote_path_edit.text().strip(),
            identity_file=self.identity_edit.text().strip(),
            build_windows=self.windows_check.isChecked(),
            build_uos=self.uos_check.isChecked(),
            windows_build_mode=str(self.windows_build_mode_combo.currentData()),
            github_remote=self.github_remote_edit.text().strip() or "origin",
            github_repo=self.github_repo_edit.text().strip(),
            uos_builder_host=self.uos_builder_host_edit.text().strip(),
            uos_builder_path=self.uos_builder_path_edit.text().strip(),
        )

    def _validate(
        self,
        *,
        for_build: bool = False,
        for_publish: bool = False,
        for_pipeline: bool = False,
    ) -> ReleaseOptions | None:
        options = self._options()
        errors = validate_release_options(
            options,
            for_build=for_build,
            for_publish=for_publish,
            for_pipeline=for_pipeline,
        )
        if errors:
            message = "\n".join(f"• {item}" for item in errors)
            self._append_log("检查未通过：\n" + message)
            QMessageBox.warning(self, "检查未通过", message)
            return None
        return options

    def _preflight(self) -> None:
        options = self._validate(for_build=True)
        if options is None:
            return
        self._save_settings()
        if options.build_windows and options.build_uos:
            platform_label = "Windows x64 + UOS ARM64"
        else:
            platform_label = "Windows x64" if options.build_windows else "UOS ARM64"
        message = (
            f"环境检查通过。\n\n目标版本：{options.version}\n"
            f"构建平台：{platform_label}\n"
            f"输出类型：{self.output_mode_combo.currentText()}\n"
            f"通道：{options.channel}\n"
            f"Windows 增量来源：{options.delta_from_version or '无'}\n"
            f"UOS 增量来源：{options.uos_delta_from_version or '无'}\n"
            f"远程主机：{options.remote_host or '仅本地'}"
        )
        self._append_log(message)
        QMessageBox.information(self, "环境检查", message)

    def _run_tests(self) -> None:
        errors = validate_test_environment(self.repo_root)
        if errors:
            message = "\n".join(f"• {item}" for item in errors)
            self._append_log("测试环境检查未通过：\n" + message)
            QMessageBox.warning(self, "测试环境检查未通过", message)
            return
        options = self._options()
        self._save_settings()
        self._run_steps(
            build_release_plan(
                options,
                include_tests=True,
                include_build=False,
                include_publish=False,
            )
        )

    def _run_build(self) -> None:
        options = self._validate(for_build=True)
        if options is None:
            return
        self._save_settings()
        self._run_steps(
            build_release_plan(
                options,
                include_tests=False,
                include_build=True,
                include_publish=False,
            )
        )

    def _import_windows_result(self) -> None:
        if not self.windows_check.isChecked():
            QMessageBox.warning(self, "未选择 Windows", "请先选择 Windows x64。")
            return
        selected, _ = QFileDialog.getOpenFileName(
            self,
            "选择 Windows 构建结果包",
            str(self.repo_root / "dist"),
            "Windows 构建结果 (windows-build-result*.zip);;ZIP 文件 (*.zip)",
        )
        if not selected:
            return
        try:
            build_portable = detect_windows_result_portable(selected)
        except ReleaseTaskError as exc:
            QMessageBox.warning(self, "无法识别 Windows 结果", str(exc))
            return
        self.portable_check.setChecked(build_portable)
        portable_status = "包含便携包" if build_portable else "不含便携包"
        self._append_log(
            f"已自动识别 Windows 结果：{portable_status}，界面选项已同步。"
        )
        options = self._validate()
        if options is None:
            return
        self._save_settings()
        self._run_steps(
            build_windows_result_import_steps(options, selected),
            completion_message="Windows 构建包已导入并校验",
        )

    def _import_uos_result(self) -> None:
        if not self.uos_check.isChecked():
            QMessageBox.warning(
                self,
                "未选择统信",
                "请先选择统信 UOS ARM64。",
            )
            return
        selected, _ = QFileDialog.getOpenFileName(
            self,
            "选择统信构建结果包",
            str(self.repo_root / "dist"),
            "统信构建结果 (uos-build-result*.zip);;ZIP 文件 (*.zip)",
        )
        if not selected:
            return
        options = self._validate()
        if options is None:
            return
        self._save_settings()
        self._run_steps(
            build_uos_result_import_steps(options, selected),
            completion_message="统信构建包已导入并校验",
        )

    def _confirm_publish(self, options: ReleaseOptions) -> bool:
        destination = options.remote_host or "本地 dist/update-release"
        packages = ["Windows x64 完整包", "UOS ARM64 完整 DEB"]
        if options.delta_from_version:
            packages.append(f"Windows {options.delta_from_version} 增量包")
        if options.uos_delta_from_version:
            packages.append(f"UOS {options.uos_delta_from_version} 增量包")
        package = " + ".join(packages)
        warning = "是（在线检查到后不可忽略）" if options.mandatory else "否"
        reply = QMessageBox.warning(
            self,
            "确认发布",
            f"即将发布版本 v{options.version}\n\n"
            f"目标：{destination}\n"
            f"通道：{options.channel}\n"
            f"包类型：{package}\n"
            f"强制更新：{warning}\n\n"
            "大文件会使用 SFTP 断点续传，网络中断时自动重试；重新点击发布也会"
            "继续已有断点。发布清单会在安装包校验完成后原子替换。确定继续吗？",
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        return reply == QMessageBox.Yes

    def _run_publish(self) -> None:
        options = self._validate(for_publish=True)
        if options is None or not self._confirm_publish(options):
            return
        self._save_settings()
        self._run_steps(
            build_release_plan(
                options,
                include_tests=False,
                include_build=False,
                include_publish=True,
            )
        )

    def _run_pause_distribution(self) -> None:
        options = self._options()
        errors = validate_pause_distribution_options(options)
        if errors:
            message = "\n".join(f"• {item}" for item in errors)
            self._append_log("暂停分发检查未通过：\n" + message)
            QMessageBox.warning(self, "无法暂停分发", message)
            return
        reply = QMessageBox.warning(
            self,
            "确认暂停分发",
            f"即将暂停远程更新分发\n\n"
            f"目标：{options.remote_host}:{options.remote_path}\n"
            f"通道：{options.channel}\n\n"
            "新的更新检查将不再收到该通道版本；服务器上的安装包不会删除，"
            "当前活动清单会完整归档。已经取得清单或正在下载的客户端不会被"
            "强制中断。\n\n"
            "以后发布更高版本时会自动恢复该通道分发。确定继续吗？",
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if reply != QMessageBox.Yes:
            return
        self._save_settings()
        self._run_steps(
            build_pause_distribution_steps(options),
            completion_message=f"{options.channel} 通道已暂停分发",
        )

    def _run_full_pipeline(self) -> None:
        options = self._validate(for_build=True, for_pipeline=True)
        if options is None:
            return
        if not self._confirm_publish(options):
            return
        self._save_settings()
        self._run_steps(
            build_release_plan(
                options,
                include_tests=True,
                include_build=True,
                include_publish=True,
            )
        )

    def _sync_project_version(self) -> None:
        target = self.version_edit.text().strip()
        reply = QMessageBox.warning(
            self,
            "同步项目版本号",
            f"将把客户端、服务端、安装器及构建脚本版本统一更新为 "
            f"{target}。\n\n此操作要求 Git 工作区干净，完成后请检查并提交"
            "版本变更。是否继续？",
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if reply != QMessageBox.Yes:
            return
        try:
            paths = set_project_version(self.repo_root, target)
        except PublisherError as exc:
            QMessageBox.warning(self, "同步失败", str(exc))
            self._append_log(f"版本同步失败：{exc}")
            return
        self._refresh_project_version()
        relative = [str(path.relative_to(self.repo_root)) for path in paths]
        self._append_log(
            f"项目版本已同步为 {target}，修改 {len(paths)} 个文件：\n"
            + "\n".join(f"  {item}" for item in relative)
        )
        QMessageBox.information(
            self,
            "版本已同步",
            f"已更新为 {target}，共修改 {len(paths)} 个文件。\n"
            "请检查变更、更新发布说明并提交 Git 后再远程发布。",
        )

    @staticmethod
    def _git_changes_preview(changes: list[str], limit: int = 20) -> str:
        visible = changes[:limit]
        preview = "\n".join(f"  {line}" for line in visible)
        remaining = len(changes) - len(visible)
        if remaining > 0:
            preview += f"\n  ……另有 {remaining} 项变更"
        return preview

    def _commit_changes(self) -> None:
        try:
            changes = git_status(self.repo_root)
        except PublisherError as exc:
            QMessageBox.warning(self, "无法读取 Git 变更", str(exc))
            self._append_log(f"读取 Git 变更失败：{exc}")
            return
        if not changes:
            self._append_log("Git 工作区没有可提交的变更。")
            QMessageBox.information(
                self,
                "没有变更",
                "Git 工作区是干净的，没有需要提交的变更。",
            )
            return

        preview = self._git_changes_preview(changes)
        try:
            version = project_version(self.repo_root)
            default_message = f"准备 v{version} 发布"
        except PublisherError:
            default_message = "提交打包变更"
        message, accepted = QInputDialog.getText(
            self,
            "提交当前变更",
            f"检测到 {len(changes)} 项 Git 变更：\n\n{preview}\n\n提交说明：",
            QLineEdit.Normal,
            default_message,
        )
        message = message.strip()
        if not accepted:
            return
        if not message:
            QMessageBox.warning(self, "无法提交", "Git 提交说明不能为空。")
            return

        reply = QMessageBox.warning(
            self,
            "确认本地提交",
            f"提交说明：{message}\n\n"
            f"将提交以下变更：\n{preview}\n\n"
            "继续后会执行 git add --all，把全部修改、删除和未跟踪文件"
            "加入本地提交。\n此操作不会推送到远程，是否继续？",
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if reply != QMessageBox.Yes:
            return
        try:
            steps = build_git_commit_steps(self.repo_root, message)
        except PublisherError as exc:
            QMessageBox.warning(self, "无法提交", str(exc))
            self._append_log(f"准备 Git 提交失败：{exc}")
            return
        self._run_steps(
            steps,
            completion_message="本地 Git 提交已创建（未推送）",
        )

    def _push_changes(self) -> None:
        gitee_url = self.gitee_url_edit.text().strip()
        if not gitee_url:
            QMessageBox.warning(
                self,
                "缺少 Gitee 仓库",
                "请先填写 Gitee 仓库链接。",
            )
            return
        try:
            plans = build_git_mirror_push_plans(
                self.repo_root,
                remote_urls={"gitee": gitee_url},
            )
        except PublisherError as exc:
            QMessageBox.warning(self, "无法推送", str(exc))
            self._append_log(f"准备 Git 推送失败：{exc}")
            return
        pending = [
            plan
            for plan in plans
            if plan.ahead_count > 0 or plan.setup_step is not None
        ]
        if not pending:
            targets = "、".join(plan.target for plan in plans)
            self._append_log(
                f"Git 分支 {plans[0].branch} 已与 {targets} 同步。"
            )
            QMessageBox.information(
                self,
                "无需推送",
                f"当前分支 {plans[0].branch} 已与 GitHub 和 Gitee 同步。",
            )
            return

        previews = []
        for plan in plans:
            commits = "\n".join(f"    {commit}" for commit in plan.commits)
            remaining = plan.ahead_count - len(plan.commits)
            if remaining > 0:
                commits += f"\n    ……另有 {remaining} 个较早提交"
            previews.append(
                f"{plan.target}：{plan.ahead_count} 个待推送提交"
                + (f"\n{commits}" if commits else "")
            )
        setup_notice = (
            "\n点击继续后会新增或更新本仓库的 gitee 远程地址。"
            if any(plan.setup_step is not None for plan in plans)
            else ""
        )
        reply = QMessageBox.warning(
            self,
            "确认双仓库推送",
            f"本地分支：{plans[0].branch}\n\n"
            + "\n\n".join(previews)
            + f"\n\nGitee：{gitee_url}"
            + setup_notice
            + "\n\n将依次执行普通 Git push 到 GitHub 与 Gitee，"
            "不会强制推送。任一远程拒绝时会停止并保留清晰日志。是否继续？",
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if reply != QMessageBox.Yes:
            return
        steps = []
        for plan in pending:
            if plan.setup_step is not None:
                steps.append(plan.setup_step)
            if plan.ahead_count > 0 or plan.setup_step is not None:
                steps.append(plan.step)
        self._save_settings()
        self._run_steps(
            steps,
            completion_message="当前分支已同步推送到 GitHub 与 Gitee",
        )

    def _run_steps(
        self,
        steps: list[CommandStep],
        *,
        completion_message: str = "全部步骤执行成功",
    ) -> None:
        if self.process is not None:
            return
        if not steps:
            return
        self._steps = deque(steps)
        self._total_steps = len(steps)
        self._completed_steps = 0
        self._cancel_requested = False
        self._completion_message = completion_message
        self._set_busy(True)
        self._append_log(
            "\n"
            + "=" * 72
            + f"\n开始执行，共 {self._total_steps} 个步骤。"
        )
        self._start_next_step()

    def _start_next_step(self) -> None:
        if self._cancel_requested:
            self._finish_pipeline(False, "操作已停止")
            return
        if not self._steps:
            self._finish_pipeline(True, self._completion_message)
            return
        step = self._steps.popleft()
        self._current_step = step
        number = self._completed_steps + 1
        self.status_label.setText(
            f"步骤 {number}/{self._total_steps}：{step.title}"
        )
        self._append_log(
            f"\n[{number}/{self._total_steps}] {step.title}\n"
            f"> {step.display_command()}"
        )

        process = QProcess(self)
        process.setWorkingDirectory(str(step.working_directory))
        process.setProcessChannelMode(QProcess.MergedChannels)
        environment = QProcessEnvironment.systemEnvironment()
        environment.insert("PYTHONUTF8", "1")
        environment.insert("PYTHONIOENCODING", "utf-8")
        process.setProcessEnvironment(environment)
        process.readyReadStandardOutput.connect(self._read_process_output)
        process.finished.connect(self._process_finished)
        process.errorOccurred.connect(self._process_error)
        self.process = process
        self._output_buffer = b""
        process.start(step.program, list(step.arguments))

    def _decode_output(self, payload: bytes, *, final: bool = False) -> str:
        payload = self._output_buffer + payload
        self._output_buffer = b""
        if not payload:
            return ""
        try:
            return payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            if (
                not final
                and exc.end == len(payload)
                and exc.reason == "unexpected end of data"
            ):
                self._output_buffer = payload[exc.start:]
                return payload[: exc.start].decode("utf-8")
        encodings = ["utf-8", locale.getpreferredencoding(False), "gb18030"]
        for encoding in dict.fromkeys(encodings):
            try:
                return payload.decode(encoding)
            except (UnicodeDecodeError, LookupError):
                continue
        return payload.decode("utf-8", errors="replace")

    def _read_process_output(self, *, final: bool = False) -> None:
        if self.process is None:
            return
        payload = bytes(self.process.readAllStandardOutput())
        text = self._decode_output(payload, final=final)
        if text:
            self._append_log(text, add_newline=False)

    def _process_finished(
        self,
        exit_code: int,
        _exit_status: QProcess.ExitStatus,
    ) -> None:
        sender = self.sender()
        if self.process is None or sender is not self.process:
            return
        self._read_process_output(final=True)
        step = self._current_step
        process = self.process
        self.process = None
        self._current_step = None
        if process is not None:
            process.deleteLater()
        if self._cancel_requested:
            self._finish_pipeline(False, "操作已停止")
            return
        if exit_code != 0:
            title = step.title if step is not None else "当前步骤"
            if step is not None and step.key == "build_windows_github" and exit_code == 20:
                self._finish_pipeline(
                    False,
                    "GitHub 暂不可用，Windows 真机构建任务包已经保留；"
                    "完成真机构建后点击“导入 Windows 构建包”继续",
                    warning=True,
                )
                return
            self._finish_pipeline(False, f"{title}失败，退出码 {exit_code}")
            return
        self._completed_steps += 1
        QTimer.singleShot(0, self._start_next_step)

    def _process_error(self, error: QProcess.ProcessError) -> None:
        sender = self.sender()
        if self.process is None or sender is not self.process:
            return
        if error == QProcess.FailedToStart:
            step = self._current_step
            name = step.program if step else "命令"
            self._append_log(f"\n无法启动：{name}")
            process = self.process
            self.process = None
            self._current_step = None
            if process is not None:
                process.deleteLater()
            self._finish_pipeline(False, f"无法启动：{name}")

    def _cancel(self) -> None:
        if self.process is None:
            return
        reply = QMessageBox.question(
            self,
            "停止当前操作",
            "确定停止当前步骤及后续流程吗？\n"
            "已经上传或生成的文件不会被自动删除；远程上传断点会保留，"
            "下次发布可继续。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        self._cancel_requested = True
        self.status_label.setText("正在停止…")
        process = self.process
        process_id = int(process.processId())
        if os.name == "nt" and process_id > 0:
            QProcess.startDetached(
                "taskkill.exe",
                ["/PID", str(process_id), "/T", "/F"],
            )
        else:
            process.terminate()
        QTimer.singleShot(
            3000,
            lambda current=process: (
                current.kill()
                if current is self.process
                and current.state() != QProcess.NotRunning
                else None
            ),
        )

    def _finish_pipeline(
        self,
        success: bool,
        message: str,
        *,
        warning: bool = False,
    ) -> None:
        self._steps.clear()
        self._current_step = None
        self._completion_message = "全部步骤执行成功"
        self._set_busy(False)
        self._refresh_release_readiness()
        self.status_label.setText(message)
        self._append_log(
            f"\n{'成功' if success else '失败'}：{message}\n" + "=" * 72
        )
        if success:
            QMessageBox.information(self, "执行完成", message)
        elif warning and not self._cancel_requested:
            QMessageBox.warning(self, "需要 Windows 真机构建", message)
        elif not self._cancel_requested:
            QMessageBox.critical(self, "执行失败", message)

    def _set_busy(self, busy: bool) -> None:
        for button in (
            self.preflight_button,
            self.test_button,
            self.build_button,
            self.import_windows_result_button,
            self.import_uos_result_button,
            self.commit_changes_button,
            self.push_button,
            self.pause_distribution_button,
            self.publish_button,
            self.pipeline_button,
            self.sync_version_button,
            self.refresh_clients_button,
            self.disconnect_client_button,
            self.restore_client_button,
            self.disconnect_all_button,
            self.restore_all_button,
        ):
            button.setEnabled(not busy)
        self.cancel_button.setEnabled(busy)
        if not busy:
            self._connection_selection_changed()
            self._platform_selection_changed()

    def _append_log(self, text: str, *, add_newline: bool = True) -> None:
        cursor = self.log_edit.textCursor()
        cursor.movePosition(cursor.End)
        cursor.insertText(text + ("\n" if add_newline else ""))
        self.log_edit.setTextCursor(cursor)
        self.log_edit.ensureCursorVisible()

    def _open_output(self) -> None:
        output = self.repo_root / "dist"
        output.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(output)))

    def closeEvent(self, event) -> None:
        if self.process is not None:
            QMessageBox.warning(
                self,
                "操作正在执行",
                "请先停止当前测试、构建或发布流程。",
            )
            event.ignore()
            return
        try:
            self._save_settings()
        except OSError:
            pass
        event.accept()

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow, QWidget {
                background: #f5f7fa;
                color: #1f2937;
                font-family: "Microsoft YaHei UI";
                font-size: 13px;
            }
            QLabel#Title {
                font-size: 24px;
                font-weight: 700;
                color: #102a43;
            }
            QLabel#Muted {
                color: #6b7c93;
            }
            QLabel#PathBadge {
                background: #e9eef5;
                border-radius: 6px;
                padding: 7px 10px;
                color: #486581;
            }
            QLabel#VersionValue {
                color: #1565c0;
                font-size: 17px;
                font-weight: 700;
            }
            QLabel#SectionTitle {
                font-size: 15px;
                font-weight: 700;
                color: #243b53;
            }
            QLabel#Status {
                background: #eef4fb;
                border-radius: 6px;
                padding: 8px 10px;
                color: #245b8a;
            }
            QLabel#ReleaseReadiness {
                background: #eef4fb;
                border-radius: 6px;
                padding: 8px 10px;
                color: #486581;
            }
            QLabel#ReleaseReadiness[state="ready"] {
                background: #e8f7ef;
                color: #176b45;
                font-weight: 700;
            }
            QLabel#ReleaseReadiness[state="partial"],
            QLabel#ReleaseReadiness[state="mismatch"] {
                background: #fff5df;
                color: #8a5a00;
            }
            QLabel#ReleaseReadiness[state="published"] {
                background: #edf2f7;
                color: #52606d;
            }
            QLabel#ActionGroupTitle {
                color: #52606d;
                font-weight: 700;
                min-width: 72px;
            }
            QFrame#Card, QFrame#LogCard, QFrame#ActionCard {
                background: white;
                border: 1px solid #d9e2ec;
                border-radius: 9px;
            }
            QLineEdit, QPlainTextEdit, QComboBox {
                background: white;
                border: 1px solid #bcccdc;
                border-radius: 6px;
                padding: 7px 8px;
                selection-background-color: #2f80ed;
            }
            QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus {
                border-color: #2f80ed;
            }
            QPlainTextEdit#Log {
                background: #101820;
                color: #d9e7f2;
                border: none;
                font-family: Consolas, "Microsoft YaHei UI";
                font-size: 12px;
            }
            QPushButton {
                background: #edf2f7;
                border: 1px solid #cbd5e0;
                border-radius: 6px;
                padding: 8px 13px;
            }
            QPushButton:hover {
                background: #e2e8f0;
            }
            QPushButton:disabled {
                color: #9aa5b1;
                background: #f1f3f5;
            }
            QPushButton#PrimaryButton {
                background: #1769aa;
                color: white;
                border-color: #1769aa;
                font-weight: 700;
            }
            QPushButton#PrimaryButton:hover {
                background: #12588f;
            }
            QPushButton#DangerButton {
                color: #b42318;
            }
            QScrollArea {
                background: transparent;
            }
            """
        )

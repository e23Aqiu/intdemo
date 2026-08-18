from __future__ import annotations

import base64
import os
import uuid
from pathlib import Path

from PyQt5.QtCore import QObject, Qt, QTimer, pyqtSignal
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..captcha_models import (
    BUILTIN_MODEL_VERSION,
    CaptchaTrainingError,
    inspect_training_dataset,
    train_candidate,
)
from ..enhanced_training import train_enhanced_candidate
from ..online.api import ApiResponseError
from ..trainer_component import (
    STATUS_AVAILABLE,
    STATUS_DAMAGED,
    STATUS_INCOMPATIBLE,
    TrainerComponentError,
    TrainerComponentManager,
)
from .announcement_page import ImagePreviewDialog, start_api_task
from .file_dialogs import SystemFileDialog as QFileDialog
from .frameless import FramelessMessageBox as QMessageBox
from .training_terminal_dialog import TrainingTerminalDialog

MAX_IMPORT_BYTES = 100 * 1024 * 1024
UPLOAD_MODES = (
    ("关闭", "off"),
    ("仅统计", "metrics_only"),
    ("采集样本并统计", "samples_and_metrics"),
)
UPLOAD_MODE_LABELS = {mode: label for label, mode in UPLOAD_MODES}
TRAINING_MODES = (
    ("标准模式", "standard"),
    ("强化模式", "enhanced"),
)
TRAINING_METHODS = {
    "standard": "OpenCV HOG + 线性 SVM（hog-linear-svm-v1）",
    "enhanced": "Tiny CNN + ONNX（tiny-cnn-onnx-v1）",
}
ACTIVATABLE_MODEL_ALGORITHMS = frozenset(
    {"hog-linear-svm-v1", "tiny-cnn-onnx-v1"}
)
RETIRED_KNN_ALGORITHM = "knn-pixels-v1"
TRAINING_ALGORITHM_BY_MODE = {
    "standard": "hog-linear-svm-v1",
    "enhanced": "tiny-cnn-onnx-v1",
}


class TrainingProgressSignals(QObject):
    event_received = pyqtSignal(str, object)


def _format_size(size):
    size = max(0, int(size or 0))
    if size >= 1024 * 1024 * 1024:
        return f"{size / (1024 * 1024 * 1024):.2f} GB"
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    if size >= 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size} B"


def _display_time(value):
    return str(value or "").replace("T", " ")[:19] or "-"


def _sample_image_extension(sample, image_data):
    """Choose a useful save-dialog extension for current and legacy rows."""
    if image_data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if image_data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    mime = str(sample.get("image_mime") or "")
    mime = mime.split(";", 1)[0].strip().lower()
    if mime in {"image/jpeg", "image/jpg"}:
        return ".jpg"
    if mime == "image/png":
        return ".png"
    return ".png"


class MachineLearningPage(QWidget):
    REFRESH_INTERVAL_MS = 5_000
    MANUAL_MODEL_PREFIXES = ("human-", "human_")

    def __init__(
        self,
        session_manager,
        learning_service=None,
        parent=None,
        trainer_manager=None,
    ):
        super().__init__(parent)
        self.session_manager = session_manager
        self.learning_service = learning_service
        if trainer_manager is not None:
            self.trainer_manager = trainer_manager
        else:
            self.trainer_manager = TrainerComponentManager()
        self._tasks = []
        self._task_callbacks = {}
        self._refresh_task = None
        self._sample_refresh_task = None
        self._sample_refresh_pending = False
        self._sample_image_loading = False
        self._shutting_down = False
        self._loading_policy = False
        self._loading_training_mode = False
        self._component_task_running = False
        self._training_in_progress = False
        self._training_id = ""
        self._training_terminal = None
        self._training_progress_signals = TrainingProgressSignals(self)
        self._training_progress_signals.event_received.connect(
            self._training_progress_received
        )
        self._server_capabilities_loaded = False
        self._server_model_algorithms = {}
        self._confirmed_upload_mode = "off"
        self._model_rows = []
        self._all_model_rows = []
        self._model_source_rows = []
        self._model_attempts = []
        self._model_active = {}
        self._selected_model_key = None
        self._sample_rows = []
        self._sample_offset = 0
        self._sample_limit = 100
        self._samples_loaded_once = False
        self._build_ui()
        self._refresh_training_component()
        if self.learning_service is not None:
            self.learning_service.model_changed.connect(
                self._active_model_ready
            )

        self.refresh_timer = QTimer(self)
        self.refresh_timer.setInterval(self.REFRESH_INTERVAL_MS)
        self.refresh_timer.timeout.connect(self._poll_refresh)
        self.refresh_timer.start()
        QTimer.singleShot(0, self.refresh)

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 24)
        root.setSpacing(14)

        heading = QHBoxLayout()
        title_block = QVBoxLayout()
        title = QLabel("机器学习")
        title.setObjectName("PageTitle")
        subtitle = QLabel(
            "管理经授权的验证码成功样本、候选模型和客户端应用版本。"
        )
        subtitle.setObjectName("Muted")
        subtitle.setWordWrap(True)
        title_block.addWidget(title)
        title_block.addWidget(subtitle)
        heading.addLayout(title_block, 1)
        self.refresh_btn = QPushButton("刷新")
        self.refresh_btn.clicked.connect(self.refresh)
        heading.addWidget(self.refresh_btn)
        root.addLayout(heading)

        policy_card = QFrame()
        policy_card.setObjectName("Card")
        policy_layout = QHBoxLayout(policy_card)
        policy_layout.setContentsMargins(18, 14, 18, 14)
        policy_text = QVBoxLayout()
        policy_title = QLabel("客户端数据上报策略")
        policy_title.setObjectName("SectionTitle")
        self.policy_detail = QLabel("默认关闭；正在读取服务器策略")
        self.policy_detail.setObjectName("Muted")
        self.policy_detail.setWordWrap(True)
        policy_text.addWidget(policy_title)
        policy_text.addWidget(self.policy_detail)
        policy_layout.addLayout(policy_text, 1)
        self.upload_mode_combo = QComboBox()
        for label, mode in UPLOAD_MODES:
            self.upload_mode_combo.addItem(label, mode)
        self.upload_mode_combo.setMinimumWidth(180)
        self.upload_mode_combo.currentIndexChanged.connect(
            self._change_policy
        )
        policy_layout.addWidget(self.upload_mode_combo)
        root.addWidget(policy_card)

        self.trainer_card = QFrame()
        self.trainer_card.setObjectName("Card")
        trainer_layout = QVBoxLayout(self.trainer_card)
        trainer_layout.setContentsMargins(18, 14, 18, 14)
        trainer_text = QVBoxLayout()
        trainer_title = QLabel("本机训练方式")
        trainer_title.setObjectName("SectionTitle")
        self.training_method_label = QLabel("正在读取训练组件状态")
        self.training_method_label.setObjectName("Muted")
        self.training_method_label.setWordWrap(True)
        self.trainer_status_label = QLabel("--")
        self.trainer_status_label.setObjectName("Muted")
        self.trainer_status_label.setWordWrap(True)
        trainer_text.addWidget(trainer_title)
        trainer_text.addWidget(self.training_method_label)
        trainer_text.addWidget(self.trainer_status_label)
        trainer_layout.addLayout(trainer_text)
        trainer_controls = QHBoxLayout()
        trainer_controls.addStretch()
        self.training_mode_combo = QComboBox()
        for label, mode in TRAINING_MODES:
            self.training_mode_combo.addItem(label, mode)
        self.training_mode_combo.setMinimumWidth(120)
        self.training_mode_combo.currentIndexChanged.connect(
            self._change_training_mode
        )
        trainer_controls.addWidget(self.training_mode_combo)
        self.install_trainer_btn = QPushButton("安装强化组件")
        self.install_trainer_btn.clicked.connect(self._install_trainer_component)
        self.self_test_trainer_btn = QPushButton("组件自检")
        self.self_test_trainer_btn.clicked.connect(self._self_test_trainer_component)
        self.uninstall_trainer_btn = QPushButton("卸载强化组件")
        self.uninstall_trainer_btn.setObjectName("DangerButton")
        self.uninstall_trainer_btn.clicked.connect(
            self._uninstall_trainer_component
        )
        trainer_controls.addWidget(self.install_trainer_btn)
        trainer_controls.addWidget(self.self_test_trainer_btn)
        trainer_controls.addWidget(self.uninstall_trainer_btn)
        trainer_layout.addLayout(trainer_controls)
        root.addWidget(self.trainer_card)

        stats_layout = QGridLayout()
        stats_layout.setHorizontalSpacing(12)
        stats_layout.setVerticalSpacing(12)
        self.total_count_value = self._add_metric(
            stats_layout,
            0,
            0,
            "数据集样本",
        )
        self.total_size_value = self._add_metric(
            stats_layout,
            0,
            1,
            "占用空间",
        )
        self.numeric_count_value = self._add_metric(
            stats_layout,
            0,
            2,
            "数字验证码",
        )
        self.click_count_value = self._add_metric(
            stats_layout,
            0,
            3,
            "文字点选",
        )
        self.numeric_current_value = self._add_metric(
            stats_layout,
            1,
            0,
            "数字当前模型准确率",
        )
        self.numeric_candidate_value = self._add_metric(
            stats_layout,
            1,
            1,
            "数字候选模型",
        )
        self.click_current_value = self._add_metric(
            stats_layout,
            1,
            2,
            "文字点选当前模型准确率",
        )
        self.click_candidate_value = self._add_metric(
            stats_layout,
            1,
            3,
            "点选候选模型",
        )
        root.addLayout(stats_layout)

        actions = QHBoxLayout()
        self.export_btn = QPushButton("导出数据集")
        self.export_btn.setToolTip(
            "导出一个 ZIP，顶层按 numeric 和 click 分类"
        )
        self.export_btn.clicked.connect(self._export_dataset)
        self.import_btn = QPushButton("导入数据集")
        self.import_btn.setToolTip(
            "支持重新导入原格式分类包以及旧版数据集 ZIP"
        )
        self.import_btn.clicked.connect(self._import_dataset)
        self.train_numeric_btn = QPushButton("训练数字候选模型")
        self.train_numeric_btn.setObjectName("PrimaryButton")
        self.train_numeric_btn.clicked.connect(
            lambda: self._train_model("numeric")
        )
        self.train_click_btn = QPushButton("训练点选候选模型")
        self.train_click_btn.setObjectName("PrimaryButton")
        self.train_click_btn.clicked.connect(lambda: self._train_model("click"))
        self.show_training_terminal_btn = QPushButton("查看训练终端")
        self.show_training_terminal_btn.setEnabled(False)
        self.show_training_terminal_btn.clicked.connect(
            self._show_training_terminal
        )
        actions.addWidget(self.export_btn)
        actions.addWidget(self.import_btn)
        actions.addStretch()
        actions.addWidget(self.show_training_terminal_btn)
        actions.addWidget(self.train_numeric_btn)
        actions.addWidget(self.train_click_btn)
        root.addLayout(actions)

        sample_header = QHBoxLayout()
        sample_title = QLabel("已采集样本")
        sample_title.setObjectName("SectionTitle")
        sample_header.addWidget(sample_title)
        self.sample_hint = QLabel(
            "双击样本可查看图片；可用 Ctrl 或 Shift 多选后删除"
        )
        self.sample_hint.setObjectName("Muted")
        sample_header.addWidget(self.sample_hint)
        sample_header.addStretch()
        self.sample_type_combo = QComboBox()
        self.sample_type_combo.addItem("全部类型", None)
        self.sample_type_combo.addItem("数字验证码", "numeric")
        self.sample_type_combo.addItem("文字点选验证码", "click")
        self.sample_type_combo.currentIndexChanged.connect(
            self._sample_filter_changed
        )
        self.prev_samples_btn = QPushButton("上一页")
        self.prev_samples_btn.clicked.connect(self._previous_samples_page)
        self.next_samples_btn = QPushButton("下一页")
        self.next_samples_btn.clicked.connect(self._next_samples_page)
        self.sample_page_label = QLabel("--")
        self.sample_page_label.setObjectName("Muted")
        self.delete_samples_btn = QPushButton("删除所选样本")
        self.delete_samples_btn.setObjectName("DangerButton")
        self.delete_samples_btn.clicked.connect(self._delete_selected_samples)
        sample_header.addWidget(self.sample_type_combo)
        sample_header.addWidget(self.prev_samples_btn)
        sample_header.addWidget(self.sample_page_label)
        sample_header.addWidget(self.next_samples_btn)
        sample_header.addWidget(self.delete_samples_btn)
        root.addLayout(sample_header)

        self.sample_table = QTableWidget(0, 7)
        self.sample_table.setHorizontalHeaderLabels(
            [
                "样本 ID",
                "类型",
                "答案",
                "采集方式",
                "识别模型",
                "大小",
                "采集时间",
            ]
        )
        self.sample_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.sample_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.sample_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.sample_table.verticalHeader().setVisible(False)
        sample_table_header = self.sample_table.horizontalHeader()
        sample_table_header.setSectionResizeMode(QHeaderView.ResizeToContents)
        sample_table_header.setSectionResizeMode(2, QHeaderView.Stretch)
        sample_table_header.setSectionResizeMode(4, QHeaderView.Stretch)
        self.sample_table.setMinimumHeight(170)
        self.sample_table.setMaximumHeight(260)
        self.sample_table.cellDoubleClicked.connect(self._view_sample_image)
        root.addWidget(self.sample_table)

        model_header = QHBoxLayout()
        table_title = QLabel("模型版本")
        table_title.setObjectName("SectionTitle")
        model_header.addWidget(table_title)
        model_header.addStretch()
        self.model_type_combo = QComboBox()
        self.model_type_combo.addItem("全部类型", None)
        self.model_type_combo.addItem("数字验证码", "numeric")
        self.model_type_combo.addItem("文字点选验证码", "click")
        self.model_type_combo.currentIndexChanged.connect(
            self._model_filter_changed
        )
        model_header.addWidget(self.model_type_combo)
        self.recalculate_model_btn = QPushButton("刷新所选统计")
        self.recalculate_model_btn.setEnabled(False)
        self.recalculate_model_btn.clicked.connect(
            self._recalculate_selected_model
        )
        model_header.addWidget(self.recalculate_model_btn)
        root.addLayout(model_header)
        self.model_table = QTableWidget(0, 11)
        self.model_table.setHorizontalHeaderLabels(
            [
                "类型",
                "模型名称",
                "算法",
                "状态",
                "离线准确率",
                "自动识别准确率",
                "自动识别次数",
                "训练样本",
                "测试样本",
                "模型大小",
                "创建时间",
            ]
        )
        self.model_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.model_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.model_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.model_table.itemSelectionChanged.connect(
            self._model_selection_changed
        )
        self.model_table.verticalHeader().setVisible(False)
        header = self.model_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        root.addWidget(self.model_table, 1)

        model_actions = QHBoxLayout()
        self.model_hint = QLabel(
            "候选模型先在固定留出集评估；激活后客户端会校验 SHA-256 并应用。"
        )
        self.model_hint.setObjectName("Muted")
        self.model_hint.setWordWrap(True)
        model_actions.addWidget(self.model_hint, 1)
        self.use_builtin_btn = QPushButton("所选类型恢复内置")
        self.use_builtin_btn.clicked.connect(self._use_builtin_for_selected_type)
        self.delete_model_btn = QPushButton("删除所选模型")
        self.delete_model_btn.setObjectName("DangerButton")
        self.delete_model_btn.clicked.connect(self._delete_selected_model)
        self.rename_model_btn = QPushButton("重命名所选模型")
        self.rename_model_btn.setEnabled(False)
        self.rename_model_btn.clicked.connect(self._rename_selected_model)
        self.activate_model_btn = QPushButton("应用所选模型")
        self.activate_model_btn.setObjectName("PrimaryButton")
        self.activate_model_btn.setEnabled(False)
        self.activate_model_btn.clicked.connect(self._activate_selected_model)
        model_actions.addWidget(self.use_builtin_btn)
        model_actions.addWidget(self.rename_model_btn)
        model_actions.addWidget(self.delete_model_btn)
        model_actions.addWidget(self.activate_model_btn)
        root.addLayout(model_actions)

    @staticmethod
    def _add_metric(layout, row, column, label):
        card = QFrame()
        card.setObjectName("Card")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(15, 13, 15, 13)
        title = QLabel(label)
        title.setObjectName("Muted")
        value = QLabel("--")
        value.setObjectName("KpiValue")
        value.setWordWrap(True)
        card_layout.addWidget(title)
        card_layout.addWidget(value)
        layout.addWidget(card, row, column)
        return value

    def _set_component_controls_enabled(self, enabled):
        available = bool(enabled) and not self._training_in_progress
        self.training_mode_combo.setEnabled(available)
        self.install_trainer_btn.setEnabled(available)
        status = self.trainer_manager.status(verify_files=False)
        self.self_test_trainer_btn.setEnabled(available and status.available)
        self.uninstall_trainer_btn.setEnabled(
            available
            and (
                status.installed
                or bool(getattr(status, "cleanup_available", False))
            )
        )

    def _refresh_training_component(self):
        status = self.trainer_manager.status(verify_files=False)
        mode = self.trainer_manager.preferred_mode()
        self._loading_training_mode = True
        self.training_mode_combo.setCurrentIndex(
            max(0, self.training_mode_combo.findData(mode))
        )
        self._loading_training_mode = False
        enhanced_index = self.training_mode_combo.findData("enhanced")
        if enhanced_index >= 0:
            item = self.training_mode_combo.model().item(enhanced_index)
            if item is not None:
                item.setEnabled(status.available)

        self.training_method_label.setText(
            f"当前模式：{'强化模式' if mode == 'enhanced' else '标准模式'} · "
            f"当前方法：{TRAINING_METHODS[mode]}"
        )
        platform_labels = {
            "windows-x86_64": "Windows x64",
            "linux-aarch64": "统信 UOS ARM64",
        }
        if status.code == STATUS_AVAILABLE:
            details = [
                "强化组件：已安装",
                f"版本 {status.version or '-'}",
                platform_labels.get(status.platform, status.platform or "未知平台"),
                f"占用 {_format_size(status.installed_size)}",
            ]
            if status.last_self_test_at:
                details.append(
                    f"上次自检 {_display_time(status.last_self_test_at)}"
                )
            if getattr(status, "maintenance_required", False):
                details.append(
                    f"需要维护：{getattr(status, 'maintenance_message', '')}"
                )
            self.trainer_status_label.setText(" · ".join(details))
        elif status.code == STATUS_DAMAGED:
            self.trainer_status_label.setText(
                f"强化组件：已损坏或不完整 · {status.detail}"
            )
        elif status.code == STATUS_INCOMPATIBLE:
            self.trainer_status_label.setText(
                f"强化组件：不兼容 · {status.detail}"
            )
        else:
            message = (
                "强化组件：未安装；可由管理员选择本机对应平台的 "
                ".inttrainer 文件安装"
            )
            if getattr(status, "maintenance_required", False):
                message += (
                    " · 检测到组件残留："
                    f"{getattr(status, 'maintenance_message', '')}"
                )
            self.trainer_status_label.setText(message)
            self.install_trainer_btn.setToolTip("")
        self._set_component_controls_enabled(not self._component_task_running)

    def _change_training_mode(self, _index):
        if self._loading_training_mode:
            return
        mode = str(self.training_mode_combo.currentData() or "standard")
        try:
            self.trainer_manager.set_preferred_mode(mode)
        except (TrainerComponentError, OSError, ValueError) as exc:
            QMessageBox.warning(self, "无法切换训练模式", str(exc))
        self._refresh_training_component()

    def _install_trainer_component(self):
        source, _ = QFileDialog.getOpenFileName(
            self,
            "安装本机强化训练组件",
            "",
            "IntDemo 强化组件 (*.inttrainer)",
        )
        if not source:
            return
        self._component_task_running = True
        self._set_component_controls_enabled(False)

        def install():
            return self.trainer_manager.install(Path(source))

        task = self._start(install, self._trainer_component_installed)
        if task is None:
            self._component_task_running = False
            self._refresh_training_component()

    def _trainer_component_installed(self, result, error):
        self._component_task_running = False
        self._refresh_training_component()
        if error is not None:
            QMessageBox.warning(self, "强化组件安装失败", str(error))
            return
        status = result.status
        replaced = (
            f"，已替换版本 {result.replaced_version}"
            if result.replaced_version
            else ""
        )
        QMessageBox.information(
            self,
            "强化组件安装完成",
            f"已安装版本 {status.version}{replaced}。组件自检已通过。",
        )

    def _self_test_trainer_component(self):
        self._component_task_running = True
        self._set_component_controls_enabled(False)
        task = self._start(
            self.trainer_manager.self_test,
            self._trainer_component_tested,
        )
        if task is None:
            self._component_task_running = False
            self._refresh_training_component()

    def _trainer_component_tested(self, _result, error):
        self._component_task_running = False
        self._refresh_training_component()
        if error is not None:
            QMessageBox.warning(self, "强化组件自检失败", str(error))
            return
        QMessageBox.information(self, "强化组件自检", "组件自检通过。")

    def _uninstall_trainer_component(self):
        status = self.trainer_manager.status(verify_files=False)
        cleanup_only = not status.installed and bool(
            getattr(status, "cleanup_available", False)
        )
        reply = QMessageBox.question(
            self,
            "清理强化组件残留" if cleanup_only else "卸载强化组件",
            (
                "只会清理程序可确认的强化组件残留，不会删除验证码样本或模型。"
                if cleanup_only
                else "只会删除本机强化训练组件，不会删除验证码样本、候选模型或已训练的 "
                "ONNX 模型。卸载后训练模式自动回落为标准模式。"
            )
            + "\n\n是否继续？",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        self._component_task_running = True
        self._set_component_controls_enabled(False)
        task = self._start(
            self.trainer_manager.uninstall,
            self._trainer_component_uninstalled,
        )
        if task is None:
            self._component_task_running = False
            self._refresh_training_component()

    def _trainer_component_uninstalled(self, result, error):
        self._component_task_running = False
        self._refresh_training_component()
        if error is not None:
            QMessageBox.warning(self, "强化组件卸载失败", str(error))
            return
        QMessageBox.information(
            self,
            "强化组件已卸载",
            f"已释放 {_format_size(result)}；验证码样本和模型未受影响。",
        )

    def _start(self, function, completed):
        if self._shutting_down:
            return None
        task = start_api_task(function, self._task_finished)
        self._task_callbacks[id(task.signals)] = (task, completed)
        self._tasks.append(task)
        return task

    def _task_finished(self, result, error):
        entry = self._task_callbacks.pop(id(self.sender()), None)
        if entry is None:
            return
        task, completed = entry
        if task in self._tasks:
            self._tasks.remove(task)
        if self._shutting_down:
            return
        completed(result, error)

    def shutdown(self):
        """Stop polling and ignore results from work already in flight."""
        if self._shutting_down:
            return
        self._shutting_down = True
        self.refresh_timer.stop()
        self._refresh_task = None
        self._sample_refresh_task = None
        self._sample_refresh_pending = False
        self._sample_image_loading = False
        self._training_id = ""
        terminal = self._training_terminal
        if terminal is not None:
            terminal.hide()
            terminal.deleteLater()
            self._training_terminal = None
        self._task_callbacks.clear()
        self._tasks.clear()

    def closeEvent(self, event):
        self.shutdown()
        super().closeEvent(event)

    def refresh(self):
        if self._shutting_down or self._refresh_task is not None:
            return
        self.refresh_btn.setEnabled(False)

        def load():
            token = self.session_manager.access_token()
            return self.session_manager.api.admin_captcha_learning_overview(token)

        self._refresh_task = self._start(load, self._overview_loaded)

    def _poll_refresh(self):
        """Refresh visible overview data without rebuilding the sample table."""
        if not self.isVisible():
            return
        self.refresh()

    def _overview_loaded(self, result, error):
        self._refresh_task = None
        self.refresh_btn.setEnabled(True)
        if error is not None:
            self.policy_detail.setText(f"读取失败：{error}")
            return
        overview = result or {}
        capabilities = overview.get("supported_model_algorithms")
        self._server_capabilities_loaded = True
        self._server_model_algorithms = (
            {
                kind: frozenset(str(value) for value in values)
                for kind, values in capabilities.items()
                if kind in {"numeric", "click"} and isinstance(values, list)
            }
            if isinstance(capabilities, dict)
            else {}
        )
        policy = overview.get("policy") or {}
        dataset = overview.get("dataset") or {}
        models = overview.get("models") or []
        attempts = overview.get("attempts") or []

        upload_mode = str(policy.get("upload_mode") or "")
        if upload_mode not in UPLOAD_MODE_LABELS:
            upload_mode = (
                "samples_and_metrics"
                if policy.get("upload_enabled")
                else "off"
            )
        self._confirmed_upload_mode = upload_mode
        self._loading_policy = True
        self.upload_mode_combo.setCurrentIndex(
            max(0, self.upload_mode_combo.findData(upload_mode))
        )
        self._loading_policy = False
        state = {
            "off": "已关闭：不上传任何验证码数据",
            "metrics_only": (
                "仅统计：只统计自动模型的识别次数和准确率；"
                "人工操作不计入，不上传图片和答案"
            ),
            "samples_and_metrics": "采集样本并统计：成功时上传图片和答案",
        }[upload_mode]
        self.policy_detail.setText(
            f"{state} · 更新时间 {_display_time(policy.get('updated_at'))}"
        )

        self.total_count_value.setText(str(int(dataset.get("total_count") or 0)))
        self.total_size_value.setText(_format_size(dataset.get("total_bytes")))
        self.numeric_count_value.setText(
            f"{int(dataset.get('numeric_count') or 0)} · "
            f"{_format_size(dataset.get('numeric_bytes'))}"
        )
        self.click_count_value.setText(
            f"{int(dataset.get('click_count') or 0)} · "
            f"{_format_size(dataset.get('click_bytes'))}"
        )

        active = policy.get("active_models") or {}
        self._set_accuracy_values("numeric", active, attempts, models)
        self._set_accuracy_values("click", active, attempts, models)
        self._populate_models(models, attempts, active)
        if not self._samples_loaded_once:
            self._refresh_samples()

    @classmethod
    def _is_manual_model_version(cls, version):
        normalized = str(version or "").strip().lower()
        return normalized == "human" or normalized.startswith(
            cls.MANUAL_MODEL_PREFIXES
        )

    @classmethod
    def _is_automatic_metric(cls, metric):
        if not isinstance(metric, dict):
            return False
        return not bool(metric.get("assisted")) and not cls._is_manual_model_version(
            metric.get("model_version")
        )

    @classmethod
    def _attempt_metric(cls, attempts, captcha_type, model_version):
        """Aggregate automatic attempts for one type/version pair.

        The service normally returns one grouped row. Aggregating here also
        handles older deployments that returned multiple rows and makes the
        model selection action a real re-count instead of displaying a stale
        first match.
        """
        if cls._is_manual_model_version(model_version):
            return None
        total = 0
        success = 0
        found = False
        for item in attempts or []:
            if not cls._is_automatic_metric(item):
                continue
            if (
                str(item.get("captcha_type") or "") != str(captcha_type)
                or str(item.get("model_version") or "") != str(model_version)
            ):
                continue
            found = True
            item_total = max(0, int(item.get("attempt_count") or 0))
            item_success = max(
                0,
                min(item_total, int(item.get("success_count") or 0)),
            )
            total += item_total
            success += item_success
        if not found:
            return None
        return {
            "attempt_count": total,
            "success_count": success,
            "success_rate": success / total if total else 0.0,
        }

    @staticmethod
    def _metric_text(metric, version, display_name=None):
        label = str(display_name or version)
        if metric and int(metric.get("attempt_count") or 0):
            return (
                f"{float(metric.get('success_rate') or 0) * 100:.1f}%\n"
                f"{int(metric.get('success_count') or 0)} / "
                f"{int(metric.get('attempt_count') or 0)}\n"
                f"{label}"
            )
        return f"--\n暂无自动识别记录\n{label}"

    def _set_accuracy_values(self, captcha_type, active, attempts, models):
        active_model = active.get(captcha_type) or {}
        current_version = str(
            active_model.get("version") or BUILTIN_MODEL_VERSION
        )
        if self._is_manual_model_version(current_version):
            current_version = BUILTIN_MODEL_VERSION
        metric = self._attempt_metric(attempts, captcha_type, current_version)
        current_text = self._metric_text(
            metric,
            current_version,
            active_model.get("display_name"),
        )
        candidates = [
            model
            for model in models
            if model.get("captcha_type") == captcha_type
            and model.get("status") == "candidate"
            and not self._is_manual_model_version(model.get("version"))
        ]
        candidate = candidates[0] if candidates else None
        candidate_text = (
            f"{float(candidate.get('accuracy') or 0) * 100:.1f}%\n"
            f"{int(candidate.get('correct_count') or 0)} / "
            f"{int(candidate.get('test_count') or 0)}\n"
            f"{candidate.get('display_name') or candidate.get('version')}"
            if candidate
            else "--\n暂无候选模型"
        )
        if captcha_type == "numeric":
            self.numeric_current_value.setText(current_text)
            self.numeric_candidate_value.setText(candidate_text)
        else:
            self.click_current_value.setText(current_text)
            self.click_candidate_value.setText(candidate_text)

    def _populate_models(self, models, attempts, active):
        self._model_attempts = list(attempts or [])
        self._model_active = dict(active or {})
        self._model_source_rows = [
            dict(model)
            for model in (models or [])
            if not self._is_manual_model_version(model.get("version"))
        ]
        builtin_rows = []
        for captcha_type in ("numeric", "click"):
            active_model = self._model_active.get(captcha_type) or {}
            builtin_rows.append(
                {
                    "id": None,
                    "captcha_type": captcha_type,
                    "version": BUILTIN_MODEL_VERSION,
                    "algorithm": "ddddocr",
                    "status": "builtin" if active_model else "current",
                    "is_builtin": True,
                    "artifact_size": 0,
                    "sample_count": 0,
                    "test_count": 0,
                    "correct_count": 0,
                    "accuracy": None,
                    "created_at": None,
                }
            )
        managed_rows = list(self._model_source_rows)
        # Attempts without a matching model are retained by the server for
        # auditing, but they are not model files and must not appear as models.
        self._all_model_rows = builtin_rows + managed_rows
        self._render_models()

    def _render_models(self):
        selected_key = self._selected_model_key
        filter_type = self.model_type_combo.currentData()
        if filter_type:
            rows = [
                model
                for model in self._all_model_rows
                if model.get("captcha_type") == filter_type
            ]
        else:
            rows = list(self._all_model_rows)
        self._model_rows = rows
        previous_signals = self.model_table.blockSignals(True)
        try:
            self.model_table.clearContents()
            self.model_table.setRowCount(len(self._model_rows))
            type_labels = {"numeric": "数字", "click": "文字点选"}
            status_labels = {
                "candidate": "候选",
                "current": "当前应用",
                "archived": "历史",
                "builtin": "内置备用",
            }
            algorithm_labels = {
                "ddddocr": "ddddocr（内置）",
                "hog-linear-svm-v1": "HOG + 线性 SVM",
                "tiny-cnn-onnx-v1": "Tiny CNN + ONNX",
                "knn-pixels-v1": "KNN（旧版）",
            }
            for row, model in enumerate(self._model_rows):
                captcha_type = str(model.get("captcha_type") or "")
                version = str(model.get("version") or "-")
                display_name = str(model.get("display_name") or "").strip()
                label = display_name or version
                metric = self._attempt_metric(
                    self._model_attempts,
                    captcha_type,
                    version,
                )
                automatic_total = int(
                    (metric or {}).get("attempt_count") or 0
                )
                automatic_success = int(
                    (metric or {}).get("success_count") or 0
                )
                automatic_accuracy = (
                    f"{float(metric.get('success_rate') or 0) * 100:.1f}%"
                    if metric and automatic_total
                    else "--"
                )
                builtin = bool(model.get("is_builtin"))
                managed = not builtin
                values = [
                    type_labels.get(captcha_type, "-"),
                    label,
                    algorithm_labels.get(
                        str(model.get("algorithm") or ""),
                        str(model.get("algorithm") or "-"),
                    ),
                    status_labels.get(model.get("status"), "-"),
                    (
                        "--"
                        if not managed
                        else f"{float(model.get('accuracy') or 0) * 100:.1f}%"
                    ),
                    automatic_accuracy,
                    (
                        f"{automatic_success} / {automatic_total}"
                        if automatic_total
                        else "0"
                    ),
                    (
                        "--"
                        if not managed
                        else str(int(model.get("sample_count") or 0))
                    ),
                    (
                        "--"
                        if not managed
                        else str(int(model.get("test_count") or 0))
                    ),
                    (
                        "内置"
                        if builtin
                        else (
                            "--"
                            if not managed
                            else _format_size(model.get("artifact_size"))
                        )
                    ),
                    _display_time(model.get("created_at")),
                ]
                for column, value in enumerate(values):
                    item = QTableWidgetItem(value)
                    if column == 1:
                        tooltip = f"内部版本：{version}"
                        if display_name:
                            tooltip = f"显示名称：{display_name}\n{tooltip}"
                        item.setToolTip(tooltip)
                    if column in {0, 2, 3, 4, 5, 6, 7, 8, 9}:
                        item.setTextAlignment(Qt.AlignCenter)
                    self.model_table.setItem(row, column, item)
        finally:
            self.model_table.blockSignals(previous_signals)
        if selected_key and any(
            (
                str(model.get("captcha_type") or ""),
                str(model.get("version") or ""),
            )
            == selected_key
            for model in self._model_rows
        ):
            for row, model in enumerate(self._model_rows):
                key = (
                    str(model.get("captcha_type") or ""),
                    str(model.get("version") or ""),
                )
                if key == selected_key:
                    self.model_table.selectRow(row)
                    break
        else:
            self._selected_model_key = None
            self.model_table.clearSelection()
        self._model_selection_changed()

    def _model_filter_changed(self, _index):
        self._selected_model_key = None
        self.recalculate_model_btn.setEnabled(False)
        self.rename_model_btn.setEnabled(False)
        self.activate_model_btn.setEnabled(False)
        self._restore_active_accuracy_values()
        self._render_models()

    def _restore_active_accuracy_values(self):
        self._set_accuracy_values(
            "numeric",
            self._model_active,
            self._model_attempts,
            self._all_model_rows,
        )
        self._set_accuracy_values(
            "click",
            self._model_active,
            self._model_attempts,
            self._all_model_rows,
        )

    def _current_model(self):
        row = self.model_table.currentRow()
        if 0 <= row < len(self._model_rows):
            return self._model_rows[row]
        return None

    @staticmethod
    def _model_activation_block_reason(model):
        if model.get("is_builtin"):
            return "" if model.get("status") != "current" else "该内置模型已在使用。"
        algorithm = str(model.get("algorithm") or "").strip()
        if algorithm == RETIRED_KNN_ALGORITHM:
            return "旧版 KNN 模型仅供历史记录查看，v1.1.0 起不能再应用。"
        if algorithm not in ACTIVATABLE_MODEL_ALGORITHMS:
            return "该模型算法不受当前客户端支持，不能应用。"
        if model.get("status") == "current":
            return "该模型已经是当前应用版本。"
        if not model.get("id"):
            return "该模型缺少有效标识，不能应用。"
        return ""

    def _model_selection_changed(self):
        self._restore_active_accuracy_values()
        model = self._current_model()
        if not model:
            self._selected_model_key = None
            self.recalculate_model_btn.setEnabled(False)
            self.rename_model_btn.setEnabled(False)
            self.activate_model_btn.setEnabled(False)
            self.activate_model_btn.setToolTip("请先选择一个模型版本")
            return
        version = str(model.get("version") or "")
        captcha_type = str(model.get("captcha_type") or "")
        self._selected_model_key = (captcha_type, version)
        self.recalculate_model_btn.setEnabled(
            bool(version) and not self._is_manual_model_version(version)
        )
        self.rename_model_btn.setEnabled(
            bool(model.get("id"))
            and not bool(model.get("is_builtin"))
            and callable(
                getattr(
                    getattr(self.session_manager, "api", None),
                    "admin_rename_captcha_model",
                    None,
                )
            )
        )
        activation_block_reason = self._model_activation_block_reason(model)
        self.activate_model_btn.setEnabled(not activation_block_reason)
        self.activate_model_btn.setToolTip(activation_block_reason)
        self._show_selected_model_metric(model)

    def _show_selected_model_metric(self, model):
        captcha_type = str(model.get("captcha_type") or "")
        version = str(model.get("version") or BUILTIN_MODEL_VERSION)
        label = str(model.get("display_name") or version)
        metric = self._attempt_metric(self._model_attempts, captcha_type, version)
        if metric and int(metric.get("attempt_count") or 0):
            self.model_hint.setText(
                f"已选择 {label}：自动识别 {int(metric.get('attempt_count') or 0)} 次，"
                f"准确率 {float(metric.get('success_rate') or 0) * 100:.1f}%"
            )
        else:
            self.model_hint.setText(f"已选择 {label}：暂无自动识别记录。")
        activation_block_reason = self._model_activation_block_reason(model)
        if str(model.get("algorithm") or "") == RETIRED_KNN_ALGORITHM:
            self.model_hint.setText(
                f"{self.model_hint.text()} {activation_block_reason}"
            )

    def _recalculate_selected_model(self):
        model = self._current_model()
        if not model:
            QMessageBox.warning(self, "未选择模型", "请先选择一个模型版本。")
            return
        if self._refresh_task is not None:
            return
        key = (
            str(model.get("captcha_type") or ""),
            str(model.get("version") or ""),
        )
        self._selected_model_key = key
        self.recalculate_model_btn.setEnabled(False)
        self.model_hint.setText(
            f"正在刷新 {model.get('display_name') or key[1]} 的自动识别记录…"
        )

        def load_overview():
            token = self.session_manager.access_token()
            overview = self.session_manager.api.admin_captcha_learning_overview
            try:
                return overview(
                    token,
                    captcha_type=key[0],
                    model_version=key[1],
                )
            except TypeError:
                # Keep compatibility with a 1.0.5 API test double or an
                # older client adapter that has not added the optional query
                # parameters yet.
                return overview(token)

        self._refresh_task = self._start(
            load_overview,
            lambda result, error, selected_key=key: self._model_recalculated(
                selected_key,
                result,
                error,
            ),
        )

    def _model_recalculated(self, selected_key, result, error):
        self.recalculate_model_btn.setEnabled(True)
        self._refresh_task = None
        if error is not None:
            QMessageBox.warning(self, "统计失败", str(error))
            return
        overview = result if isinstance(result, dict) else {}
        filtered_attempts = overview.get("attempts")
        if not isinstance(filtered_attempts, list):
            QMessageBox.warning(self, "统计失败", "服务器返回的统计结果格式无效。")
            return
        selected_metric = self._attempt_metric(
            filtered_attempts,
            selected_key[0],
            selected_key[1],
        )
        # Replace only the selected version's aggregate so the other model
        # rows remain populated while the selected model is re-counted.
        merged_attempts = [
            item
            for item in self._model_attempts
            if not (
                str(item.get("captcha_type") or "") == selected_key[0]
                and str(item.get("model_version") or "") == selected_key[1]
            )
        ]
        if selected_metric is not None:
            merged_attempts.append(
                {
                    "captcha_type": selected_key[0],
                    "model_version": selected_key[1],
                    **selected_metric,
                }
            )
        self._model_attempts = merged_attempts
        policy = overview.get("policy")
        if isinstance(policy, dict) and isinstance(
            policy.get("active_models"), dict
        ):
            self._model_active = dict(policy.get("active_models") or {})
        models = overview.get("models")
        model_rows = models if isinstance(models, list) else self._model_source_rows
        self._selected_model_key = selected_key
        self._set_accuracy_values(
            "numeric",
            self._model_active,
            merged_attempts,
            model_rows,
        )
        self._set_accuracy_values(
            "click",
            self._model_active,
            merged_attempts,
            model_rows,
        )
        self._populate_models(model_rows, merged_attempts, self._model_active)

    def _refresh_samples(self):
        if self._shutting_down or self._sample_refresh_task is not None:
            return
        captcha_type = self.sample_type_combo.currentData()

        def load_samples():
            token = self.session_manager.access_token()
            return self.session_manager.api.admin_captcha_samples(
                token,
                captcha_type=str(captcha_type) if captcha_type else None,
                limit=self._sample_limit,
                offset=self._sample_offset,
            )

        self._sample_refresh_task = self._start(
            load_samples,
            self._samples_loaded,
        )

    def _request_sample_refresh(self):
        if self._sample_refresh_task is not None:
            self._sample_refresh_pending = True
            return
        self._refresh_samples()

    @staticmethod
    def _sample_answer_text(sample):
        answer = sample.get("answer") or {}
        if sample.get("captcha_type") == "numeric":
            return str(answer.get("value") or "-")
        prompt = [str(value) for value in (answer.get("prompt") or [])]
        return "、".join(prompt) or "-"

    def _samples_loaded(self, result, error):
        self._sample_refresh_task = None
        if self._sample_refresh_pending:
            self._sample_refresh_pending = False
            self._refresh_samples()
            return
        if error is not None:
            self._samples_loaded_once = True
            self._sample_rows = []
            self.sample_table.clearContents()
            self.sample_table.setRowCount(0)
            self.sample_page_label.setText("0 / 0")
            self.prev_samples_btn.setEnabled(False)
            self.next_samples_btn.setEnabled(False)
            self.delete_samples_btn.setEnabled(False)
            if isinstance(error, ApiResponseError) and error.status_code == 404:
                self.sample_hint.setText(
                    "样本读取失败：服务端版本过旧，尚未提供样本列表接口；"
                    "现有样本无需删除或重新采集"
                )
            else:
                self.sample_hint.setText(f"样本读取失败：{error}")
            return
        try:
            if isinstance(result, dict):
                page = result
                raw_items = page.get("items") or []
                total = int(page.get("total") or len(raw_items))
                limit = max(
                    1,
                    int(page.get("limit") or self._sample_limit),
                )
                offset = max(0, int(page.get("offset") or 0))
            elif isinstance(result, list):
                # A few pre-v1.0.5 deployments returned a bare list. Keep the
                # page usable while those servers are being upgraded.
                raw_items = result
                total = len(raw_items)
                limit = max(1, self._sample_limit)
                offset = 0
            else:
                raise TypeError("服务器返回的样本列表格式无效")
            if not isinstance(raw_items, list):
                raise TypeError("服务器返回的样本列表格式无效")
            self._sample_rows = [
                item for item in raw_items if isinstance(item, dict)
            ]
        except (TypeError, ValueError, OverflowError) as exc:
            self._samples_loaded_once = True
            self._sample_rows = []
            self.sample_table.clearContents()
            self.sample_table.setRowCount(0)
            self.sample_page_label.setText("0 / 0")
            self.prev_samples_btn.setEnabled(False)
            self.next_samples_btn.setEnabled(False)
            self.sample_hint.setText(f"样本读取失败：{exc}")
            return
        self._sample_limit = limit
        self._sample_offset = offset
        self._samples_loaded_once = True
        self.delete_samples_btn.setEnabled(True)
        self.sample_table.setRowCount(len(self._sample_rows))
        type_labels = {"numeric": "数字", "click": "文字点选"}
        for row, sample in enumerate(self._sample_rows):
            sample_id = str(sample.get("id") or "")
            model_version = str(sample.get("model_version") or "-")
            if str(sample.get("origin") or "") == "import":
                collection_mode = "导入"
            elif self._is_manual_model_version(model_version):
                collection_mode = "人工"
            else:
                collection_mode = "自动"
            image_available = sample.get("image_available", True) is not False
            if not image_available:
                collection_mode = f"{collection_mode}（图片缺失）"
            values = [
                sample_id[:8] or "-",
                type_labels.get(sample.get("captcha_type"), "-"),
                self._sample_answer_text(sample),
                collection_mode,
                model_version,
                _format_size(sample.get("image_size")),
                _display_time(sample.get("captured_at")),
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if sample_id:
                    item.setToolTip(f"样本 ID：{sample_id}")
                if not image_available:
                    item.setToolTip(
                        "该样本记录存在，但服务器没有可读取的图片；"
                        "不会参与导出或模型训练。"
                    )
                if column in {0, 1, 3, 5, 6}:
                    item.setTextAlignment(Qt.AlignCenter)
                self.sample_table.setItem(row, column, item)

        first = offset + 1 if self._sample_rows else 0
        last = offset + len(self._sample_rows)
        self.sample_page_label.setText(f"{first}-{last} / {total}")
        self.sample_hint.setText(
            f"共 {total} 条；双击可查看图片；"
            "可用 Ctrl 或 Shift 多选后删除"
        )
        self.prev_samples_btn.setEnabled(offset > 0)
        self.next_samples_btn.setEnabled(offset + limit < total)

    def _sample_filter_changed(self, _index):
        self._sample_offset = 0
        self._request_sample_refresh()

    def _previous_samples_page(self):
        self._sample_offset = max(0, self._sample_offset - self._sample_limit)
        self._request_sample_refresh()

    def _next_samples_page(self):
        self._sample_offset += self._sample_limit
        self._request_sample_refresh()

    def _view_sample_image(self, row, _column):
        if (
            self._shutting_down
            or self._sample_image_loading
            or not 0 <= row < len(self._sample_rows)
        ):
            return
        sample = self._sample_rows[row]
        sample_id = str(sample.get("id") or "")
        if not sample_id:
            QMessageBox.warning(
                self,
                "无法查看图片",
                "该样本没有有效的样本 ID。",
            )
            return
        if sample.get("image_available", True) is False:
            QMessageBox.warning(
                self,
                "图片不可用",
                "该样本记录存在，但服务器没有可读取的图片。",
            )
            return
        api = getattr(self.session_manager, "api", None)
        download = getattr(api, "admin_download_captcha_sample_image", None)
        if not callable(download):
            QMessageBox.warning(
                self,
                "图片加载失败",
                "当前客户端接口不支持读取样本图片，请升级后重试。",
            )
            return

        self._sample_image_loading = True
        self.sample_table.setEnabled(False)

        def download_image():
            token = self.session_manager.access_token()
            return download(
                token,
                sample_id,
            )

        try:
            task = self._start(
                download_image,
                lambda result, error, current=sample: self._sample_image_loaded(
                    current,
                    result,
                    error,
                ),
            )
        except Exception as exc:  # noqa: BLE001 - restore UI after submit failure
            self._sample_image_loading = False
            self.sample_table.setEnabled(True)
            QMessageBox.warning(self, "图片加载失败", str(exc))
            return
        if task is None:
            self._sample_image_loading = False
            self.sample_table.setEnabled(True)

    def _sample_image_loaded(self, sample, result, error):
        self._sample_image_loading = False
        if self._shutting_down:
            return
        self.sample_table.setEnabled(True)
        if error is not None:
            message = str(error)
            if isinstance(error, ApiResponseError) and error.status_code == 404:
                if error.code == "captcha_sample_image_missing":
                    message = "该样本记录存在，但服务器没有可读取的图片。"
                elif error.code == "captcha_sample_not_found":
                    message = "该样本已不存在，请刷新样本列表后重试。"
                else:
                    message = "服务端版本过旧，尚未提供样本图片读取接口。"
            QMessageBox.warning(self, "图片加载失败", message)
            return
        if result is None:
            QMessageBox.warning(self, "图片加载失败", "服务器返回的图片为空。")
            return
        if not isinstance(result, (bytes, bytearray, memoryview)):
            QMessageBox.warning(
                self,
                "图片加载失败",
                "服务器返回的图片数据格式无效。",
            )
            return
        try:
            image_data = bytes(result)
        except (TypeError, ValueError) as exc:
            QMessageBox.warning(self, "图片加载失败", str(exc))
            return
        if not image_data:
            QMessageBox.warning(self, "图片加载失败", "服务器返回的图片为空。")
            return

        sample_id = str(sample.get("id") or "")
        extension = _sample_image_extension(sample, image_data)
        file_name = f"captcha-sample-{sample_id or 'image'}{extension}"
        ImagePreviewDialog(
            image_data,
            file_name,
            self,
            save_caption="保存样本图片",
        ).exec_()

    def _delete_selected_samples(self):
        rows = sorted(
            {
                index.row()
                for index in self.sample_table.selectionModel().selectedRows()
            }
        )
        samples = [
            self._sample_rows[row]
            for row in rows
            if 0 <= row < len(self._sample_rows)
        ]
        sample_ids = [str(sample.get("id") or "") for sample in samples]
        sample_ids = [sample_id for sample_id in sample_ids if sample_id]
        if not sample_ids:
            QMessageBox.warning(self, "未选择样本", "请先选择要删除的样本。")
            return
        reply = QMessageBox.question(
            self,
            "删除采集样本",
            f"确定永久删除选中的 {len(sample_ids)} 个验证码样本吗？\n\n"
            "删除后将不再参与导出和后续模型训练。",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        self.delete_samples_btn.setEnabled(False)

        def delete_samples():
            token = self.session_manager.access_token()
            return self.session_manager.api.admin_delete_captcha_samples(
                token,
                sample_ids,
            )

        self._start(delete_samples, self._samples_deleted)

    def _samples_deleted(self, result, error):
        self.delete_samples_btn.setEnabled(True)
        if error is not None:
            QMessageBox.warning(self, "删除失败", str(error))
            return
        result = result or {}
        deleted = int(result.get("deleted_count") or 0)
        missing = int(result.get("missing_count") or 0)
        QMessageBox.information(
            self,
            "删除完成",
            f"已删除 {deleted} 个样本，未找到 {missing} 个。",
        )
        if (
            deleted >= len(self._sample_rows)
            and self._sample_rows
            and self._sample_offset
        ):
            self._sample_offset = max(
                0,
                self._sample_offset - self._sample_limit,
            )
        self._request_sample_refresh()
        self.refresh()

    def _change_policy(self, _index):
        if self._loading_policy:
            return
        upload_mode = str(self.upload_mode_combo.currentData() or "off")
        self.upload_mode_combo.setEnabled(False)

        def update():
            token = self.session_manager.access_token()
            return self.session_manager.api.admin_update_captcha_policy(
                token,
                upload_mode,
            )

        self._start(update, self._policy_updated)

    def _policy_updated(self, result, error):
        self.upload_mode_combo.setEnabled(True)
        if error is not None:
            self._loading_policy = True
            self.upload_mode_combo.setCurrentIndex(
                max(
                    0,
                    self.upload_mode_combo.findData(
                        self._confirmed_upload_mode
                    ),
                )
            )
            self._loading_policy = False
            QMessageBox.warning(self, "策略更新失败", str(error))
            return
        result = result or {}
        upload_mode = str(result.get("upload_mode") or "")
        if upload_mode in UPLOAD_MODE_LABELS:
            self._confirmed_upload_mode = upload_mode
        if self.learning_service is not None:
            self.learning_service.refresh_policy()
        self.refresh()

    def _export_dataset(self):
        target, _ = QFileDialog.getSaveFileName(
            self,
            "导出验证码数据集",
            "intdemo-captcha-dataset.zip",
            "ZIP 数据集 (*.zip)",
        )
        if not target:
            return
        self.export_btn.setEnabled(False)

        def export():
            token = self.session_manager.access_token()
            archive = self.session_manager.api.admin_export_captcha_dataset(token)
            target_path = Path(target)
            temporary = target_path.with_name(
                f".{target_path.name}.{uuid.uuid4().hex}.tmp"
            )
            try:
                temporary.write_bytes(archive)
                os.replace(temporary, target_path)
            finally:
                if temporary.exists():
                    try:
                        temporary.unlink()
                    except OSError:
                        pass
            return len(archive)

        self._start(export, self._dataset_exported)

    def _dataset_exported(self, size, error):
        self.export_btn.setEnabled(True)
        if error is not None:
            QMessageBox.warning(self, "导出失败", str(error))
            return
        QMessageBox.information(
            self,
            "导出完成",
            "数据集已导出到 numeric 和 click 两个目录，"
            f"共 {_format_size(size)}。",
        )

    def _import_dataset(self):
        source, _ = QFileDialog.getOpenFileName(
            self,
            "导入验证码数据集",
            "",
            "ZIP 数据集 (*.zip)",
        )
        if not source:
            return
        try:
            if Path(source).stat().st_size > MAX_IMPORT_BYTES:
                raise OSError("数据集压缩包不能超过 100 MB")
        except OSError as exc:
            QMessageBox.warning(self, "无法导入", str(exc))
            return
        self.import_btn.setEnabled(False)

        def import_archive():
            archive = Path(source).read_bytes()
            if len(archive) > MAX_IMPORT_BYTES:
                raise OSError("数据集压缩包不能超过 100 MB")
            token = self.session_manager.access_token()
            return self.session_manager.api.admin_import_captcha_dataset(
                token,
                archive,
            )

        self._start(import_archive, self._dataset_imported)

    def _dataset_imported(self, result, error):
        self.import_btn.setEnabled(True)
        if error is not None:
            QMessageBox.warning(self, "导入失败", str(error))
            return
        result = result or {}
        QMessageBox.information(
            self,
            "导入完成",
            f"新增 {int(result.get('imported_count') or 0)}，"
            f"重复 {int(result.get('duplicate_count') or 0)}，"
            f"跳过 {int(result.get('skipped_count') or 0)}。",
        )
        self._request_sample_refresh()
        self.refresh()

    def _begin_training_terminal(
        self,
        *,
        training_id,
        captcha_name,
        mode_name,
        method_name,
    ):
        previous = self._training_terminal
        if previous is not None:
            previous.close()
            previous.deleteLater()
        self._training_id = training_id
        self._training_terminal = TrainingTerminalDialog(
            captcha_name=captcha_name,
            mode_name=mode_name,
            method_name=method_name,
            parent=self.window(),
        )
        self._training_terminal.destroyed.connect(
            lambda _object=None, task_id=training_id: (
                self._training_terminal_destroyed(task_id)
            )
        )
        self.show_training_terminal_btn.setEnabled(True)
        self._training_terminal.show()
        self._training_terminal.raise_()
        self._training_terminal.activateWindow()
        self._emit_training_progress(
            training_id,
            {
                "event": "queued",
                "progress": 2,
                "message": f"训练任务已启动 · {mode_name} · {method_name}",
            },
        )

    def _training_terminal_destroyed(self, training_id):
        if training_id != self._training_id:
            return
        self._training_terminal = None
        self.show_training_terminal_btn.setEnabled(False)

    def _show_training_terminal(self):
        terminal = self._training_terminal
        if terminal is None:
            return
        terminal.show()
        terminal.raise_()
        terminal.activateWindow()

    def _emit_training_progress(self, training_id, payload):
        if self._shutting_down:
            return
        try:
            self._training_progress_signals.event_received.emit(
                str(training_id),
                dict(payload),
            )
        except RuntimeError:
            # The page may be destroyed while a training worker is finishing.
            return

    def _training_progress_received(self, training_id, payload):
        if self._shutting_down or training_id != self._training_id:
            return
        terminal = self._training_terminal
        if terminal is not None:
            terminal.append_event(dict(payload or {}))

    def _train_model(self, captcha_type):
        type_name = "数字验证码" if captcha_type == "numeric" else "文字点选验证码"
        mode = self.trainer_manager.preferred_mode()
        mode_name = "强化模式" if mode == "enhanced" else "标准模式"
        method_name = TRAINING_METHODS[mode]
        algorithm = TRAINING_ALGORITHM_BY_MODE[mode]
        if self._server_capabilities_loaded and algorithm not in (
            self._server_model_algorithms.get(captcha_type) or frozenset()
        ):
            QMessageBox.warning(
                self,
                "服务器版本不兼容",
                "当前在线服务器尚不支持 v1.1.0 的验证码训练模型。\n\n"
                "请先将服务器升级到 v1.1.0，再开始训练。",
            )
            return
        reply = QMessageBox.question(
            self,
            "训练候选模型",
            f"将下载当前{type_name}成功样本，在本机划分训练集和固定留出集，"
            f"使用{mode_name}生成候选模型并上传模型及评估结果。\n"
            f"当前方法：{method_name}\n\n是否继续？",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        button = (
            self.train_numeric_btn
            if captcha_type == "numeric"
            else self.train_click_btn
        )
        button.setEnabled(False)
        button.setText("训练中…")
        self._training_in_progress = True
        self.train_numeric_btn.setEnabled(False)
        self.train_click_btn.setEnabled(False)
        self._set_component_controls_enabled(False)
        training_id = uuid.uuid4().hex
        self._begin_training_terminal(
            training_id=training_id,
            captcha_name=type_name,
            mode_name=mode_name,
            method_name=method_name,
        )

        def progress(payload):
            self._emit_training_progress(training_id, payload)

        def train():
            progress(
                {
                    "event": "download",
                    "progress": 5,
                    "message": "正在从服务器下载未处理的原始成功样本",
                }
            )
            token = self.session_manager.access_token()
            archive = self.session_manager.api.admin_export_captcha_dataset(
                token,
                captcha_type,
            )
            progress(
                {
                    "event": "inspect",
                    "progress": 12,
                    "message": f"数据集下载完成 · {_format_size(len(archive))}，正在检查样本",
                }
            )
            inspection = inspect_training_dataset(archive, captcha_type)
            declared_samples = int(
                getattr(inspection, "declared_samples", inspection.valid_count)
                or 0
            )
            rejection_summary = getattr(inspection, "rejection_summary", None)
            rejection_text = (
                str(rejection_summary())
                if callable(rejection_summary)
                else "无"
            )
            progress(
                {
                    "event": "inspection_complete",
                    "progress": 18,
                    "message": (
                        f"样本检查完成：服务器记录 {declared_samples} 条，"
                        f"有效且不重复 {inspection.valid_count} 条，"
                        f"过滤原因：{rejection_text}"
                    ),
                }
            )
            minimum = 20 if captcha_type == "numeric" else 30
            if inspection.valid_count < minimum:
                raise CaptchaTrainingError(
                    f"服务器记录 {declared_samples} 条，实际有效且不重复的"
                    f"{type_name}样本只有 {inspection.valid_count} 条，"
                    f"至少需要 {minimum} 条。\n"
                    f"过滤原因：{rejection_text}"
                )
            candidate = (
                train_enhanced_candidate(
                    archive,
                    captcha_type,
                    self.trainer_manager,
                    progress_callback=progress,
                )
                if mode == "enhanced"
                else train_candidate(
                    archive,
                    captcha_type,
                    mode="standard",
                    progress_callback=progress,
                )
            )
            progress(
                {
                    "event": "upload",
                    "progress": 94,
                    "message": (
                        f"本机模型校验完成 · {_format_size(len(candidate.artifact))}，"
                        "正在上传候选模型和评估结果"
                    ),
                }
            )
            model = self.session_manager.api.admin_create_captcha_model(
                token,
                {
                    "captcha_type": candidate.captcha_type,
                    "version": candidate.version,
                    "algorithm": candidate.algorithm,
                    "artifact_base64": base64.b64encode(
                        candidate.artifact
                    ).decode("ascii"),
                    "sample_count": candidate.sample_count,
                    "test_count": candidate.test_count,
                    "correct_count": candidate.correct_count,
                    "metrics": candidate.metrics,
                },
            )
            return model

        self._start(
            train,
            lambda result, error, kind=captcha_type, task_id=training_id: (
                self._model_trained(kind, result, error, task_id)
            ),
        )

    def _model_trained(self, captcha_type, result, error, training_id=None):
        button = (
            self.train_numeric_btn
            if captcha_type == "numeric"
            else self.train_click_btn
        )
        self._training_in_progress = False
        self.train_numeric_btn.setEnabled(True)
        self.train_click_btn.setEnabled(True)
        self._refresh_training_component()
        button.setText(
            "训练数字候选模型"
            if captcha_type == "numeric"
            else "训练点选候选模型"
        )
        terminal = (
            self._training_terminal
            if training_id is None or training_id == self._training_id
            else None
        )
        if terminal is not None:
            terminal.finish(error=error)
        if error is not None:
            if (
                isinstance(error, ApiResponseError)
                and error.code == "validation_error"
            ):
                QMessageBox.warning(
                    self,
                    "服务器版本不兼容",
                    "服务器拒绝了 v1.1.0 模型参数。请先将在线服务器升级到 "
                    "v1.1.0，再重新训练。",
                )
                return
            title = (
                "样本不足"
                if isinstance(error, CaptchaTrainingError)
                else "训练失败"
            )
            QMessageBox.warning(self, title, str(error))
            return
        accuracy = float((result or {}).get("accuracy") or 0) * 100
        QMessageBox.information(
            self,
            "候选模型已生成",
            f"候选版本：{(result or {}).get('version')}\n"
            f"固定留出集准确率：{accuracy:.1f}%\n\n"
            "请核对后再选择“应用所选模型”。",
        )
        self.refresh()

    def _selected_model(self):
        model = self._current_model()
        if model is None:
            QMessageBox.warning(self, "未选择模型", "请先选择一个模型版本。")
            return None
        return model

    def _rename_selected_model(self):
        model = self._selected_model()
        if not model:
            return
        if model.get("is_builtin") or not model.get("id"):
            QMessageBox.warning(
                self,
                "不能重命名",
                "内置 ddddocr 没有可编辑的模型名称。",
            )
            return
        version = str(model.get("version") or "")
        current_name = str(model.get("display_name") or version)
        display_name, accepted = QInputDialog.getText(
            self,
            "重命名模型",
            f"请输入模型显示名称：\n内部版本：{version}",
            text=current_name,
        )
        if not accepted:
            return
        display_name = str(display_name).strip()
        if not display_name:
            QMessageBox.warning(self, "重命名失败", "模型显示名称不能为空。")
            return
        if len(display_name) > 80:
            QMessageBox.warning(self, "重命名失败", "模型显示名称不能超过 80 个字符。")
            return
        rename_api = getattr(
            getattr(self.session_manager, "api", None),
            "admin_rename_captcha_model",
            None,
        )
        if not callable(rename_api):
            QMessageBox.warning(
                self,
                "重命名失败",
                "当前客户端接口不支持模型重命名，请先升级客户端。",
            )
            return
        self.rename_model_btn.setEnabled(False)

        def rename():
            token = self.session_manager.access_token()
            return rename_api(
                token,
                str(model["id"]),
                display_name,
            )

        self._start(
            rename,
            lambda result, error, name=display_name: self._model_renamed(
                name,
                result,
                error,
            ),
        )

    def _model_renamed(self, display_name, result, error):
        if error is not None:
            self.rename_model_btn.setEnabled(True)
            if isinstance(error, ApiResponseError) and error.status_code == 404:
                message = "服务端版本过旧，暂不支持模型重命名。"
            else:
                message = str(error)
            QMessageBox.warning(self, "重命名失败", message)
            return
        self.rename_model_btn.setEnabled(True)
        actual_name = str((result or {}).get("display_name") or display_name)
        QMessageBox.information(self, "重命名完成", f"模型已重命名为：{actual_name}")
        self.refresh()

    def _activate_selected_model(self):
        model = self._selected_model()
        if not model:
            return
        if model.get("is_builtin"):
            if model.get("status") == "current":
                QMessageBox.information(
                    self,
                    "已经应用",
                    "该类型当前已经使用内置 ddddocr。",
                )
                return
            self._use_builtin_for_selected_type()
            return
        if model.get("status") == "current":
            QMessageBox.information(self, "已经应用", "该模型已经是当前应用版本。")
            return
        activation_block_reason = self._model_activation_block_reason(model)
        if activation_block_reason:
            QMessageBox.warning(self, "不能应用模型", activation_block_reason)
            return
        reply = QMessageBox.question(
            self,
            "应用候选模型",
            f"确定把 {model.get('version')} 设置为当前应用模型吗？\n\n"
            "客户端将在下次策略刷新后下载并校验模型。",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        def activate():
            token = self.session_manager.access_token()
            return self.session_manager.api.admin_activate_captcha_model(
                token,
                str(model["id"]),
            )

        self._start(activate, self._model_activated)

    def _model_activated(self, result, error):
        if error is not None:
            QMessageBox.warning(self, "应用失败", str(error))
            return
        if self.learning_service is not None:
            self.learning_service.refresh_policy()
        QMessageBox.information(
            self,
            "模型已应用",
            f"服务端当前模型已切换为 {(result or {}).get('version')}。\n"
            "本客户端正在立即下载并校验，完成后业务自动识别会使用该模型。",
        )
        self.refresh()

    def _active_model_ready(self, captcha_type, version):
        type_name = (
            "数字验证码"
            if captcha_type == "numeric"
            else "文字点选验证码"
        )
        self.model_hint.setText(
            f"{type_name}当前模型 {version} 已下载并校验，"
            "后续业务自动识别将使用该模型。"
        )
        self.refresh()

    def _use_builtin_for_selected_type(self):
        model = self._selected_model()
        if not model:
            return
        captcha_type = str(model.get("captcha_type") or "")
        type_name = "数字验证码" if captcha_type == "numeric" else "文字点选验证码"
        reply = QMessageBox.question(
            self,
            "恢复内置模型",
            f"确定让{type_name}恢复使用内置 ddddocr 吗？\n\n"
            "当前自定义模型会保留为历史版本，可随时重新应用。",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        def use_builtin():
            token = self.session_manager.access_token()
            return self.session_manager.api.admin_use_builtin_captcha_model(
                token,
                captcha_type,
            )

        self._start(use_builtin, self._builtin_model_selected)

    def _builtin_model_selected(self, result, error):
        if error is not None:
            QMessageBox.warning(self, "恢复失败", str(error))
            return
        if self.learning_service is not None:
            self.learning_service.refresh_policy()
        QMessageBox.information(
            self,
            "已恢复内置模型",
            "客户端将在下次策略刷新后停止使用该类型的自定义模型。",
        )
        self.refresh()

    def _delete_selected_model(self):
        model = self._selected_model()
        if not model:
            return
        if model.get("is_builtin"):
            QMessageBox.warning(self, "不能删除", "内置 ddddocr 不能删除。")
            return
        if model.get("status") == "current":
            QMessageBox.warning(self, "不能删除", "当前正在应用的模型不能删除。")
            return
        reply = QMessageBox.question(
            self,
            "删除模型",
            f"确定删除 {model.get('version')} 吗？样本数据不会被删除。",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        def delete():
            token = self.session_manager.access_token()
            self.session_manager.api.admin_delete_captcha_model(
                token,
                str(model["id"]),
            )
            return model.get("version")

        self._start(delete, self._model_deleted)

    def _model_deleted(self, version, error):
        if error is not None:
            QMessageBox.warning(self, "删除失败", str(error))
            return
        QMessageBox.information(self, "删除完成", f"已删除模型 {version}。")
        self.refresh()

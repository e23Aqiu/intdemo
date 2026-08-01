from __future__ import annotations

import base64
import os
import uuid
from pathlib import Path

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
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
    train_candidate,
)
from .announcement_page import start_api_task
from .frameless import FramelessMessageBox as QMessageBox

MAX_IMPORT_BYTES = 100 * 1024 * 1024
UPLOAD_MODES = (
    ("关闭", "off"),
    ("仅统计", "metrics_only"),
    ("采集样本并统计", "samples_and_metrics"),
)
UPLOAD_MODE_LABELS = dict((mode, label) for label, mode in UPLOAD_MODES)


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


class MachineLearningPage(QWidget):
    REFRESH_INTERVAL_MS = 5_000

    def __init__(self, session_manager, learning_service=None, parent=None):
        super().__init__(parent)
        self.session_manager = session_manager
        self.learning_service = learning_service
        self._tasks = []
        self._task_callbacks = {}
        self._refresh_task = None
        self._shutting_down = False
        self._loading_policy = False
        self._confirmed_upload_mode = "off"
        self._model_rows = []
        self._build_ui()

        self.refresh_timer = QTimer(self)
        self.refresh_timer.setInterval(self.REFRESH_INTERVAL_MS)
        self.refresh_timer.timeout.connect(self.refresh)
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
            "数字当前模型",
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
            "点选当前模型",
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
        self.export_btn.clicked.connect(self._export_dataset)
        self.import_btn = QPushButton("导入数据集")
        self.import_btn.clicked.connect(self._import_dataset)
        self.train_numeric_btn = QPushButton("训练数字候选模型")
        self.train_numeric_btn.setObjectName("PrimaryButton")
        self.train_numeric_btn.clicked.connect(
            lambda: self._train_model("numeric")
        )
        self.train_click_btn = QPushButton("训练点选候选模型")
        self.train_click_btn.setObjectName("PrimaryButton")
        self.train_click_btn.clicked.connect(lambda: self._train_model("click"))
        actions.addWidget(self.export_btn)
        actions.addWidget(self.import_btn)
        actions.addStretch()
        actions.addWidget(self.train_numeric_btn)
        actions.addWidget(self.train_click_btn)
        root.addLayout(actions)

        table_title = QLabel("模型版本")
        table_title.setObjectName("SectionTitle")
        root.addWidget(table_title)
        self.model_table = QTableWidget(0, 8)
        self.model_table.setHorizontalHeaderLabels(
            [
                "类型",
                "版本",
                "状态",
                "离线准确率",
                "训练样本",
                "测试样本",
                "模型大小",
                "创建时间",
            ]
        )
        self.model_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.model_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.model_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
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
        self.activate_model_btn = QPushButton("应用所选模型")
        self.activate_model_btn.setObjectName("PrimaryButton")
        self.activate_model_btn.clicked.connect(self._activate_selected_model)
        model_actions.addWidget(self.use_builtin_btn)
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
        self._task_callbacks.clear()
        self._tasks.clear()

    def refresh(self):
        if self._shutting_down or self._refresh_task is not None:
            return
        self.refresh_btn.setEnabled(False)

        def load():
            token = self.session_manager.access_token()
            return self.session_manager.api.admin_captcha_learning_overview(token)

        self._refresh_task = self._start(load, self._overview_loaded)

    def _overview_loaded(self, result, error):
        self._refresh_task = None
        self.refresh_btn.setEnabled(True)
        if error is not None:
            self.policy_detail.setText(f"读取失败：{error}")
            return
        overview = result or {}
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
            "metrics_only": "仅统计：不上传图片和答案",
            "samples_and_metrics": "采集样本并统计：成功时上传图片和答案",
        }[upload_mode]
        self.policy_detail.setText(
            f"{state} · 策略修订 {int(policy.get('revision') or 0)} · "
            f"更新时间 {_display_time(policy.get('updated_at'))}"
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
        self._populate_models(models)

    def _set_accuracy_values(self, captcha_type, active, attempts, models):
        active_model = active.get(captcha_type) or {}
        current_version = str(
            active_model.get("version") or BUILTIN_MODEL_VERSION
        )
        metric = next(
            (
                item
                for item in attempts
                if item.get("captcha_type") == captcha_type
                and item.get("model_version") == current_version
            ),
            None,
        )
        if metric and int(metric.get("attempt_count") or 0):
            current_text = (
                f"{float(metric.get('success_rate') or 0) * 100:.1f}%\n"
                f"{int(metric.get('success_count') or 0)} / "
                f"{int(metric.get('attempt_count') or 0)}\n"
                f"{current_version}"
            )
        else:
            current_text = f"--\n暂无线上尝试\n{current_version}"
        candidates = [
            model
            for model in models
            if model.get("captcha_type") == captcha_type
            and model.get("status") == "candidate"
        ]
        candidate = candidates[0] if candidates else None
        candidate_text = (
            f"{float(candidate.get('accuracy') or 0) * 100:.1f}%\n"
            f"{int(candidate.get('correct_count') or 0)} / "
            f"{int(candidate.get('test_count') or 0)}\n"
            f"{candidate.get('version')}"
            if candidate
            else "--\n暂无候选模型"
        )
        if captcha_type == "numeric":
            self.numeric_current_value.setText(current_text)
            self.numeric_candidate_value.setText(candidate_text)
        else:
            self.click_current_value.setText(current_text)
            self.click_candidate_value.setText(candidate_text)

    def _populate_models(self, models):
        self._model_rows = list(models)
        self.model_table.setRowCount(len(self._model_rows))
        type_labels = {"numeric": "数字", "click": "文字点选"}
        status_labels = {
            "candidate": "候选",
            "current": "当前应用",
            "archived": "历史",
        }
        for row, model in enumerate(self._model_rows):
            values = [
                type_labels.get(model.get("captcha_type"), "-"),
                str(model.get("version") or "-"),
                status_labels.get(model.get("status"), "-"),
                f"{float(model.get('accuracy') or 0) * 100:.1f}%",
                str(int(model.get("sample_count") or 0)),
                str(int(model.get("test_count") or 0)),
                _format_size(model.get("artifact_size")),
                _display_time(model.get("created_at")),
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column in {0, 2, 3, 4, 5, 6}:
                    item.setTextAlignment(Qt.AlignCenter)
                self.model_table.setItem(row, column, item)

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
            f"数据集已导出，共 {_format_size(size)}。",
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
        self.refresh()

    def _train_model(self, captcha_type):
        type_name = "数字验证码" if captcha_type == "numeric" else "文字点选验证码"
        reply = QMessageBox.question(
            self,
            "训练候选模型",
            f"将下载当前{type_name}成功样本，在本机划分训练集和固定留出集，"
            "生成候选模型并上传评估结果。\n\n是否继续？",
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

        def train():
            token = self.session_manager.access_token()
            archive = self.session_manager.api.admin_export_captcha_dataset(
                token,
                captcha_type,
            )
            candidate = train_candidate(archive, captcha_type)
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
            lambda result, error, kind=captcha_type: (
                self._model_trained(kind, result, error)
            ),
        )

    def _model_trained(self, captcha_type, result, error):
        button = (
            self.train_numeric_btn
            if captcha_type == "numeric"
            else self.train_click_btn
        )
        button.setEnabled(True)
        button.setText(
            "训练数字候选模型"
            if captcha_type == "numeric"
            else "训练点选候选模型"
        )
        if error is not None:
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
        row = self.model_table.currentRow()
        if not 0 <= row < len(self._model_rows):
            QMessageBox.warning(self, "未选择模型", "请先选择一个模型版本。")
            return None
        return self._model_rows[row]

    def _activate_selected_model(self):
        model = self._selected_model()
        if not model:
            return
        if model.get("status") == "current":
            QMessageBox.information(self, "已经应用", "该模型已经是当前应用版本。")
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
            f"当前模型已切换为 {(result or {}).get('version')}。",
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

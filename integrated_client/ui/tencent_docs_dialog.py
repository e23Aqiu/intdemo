from PyQt5.QtCore import QDate, QLocale, Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QCheckBox,
    QComboBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)

from ..tencent_docs import TencentDocsImportOptions
from .date_range import DateRangeSelector, StableDateEdit
from .frameless import FramelessDialog


class TencentDocsLinkDialog(FramelessDialog):
    def __init__(
        self,
        current_url="",
        parent=None,
        *,
        current_station="",
        available_stations=(),
        only_unfilled=True,
    ):
        super().__init__(parent)
        self.setWindowTitle("导入腾讯文档")
        self.setModal(True)
        self.setFixedSize(640, 405)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 26, 28, 24)
        layout.setSpacing(14)

        title = QLabel("导入腾讯文档")
        title.setObjectName("PageTitle")
        layout.addWidget(title)
        prompt = QLabel("请输入腾讯文档在线表格链接")
        prompt.setObjectName("SettingFieldLabel")
        layout.addWidget(prompt)
        self.url_edit = QLineEdit(str(current_url or ""))
        self.url_edit.setPlaceholderText(
            "https://docs.qq.com/sheet/..."
        )
        self.url_edit.returnPressed.connect(self._accept_if_valid)
        layout.addWidget(self.url_edit)

        filters = QGridLayout()
        filters.setHorizontalSpacing(12)
        filters.setVerticalSpacing(10)
        filters.setColumnMinimumWidth(1, 160)
        filters.setColumnMinimumWidth(3, 160)
        filters.setColumnStretch(1, 1)
        filters.setColumnStretch(3, 1)
        filters.addWidget(QLabel("跟进中心站"), 0, 0)
        self.station_combo = QComboBox()
        self.station_combo.addItem("全部中心站", "")
        station_names = []
        for value in (current_station, *tuple(available_stations or ())):
            name = str(value or "").strip()
            if name and name not in station_names:
                station_names.append(name)
                self.station_combo.addItem(name, name)
        selected_index = self.station_combo.findData(str(current_station or "").strip())
        self.station_combo.setCurrentIndex(max(0, selected_index))
        self.station_combo.setToolTip(
            "仅新格式表格的“跟进中心站”列参与筛选；旧格式表格会忽略此项"
        )
        filters.addWidget(self.station_combo, 0, 1, 1, 3)

        filters.addWidget(QLabel("下发日期"), 1, 0)
        self.date_start_edit = self._date_edit("TencentDocsStartDate")
        filters.addWidget(self.date_start_edit, 1, 1)
        filters.addWidget(QLabel("至"), 1, 2)
        self.date_end_edit = self._date_edit("TencentDocsEndDate")
        filters.addWidget(self.date_end_edit, 1, 3)

        self.only_unfilled_check = QCheckBox("仅导入“车辆所有人/企业”未填写的行")
        self.only_unfilled_check.setChecked(bool(only_unfilled))
        filters.addWidget(self.only_unfilled_check, 2, 0, 1, 4)
        layout.addLayout(filters)
        hint = QLabel(
            "链接和腾讯文档登录状态会按当前程序账号保存在本机。"
        )
        hint.setObjectName("Muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        confirm = QPushButton("确认并打开")
        confirm.setObjectName("PrimaryButton")
        confirm.clicked.connect(self._accept_if_valid)
        buttons.addWidget(cancel)
        buttons.addWidget(confirm)
        layout.addLayout(buttons)

        self.url_edit.setFocus()
        self.url_edit.selectAll()

    @staticmethod
    def _date_edit(object_name):
        editor = StableDateEdit(QDate.currentDate())
        editor.setObjectName(object_name)
        editor.setCalendarPopup(True)
        editor.setDisplayFormat("yyyy-MM-dd")
        editor.setLocale(QLocale(QLocale.Chinese, QLocale.China))
        editor.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        editor.setFixedWidth(160)
        editor._calendar_hover_delegate = DateRangeSelector._polish_calendar(
            editor.calendarWidget()
        )
        return editor

    def _accept_if_valid(self):
        if self.url_edit.text().strip():
            self.accept()

    def value(self):
        return self.url_edit.text().strip()

    def import_options(self):
        return TencentDocsImportOptions(
            date_start=self.date_start_edit.date().toPyDate(),
            date_end=self.date_end_edit.date().toPyDate(),
            center_station=str(self.station_combo.currentData() or "").strip(),
            only_unfilled=self.only_unfilled_check.isChecked(),
        )


class TencentDocsProgressDialog(FramelessDialog):
    cancel_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._finished = False
        self._cancel_sent = False
        self.setWindowTitle("导入腾讯文档")
        self.setModal(True)
        self.setFixedSize(460, 210)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 28, 28, 24)
        layout.setSpacing(16)

        title = QLabel("正在导入腾讯文档")
        title.setObjectName("PageTitle")
        layout.addWidget(title)
        self.status_label = QLabel("正在准备浏览器…")
        self.status_label.setObjectName("LoadingMessage")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        self.progress = QProgressBar()
        self.progress.setObjectName("LoadingProgress")
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFormat("%p%")
        self.progress.setTextVisible(True)
        layout.addWidget(self.progress)
        self.cancel_button = QPushButton("取消导入")
        self.cancel_button.clicked.connect(self._request_cancel)
        layout.addWidget(self.cancel_button)

    def set_status(self, message: str) -> None:
        self.status_label.setText(str(message or "正在处理…"))

    def set_progress(self, value: int) -> None:
        self.progress.setValue(max(0, min(100, int(value))))

    def _request_cancel(self) -> None:
        if self._finished or self._cancel_sent:
            return
        self._cancel_sent = True
        self.cancel_button.setEnabled(False)
        self.status_label.setText("正在取消并关闭腾讯文档浏览器…")
        self.cancel_requested.emit()

    def finish(self) -> None:
        self._finished = True
        self.accept()

    def reject(self) -> None:
        if self._finished:
            super().reject()
        else:
            self._request_cancel()

    def closeEvent(self, event) -> None:
        if self._finished:
            event.accept()
        else:
            event.ignore()
            self._request_cancel()

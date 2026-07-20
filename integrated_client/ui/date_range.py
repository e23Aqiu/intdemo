from PyQt5.QtCore import QDate, QEvent, QLocale, QPoint, Qt, pyqtSignal
from PyQt5.QtGui import (
    QColor,
    QIcon,
    QMouseEvent,
    QPainter,
    QPalette,
    QPen,
    QTextCharFormat,
)
from PyQt5.QtWidgets import (
    QCalendarWidget,
    QCheckBox,
    QDateEdit,
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QStyle,
    QStyledItemDelegate,
    QTableView,
    QToolButton,
    QWidget,
)


class CalendarHoverDelegate(QStyledItemDelegate):
    """Adds a visible rounded hover state to calendar day cells."""

    def __init__(self, calendar_view):
        super().__init__(calendar_view)
        self.calendar_view = calendar_view
        self.viewport = calendar_view.viewport()
        self.hovered_index = None
        self.viewport.installEventFilter(self)

    def eventFilter(self, watched, event):
        if watched is self.viewport:
            previous_index = self.hovered_index
            if event.type() == QEvent.MouseMove:
                current_index = self.calendar_view.indexAt(event.pos())
                self.hovered_index = (
                    current_index
                    if current_index.isValid() and current_index.row() > 0
                    else None
                )
            elif event.type() == QEvent.Leave:
                self.hovered_index = None

            if previous_index != self.hovered_index:
                if previous_index is not None:
                    self.viewport.update(
                        self.calendar_view.visualRect(previous_index)
                    )
                if self.hovered_index is not None:
                    self.viewport.update(
                        self.calendar_view.visualRect(self.hovered_index)
                    )
        return super().eventFilter(watched, event)

    def paint(self, painter, option, index):
        super().paint(painter, option, index)
        if index != self.hovered_index:
            return
        if option.state & QStyle.State_Selected:
            return

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(QPen(QColor("#b8d0fb"), 1))
        painter.setBrush(QColor(52, 120, 246, 34))
        painter.drawRoundedRect(option.rect.adjusted(3, 3, -3, -3), 6, 6)
        painter.restore()


class StableDateEdit(QDateEdit):
    """Read-only date editor that opens its calendar from the whole field."""

    drop_down_width = 30

    def __init__(self, value, parent=None):
        super().__init__(value, parent)
        self._redirected_calendar_click = False
        self._date_line_edit = self.lineEdit()
        self._date_line_edit.setReadOnly(True)
        self._date_line_edit.setCursor(Qt.PointingHandCursor)
        self._date_line_edit.installEventFilter(self)
        self.setCursor(Qt.PointingHandCursor)

    def eventFilter(self, watched, event):
        if (
            watched is self._date_line_edit
            and self.isEnabled()
            and event.type() in (
                QEvent.MouseButtonPress,
                QEvent.MouseButtonRelease,
                QEvent.MouseButtonDblClick,
            )
            and event.button() == Qt.LeftButton
        ):
            if event.type() == QEvent.MouseButtonPress:
                self.mousePressEvent(event)
                return event.isAccepted()
            if event.type() == QEvent.MouseButtonRelease:
                self.mouseReleaseEvent(event)
                return event.isAccepted()
            if event.type() == QEvent.MouseButtonDblClick:
                self.mouseDoubleClickEvent(event)
                return event.isAccepted()
        return super().eventFilter(watched, event)

    def _calendar_button_event(self, event_type, source_event):
        position = QPoint(
            max(0, self.width() - self.drop_down_width // 2),
            self.height() // 2,
        )
        return QMouseEvent(
            event_type,
            position,
            self.mapToGlobal(position),
            source_event.button(),
            source_event.buttons(),
            source_event.modifiers(),
        )

    def mousePressEvent(self, event):
        if (
            event.button() == Qt.LeftButton
            and event.pos().x() < self.width() - self.drop_down_width
        ):
            self.setFocus(Qt.MouseFocusReason)
            self._redirected_calendar_click = True
            super().mousePressEvent(
                self._calendar_button_event(QEvent.MouseButtonPress, event)
            )
            event.accept()
            return
        self._redirected_calendar_click = False
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if self._redirected_calendar_click and event.button() == Qt.LeftButton:
            self._redirected_calendar_click = False
            super().mouseReleaseEvent(
                self._calendar_button_event(QEvent.MouseButtonRelease, event)
            )
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        self.mousePressEvent(event)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Tab, Qt.Key_Backtab):
            super().keyPressEvent(event)
            return
        event.accept()

    def wheelEvent(self, event):
        event.accept()


class DateRangeSelector(QWidget):
    """Inclusive local-date range selector with an explicit all-dates state."""

    range_changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._adjusting = False

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(7)

        today = QDate.currentDate()
        first_day = QDate(today.year(), today.month(), 1)
        self.start_edit = self._date_edit("StartDateEdit", first_day)
        self.end_edit = self._date_edit("EndDateEdit", today)
        self.start_label = QLabel("从")
        self.end_label = QLabel("至")
        layout.addWidget(self.start_label)
        layout.addWidget(self.start_edit)
        layout.addWidget(self.end_label)
        layout.addWidget(self.end_edit)

        self.range_separator = QFrame()
        self.range_separator.setObjectName("DateRangeSeparator")
        self.range_separator.setFixedWidth(1)
        layout.addSpacing(3)
        layout.addWidget(self.range_separator)
        layout.addSpacing(3)

        self.all_dates_panel = QFrame()
        self.all_dates_panel.setObjectName("AllDatesPanel")
        all_dates_layout = QHBoxLayout(self.all_dates_panel)
        all_dates_layout.setContentsMargins(8, 2, 9, 2)
        all_dates_layout.setSpacing(0)
        self.all_dates_check = QCheckBox("全部日期")
        self.all_dates_check.setObjectName("AllDatesCheck")
        self.all_dates_check.setChecked(True)
        self.all_dates_check.setMinimumWidth(
            self.all_dates_check.sizeHint().width()
        )
        all_dates_layout.addWidget(self.all_dates_check)
        self.all_dates_panel.setMinimumWidth(
            self.all_dates_check.minimumWidth()
            + all_dates_layout.contentsMargins().left()
            + all_dates_layout.contentsMargins().right()
            + 2
        )
        layout.addWidget(self.all_dates_panel)

        self.setStyleSheet(
            "QFrame#DateRangeSeparator{background:#d8e1ed;border:none;}"
            "QFrame#AllDatesPanel{background:#edf4ff;border:1px solid #cfe0ff;"
            "border-radius:7px;}"
            "QFrame#AllDatesPanel:disabled{background:#f3f5f8;"
            "border-color:#e4e9f0;}"
            "QCheckBox#AllDatesCheck{color:#1c5ed6;font-weight:600;}"
            "QCheckBox#AllDatesCheck:disabled{color:#a3adba;}"
        )

        self.all_dates_check.toggled.connect(self._all_dates_toggled)
        self.start_edit.dateChanged.connect(self._start_changed)
        self.end_edit.dateChanged.connect(self._end_changed)
        self._set_date_edits_enabled(False)
        layout.activate()
        self.setFixedWidth(layout.minimumSize().width())
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

    @staticmethod
    def _date_edit(object_name, value):
        editor = StableDateEdit(value)
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

    @staticmethod
    def _polish_calendar(calendar):
        calendar.setLocale(QLocale(QLocale.Chinese, QLocale.China))
        calendar.setMinimumSize(332, 280)
        calendar.setFirstDayOfWeek(Qt.Monday)
        calendar.setHorizontalHeaderFormat(QCalendarWidget.ShortDayNames)
        calendar.setVerticalHeaderFormat(QCalendarWidget.NoVerticalHeader)
        calendar.setGridVisible(False)

        calendar_view = calendar.findChild(
            QTableView, "qt_calendar_calendarview"
        )
        if calendar_view is not None:
            calendar_view.setMouseTracking(True)
            calendar_view.setAttribute(Qt.WA_Hover, True)
            calendar_view.viewport().setMouseTracking(True)
            calendar_view.viewport().setAttribute(Qt.WA_Hover, True)
            hover_delegate = CalendarHoverDelegate(calendar_view)
            calendar_view.setItemDelegate(hover_delegate)
        else:
            hover_delegate = None

        palette = calendar.palette()
        palette.setColor(QPalette.Base, QColor("#ffffff"))
        palette.setColor(QPalette.Text, QColor("#243047"))
        palette.setColor(QPalette.Highlight, QColor("#3478f6"))
        palette.setColor(QPalette.HighlightedText, QColor("#ffffff"))
        calendar.setPalette(palette)

        weekday_format = QTextCharFormat()
        weekday_format.setForeground(QColor("#243047"))
        weekend_format = QTextCharFormat()
        weekend_format.setForeground(QColor("#e45454"))
        for weekday in (
            Qt.Monday,
            Qt.Tuesday,
            Qt.Wednesday,
            Qt.Thursday,
            Qt.Friday,
        ):
            calendar.setWeekdayTextFormat(weekday, weekday_format)
        calendar.setWeekdayTextFormat(Qt.Saturday, weekend_format)
        calendar.setWeekdayTextFormat(Qt.Sunday, weekend_format)

        for object_name, text, tooltip in (
            ("qt_calendar_prevmonth", "‹", "上个月"),
            ("qt_calendar_nextmonth", "›", "下个月"),
        ):
            button = calendar.findChild(QToolButton, object_name)
            if button is not None:
                button.setIcon(QIcon())
                button.setText(text)
                button.setToolTip(tooltip)
        return hover_delegate

    def _set_date_edits_enabled(self, enabled):
        self.start_edit.setEnabled(enabled)
        self.end_edit.setEnabled(enabled)

    def _all_dates_toggled(self, checked):
        self._set_date_edits_enabled(not checked)
        self.range_changed.emit()

    def _start_changed(self, value):
        if self._adjusting:
            return
        if value > self.end_edit.date():
            self._adjusting = True
            self.end_edit.setDate(value)
            self._adjusting = False
        self.range_changed.emit()

    def _end_changed(self, value):
        if self._adjusting:
            return
        if value < self.start_edit.date():
            self._adjusting = True
            self.start_edit.setDate(value)
            self._adjusting = False
        self.range_changed.emit()

    def date_range(self):
        if self.all_dates_check.isChecked():
            return None, None
        return self.start_edit.date().toPyDate(), self.end_edit.date().toPyDate()

    def range_label(self):
        start_date, end_date = self.date_range()
        if start_date is None:
            return "全部日期"
        return f"{start_date.isoformat()} 至 {end_date.isoformat()}"

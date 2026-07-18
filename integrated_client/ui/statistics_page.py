import math

from PyQt5.QtCore import QRect, QRectF, Qt, QTimer
from PyQt5.QtGui import (
    QColor,
    QFont,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
)
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QFrame,
    QGridLayout,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..database import (
    DEFAULT_STATION_USERS,
    Database,
    WORKFLOW_EMPTY_METRIC,
    WORKFLOW_HAS_PHONE_METRIC,
    WORKFLOW_INDIVIDUAL_METRIC,
    WORKFLOW_NO_OPERATION_METRIC,
    WORKFLOW_NO_PHONE_METRIC,
    WORKFLOW_NO_TRANSPORT_METRIC,
    WORKFLOW_TOTAL_METRIC,
)
from ..models import Account


class ChartHoverCard(QFrame):
    """不受图表边界裁切、可由图表内容单独定制的悬浮信息卡。"""

    def __init__(self, parent=None):
        super().__init__(
            parent,
            Qt.ToolTip | Qt.FramelessWindowHint,
        )
        self.title_text = ""
        self.details = []
        self._detail_row_count = 0
        self.setObjectName("ChartHoverCard")
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setFixedWidth(240)
        self.setStyleSheet(
            """
            QFrame#ChartHoverCard {
                background: rgba(23, 35, 60, 245);
                border: 1px solid #40506b;
                border-radius: 10px;
            }
            QLabel#HoverCardTitle {
                color: #ffffff;
                font-size: 13px;
                font-weight: 700;
            }
            QLabel#HoverCardKey {
                color: #aebbd1;
                font-size: 12px;
            }
            QLabel#HoverCardValue {
                color: #ffffff;
                font-size: 12px;
                font-weight: 700;
            }
            """
        )
        outer = QHBoxLayout(self)
        outer.setContentsMargins(10, 10, 12, 10)
        outer.setSpacing(10)
        self._accent = QFrame()
        self._accent.setFixedWidth(4)
        outer.addWidget(self._accent)

        content = QVBoxLayout()
        content.setContentsMargins(0, 0, 0, 0)
        content.setSpacing(7)
        self._title = QLabel()
        self._title.setObjectName("HoverCardTitle")
        self._title.setWordWrap(True)
        self._title.setFixedWidth(202)
        content.addWidget(self._title)
        self._details_layout = QGridLayout()
        self._details_layout.setContentsMargins(0, 0, 0, 0)
        self._details_layout.setHorizontalSpacing(18)
        self._details_layout.setVerticalSpacing(4)
        content.addLayout(self._details_layout)
        outer.addLayout(content, 1)
        self.hide()

    def _clear_details(self):
        while self._details_layout.count():
            item = self._details_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def show_details(self, title, details, accent_color, anchor):
        """在全局坐标 anchor 附近显示，并保持在当前屏幕可用区域内。"""
        self.title_text = str(title)
        self.details = [(str(key), str(value)) for key, value in details]
        self._title.setText(self.title_text)
        self._title.ensurePolished()
        title_rect = self._title.fontMetrics().boundingRect(
            QRect(0, 0, self._title.width(), 1000),
            Qt.TextWordWrap,
            self.title_text,
        )
        self._title.setFixedHeight(
            max(self._title.fontMetrics().lineSpacing(), title_rect.height())
        )
        self._accent.setStyleSheet(
            f"background:{QColor(accent_color).name()};border-radius:2px;"
        )
        self._clear_details()
        compact_grid = len(self.details) > 2
        self._detail_row_count = (
            math.ceil(len(self.details) / 2) if compact_grid else len(self.details)
        )
        for index, (key, value) in enumerate(self.details):
            row = index // 2 if compact_grid else index
            group = index % 2 if compact_grid else 0
            key_column = group * 2
            value_column = key_column + 1
            key_label = QLabel(key)
            key_label.setObjectName("HoverCardKey")
            value_label = QLabel(value)
            value_label.setObjectName("HoverCardValue")
            value_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self._details_layout.addWidget(key_label, row, key_column)
            self._details_layout.addWidget(value_label, row, value_column)
        self._details_layout.activate()
        self.layout().activate()
        hint = self.sizeHint()
        card_width = max(
            self.minimumWidth(), min(self.maximumWidth(), hint.width())
        )
        card_height = max(
            self.minimumSizeHint().height(),
            hint.height(),
            50 + self._detail_row_count * 22,
        )
        self.resize(card_width, card_height)
        x = anchor.x() + 16
        y = anchor.y() + 14
        screen = QApplication.screenAt(anchor)
        if screen is None:
            screen = QApplication.primaryScreen()
        available = screen.availableGeometry()
        if x + card_width > available.right() - 8:
            x = anchor.x() - card_width - 16
        if y + card_height > available.bottom() - 8:
            y = anchor.y() - card_height - 14
        x = max(available.left() + 8, min(int(x), available.right() - card_width - 8))
        y = max(available.top() + 8, min(int(y), available.bottom() - card_height - 8))
        self.move(x, y)
        self.show()
        self.raise_()


class AnimatedDonutChart(QWidget):
    """为仪表盘环形图提供悬停命中、浮出效果和计量条流光。"""

    BAR_HEIGHT = 10.0
    DONUT_HOVER_OFFSET = 9.0

    def __init__(self, parent=None):
        super().__init__(parent)
        self._slice_hitboxes = []
        self._hovered_slice = None
        self._animation_phase = 0.0
        self._animation_timer = QTimer(self)
        self._animation_timer.setInterval(40)
        self._animation_timer.timeout.connect(self._advance_animation)
        self._hover_card = ChartHoverCard(self)
        self.setMouseTracking(True)

    def _advance_animation(self):
        self._animation_phase = (self._animation_phase + 0.025) % 1.0
        self.update()

    def showEvent(self, event):
        self._animation_timer.start()
        super().showEvent(event)

    def hideEvent(self, event):
        self._animation_timer.stop()
        self._hovered_slice = None
        self._hover_card.hide()
        super().hideEvent(event)

    def leaveEvent(self, event):
        self._hovered_slice = None
        self._hover_card.hide()
        self.update()
        super().leaveEvent(event)

    def _slice_at(self, position):
        for index, item in enumerate(self._slice_hitboxes):
            outer = item["outer"]
            inner = item["inner"]
            center = outer.center()
            dx = position.x() - center.x()
            dy = center.y() - position.y()
            distance = math.hypot(dx, dy)
            if distance < inner.width() / 2 or distance > outer.width() / 2:
                continue
            angle = math.degrees(math.atan2(dy, dx)) % 360
            clockwise_delta = (item["start"] - angle) % 360
            if clockwise_delta <= item["sweep"] + 0.25:
                return index, item["payload"]
        return None, None

    def _draw_donut_slice(
        self, painter, outer, inner, start_degrees, span_degrees, color, index
    ):
        wedge = QPainterPath()
        wedge.moveTo(outer.center())
        wedge.arcTo(outer, start_degrees, span_degrees)
        wedge.closeSubpath()
        hole = QPainterPath()
        hole.addEllipse(inner)
        ring_slice = wedge.subtracted(hole)
        if index == self._hovered_slice:
            midpoint = math.radians(start_degrees + span_degrees / 2)
            floating = self.DONUT_HOVER_OFFSET + math.sin(
                self._animation_phase * math.tau
            ) * 1.5
            offset_x = math.cos(midpoint) * floating
            offset_y = -math.sin(midpoint) * floating
            ring_slice.translate(offset_x, offset_y)
        painter.setPen(Qt.NoPen)
        painter.setBrush(color)
        painter.drawPath(ring_slice)

    def _draw_flow_highlight(self, painter, clip_path, rect):
        band_width = max(42.0, rect.width() * 0.12)
        travel = rect.width() + band_width * 2
        center_x = rect.left() - band_width + travel * self._animation_phase
        gradient = QLinearGradient(
            center_x - band_width,
            rect.top(),
            center_x + band_width,
            rect.top(),
        )
        gradient.setColorAt(0.0, QColor(255, 255, 255, 0))
        gradient.setColorAt(0.5, QColor(255, 255, 255, 105))
        gradient.setColorAt(1.0, QColor(255, 255, 255, 0))
        painter.save()
        painter.setClipPath(clip_path)
        painter.fillRect(rect, gradient)
        painter.restore()


class WorkflowDistributionChart(AnimatedDonutChart):
    """不依赖额外图表库的完整流程结果环形图。"""

    SEGMENTS = (
        (WORKFLOW_HAS_PHONE_METRIC, "有公司名、有电话", QColor("#31ad76")),
        (WORKFLOW_NO_PHONE_METRIC, "有公司名、无电话", QColor("#e1b13d")),
        (WORKFLOW_INDIVIDUAL_METRIC, "个体经营", QColor("#8c6bd8")),
        (WORKFLOW_NO_OPERATION_METRIC, "无营运信息", QColor("#ed874c")),
        (WORKFLOW_NO_TRANSPORT_METRIC, "无运输证号", QColor("#e35d6a")),
        (WORKFLOW_EMPTY_METRIC, "空", QColor("#8a98aa")),
    )

    def __init__(self, parent=None):
        super().__init__(parent)
        self._values = {}
        self._scope = ""
        self._bar_rects = []
        self.setMinimumHeight(280)

    def set_values(self, values, scope=""):
        self._values = {key: int(value or 0) for key, value in values.items()}
        self._scope = scope
        self.update()

    def paintEvent(self, event):
        self._slice_hitboxes = []
        self._bar_rects = []
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        bounds = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        painter.setPen(QPen(QColor("#e4eaf2"), 1))
        painter.setBrush(QColor("#ffffff"))
        painter.drawRoundedRect(bounds, 10, 10)

        painter.setPen(QColor("#17233c"))
        painter.setFont(QFont("Microsoft YaHei UI", 11, QFont.Bold))
        painter.drawText(20, 29, "完整流程结果分布")
        painter.setPen(QColor("#708096"))
        painter.setFont(QFont("Microsoft YaHei UI", 9))
        painter.drawText(160, 29, self._scope)

        chart_size = min(190, max(150, self.height() - 82))
        pie_rect = QRectF(32, 56, chart_size, chart_size)
        inner = pie_rect.adjusted(
            chart_size * 0.28,
            chart_size * 0.28,
            -chart_size * 0.28,
            -chart_size * 0.28,
        )
        segment_values = [self._values.get(key, 0) for key, _, _ in self.SEGMENTS]
        classified_total = sum(segment_values)
        start_degrees = 90.0
        if classified_total:
            for index, (value, (_, label, color)) in enumerate(
                zip(segment_values, self.SEGMENTS)
            ):
                span_degrees = -(value / classified_total * 360)
                if value:
                    self._slice_hitboxes.append(
                        {
                            "outer": QRectF(pie_rect),
                            "inner": QRectF(inner),
                            "start": start_degrees,
                            "sweep": abs(span_degrees),
                            "payload": {
                                "label": label,
                                "value": value,
                                "total": classified_total,
                                "color": color,
                            },
                        }
                    )
                    slice_index = len(self._slice_hitboxes) - 1
                    self._draw_donut_slice(
                        painter,
                        pie_rect,
                        inner,
                        start_degrees,
                        span_degrees,
                        color,
                        slice_index,
                    )
                start_degrees += span_degrees
        else:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor("#e9eef5"))
            painter.drawEllipse(pie_rect)
            painter.setBrush(QColor("#ffffff"))
            painter.drawEllipse(inner)
        total = self._values.get(WORKFLOW_TOTAL_METRIC, classified_total)
        painter.setPen(QColor("#17233c"))
        painter.setFont(QFont("Microsoft YaHei UI", 18, QFont.Bold))
        painter.drawText(inner, Qt.AlignCenter, str(total))
        painter.setPen(QColor("#708096"))
        painter.setFont(QFont("Microsoft YaHei UI", 8))
        painter.drawText(
            QRectF(inner.left(), inner.center().y() + 17, inner.width(), 20),
            Qt.AlignHCenter | Qt.AlignTop,
            "总计",
        )

        legend_left = pie_rect.right() + 42
        legend_width = max(120, self.width() - legend_left - 28)
        row_height = 34
        for index, ((_, label, color), value) in enumerate(zip(self.SEGMENTS, segment_values)):
            top = 50 + index * row_height
            painter.setPen(Qt.NoPen)
            painter.setBrush(color)
            painter.drawRoundedRect(QRectF(legend_left, top + 2, 10, 10), 3, 3)
            painter.setPen(QColor("#526177"))
            painter.setFont(QFont("Microsoft YaHei UI", 9))
            painter.drawText(int(legend_left + 18), int(top + 12), label)
            percent = value / classified_total * 100 if classified_total else 0
            painter.setPen(QColor("#17233c"))
            painter.setFont(QFont("Microsoft YaHei UI", 10, QFont.Bold))
            painter.drawText(
                QRectF(legend_left, top - 3, legend_width, 20),
                Qt.AlignRight | Qt.AlignVCenter,
                f"{value} 条  ·  {percent:.1f}%",
            )
            bar_rect = QRectF(
                legend_left, top + 20, legend_width, self.BAR_HEIGHT
            )
            self._bar_rects.append(QRectF(bar_rect))
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor("#edf1f6"))
            painter.drawRoundedRect(bar_rect, 5, 5)
            if classified_total and value:
                value_rect = QRectF(bar_rect)
                value_rect.setWidth(max(10, bar_rect.width() * value / classified_total))
                painter.setBrush(color)
                value_path = QPainterPath()
                value_path.addRoundedRect(value_rect, 5, 5)
                painter.drawPath(value_path)
                self._draw_flow_highlight(painter, value_path, value_rect)

    def mouseMoveEvent(self, event):
        index, payload = self._slice_at(event.pos())
        if payload is not None:
            self._hovered_slice = index
            percent = payload["value"] / max(payload["total"], 1) * 100
            self._hover_card.show_details(
                payload["label"],
                [("数量", f"{payload['value']} 条"), ("占比", f"{percent:.1f}%")],
                payload["color"],
                event.globalPos(),
            )
            self.update()
            return
        if self._hovered_slice is not None:
            self._hovered_slice = None
            self.update()
        self._hover_card.hide()
        super().mouseMoveEvent(event)


class ViolationReasonChart(AnimatedDonutChart):
    """违规原因环形图及可切换的电话拆分/原因占比计量条。"""

    COLORS = (
        QColor("#3478f6"),
        QColor("#31ad76"),
        QColor("#e1b13d"),
        QColor("#e35d6a"),
        QColor("#8c6bd8"),
        QColor("#ed874c"),
        QColor("#28a7a1"),
        QColor("#7f93ad"),
        QColor("#c765a7"),
        QColor("#65a84f"),
    )
    PHONE_COLOR = QColor("#16a66a")
    OTHER_COLOR = QColor("#f28c28")

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows = []
        self._scope = ""
        self._bar_hitboxes = []
        self._bar_mode = "share"
        self.mode_button = QPushButton("切换为电话拆分", self)
        self.mode_button.setObjectName("ViolationModeButton")
        self.mode_button.setCheckable(True)
        self.mode_button.setChecked(True)
        self.mode_button.setFixedSize(150, 32)
        self.mode_button.toggled.connect(self._on_mode_button_toggled)
        self.setMinimumHeight(280)

    def set_rows(self, rows, scope=""):
        self._rows = list(rows)
        self._scope = scope
        self.update()

    def set_bar_mode(self, mode):
        if mode not in {"split", "share"}:
            raise ValueError(f"Unsupported violation bar mode: {mode}")
        self._bar_mode = mode
        checked = mode == "share"
        self.mode_button.blockSignals(True)
        self.mode_button.setChecked(checked)
        self.mode_button.blockSignals(False)
        self.mode_button.setText(
            "切换为电话拆分" if checked else "切换为原因占比"
        )
        self._hover_card.hide()
        self.update()

    def _on_mode_button_toggled(self, checked):
        self.set_bar_mode("share" if checked else "split")

    def resizeEvent(self, event):
        chart_size = min(190, max(150, self.height() - 82))
        preferred_x = int(32 + chart_size + 46)
        right_aligned_x = max(8, self.width() - self.mode_button.width() - 12)
        self.mode_button.move(min(preferred_x, right_aligned_x), 7)
        self.mode_button.raise_()
        super().resizeEvent(event)

    @staticmethod
    def _format_total_label(total):
        return f"{int(total)} 条"

    def _format_bar_label(self, total, all_total):
        if self._bar_mode == "share":
            percent = int(total) / max(int(all_total), 1) * 100
            return f"{int(total)} 条  ·  {percent:.1f}%"
        return self._format_total_label(total)

    def paintEvent(self, event):
        self._slice_hitboxes = []
        self._bar_hitboxes = []
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        bounds = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        painter.setPen(QPen(QColor("#e4eaf2"), 1))
        painter.setBrush(QColor("#ffffff"))
        painter.drawRoundedRect(bounds, 10, 10)

        painter.setPen(QColor("#17233c"))
        painter.setFont(QFont("Microsoft YaHei UI", 11, QFont.Bold))
        painter.drawText(20, 29, "违规原因分布")
        painter.setPen(QColor("#708096"))
        painter.setFont(QFont("Microsoft YaHei UI", 9))
        painter.drawText(130, 29, self._scope)

        legend_x = max(320, self.width() - 214)
        if self._bar_mode == "split":
            painter.setPen(Qt.NoPen)
            painter.setBrush(self.PHONE_COLOR)
            painter.drawRoundedRect(QRectF(legend_x, 19, 9, 9), 3, 3)
            painter.setPen(QColor("#708096"))
            painter.drawText(legend_x + 14, 29, "有电话")
            painter.setPen(Qt.NoPen)
            painter.setBrush(self.OTHER_COLOR)
            painter.drawRoundedRect(QRectF(legend_x + 82, 19, 9, 9), 3, 3)
            painter.setPen(QColor("#708096"))
            painter.drawText(legend_x + 96, 29, "其他数据")
        else:
            painter.setPen(QColor("#708096"))
            painter.drawText(
                QRectF(legend_x, 14, 186, 20),
                Qt.AlignRight | Qt.AlignVCenter,
                "条长表示原因占比",
            )

        if not self._rows:
            painter.setPen(QColor("#8a98aa"))
            painter.drawText(self.rect(), Qt.AlignCenter, "当前筛选范围暂无“原因”数据")
            return

        total = sum(int(row["total"]) for row in self._rows)
        chart_size = min(190, max(150, self.height() - 82))
        pie_rect = QRectF(32, 56, chart_size, chart_size)
        inner = pie_rect.adjusted(
            chart_size * 0.28,
            chart_size * 0.28,
            -chart_size * 0.28,
            -chart_size * 0.28,
        )
        start_degrees = 90.0
        for index, row in enumerate(self._rows):
            span_degrees = -(int(row["total"]) / max(total, 1) * 360)
            if int(row["total"]):
                color = self.COLORS[index % len(self.COLORS)]
                self._slice_hitboxes.append(
                    {
                        "outer": QRectF(pie_rect),
                        "inner": QRectF(inner),
                        "start": start_degrees,
                        "sweep": abs(span_degrees),
                        "payload": {**dict(row), "color": color},
                    }
                )
                slice_index = len(self._slice_hitboxes) - 1
                self._draw_donut_slice(
                    painter,
                    pie_rect,
                    inner,
                    start_degrees,
                    span_degrees,
                    color,
                    slice_index,
                )
            start_degrees += span_degrees
        painter.setPen(QColor("#17233c"))
        painter.setFont(QFont("Microsoft YaHei UI", 18, QFont.Bold))
        painter.drawText(inner, Qt.AlignCenter, str(total))
        painter.setPen(QColor("#708096"))
        painter.setFont(QFont("Microsoft YaHei UI", 8))
        painter.drawText(
            QRectF(inner.left(), inner.center().y() + 17, inner.width(), 20),
            Qt.AlignHCenter | Qt.AlignTop,
            "原因条目",
        )

        rows = self._rows[:7]
        legend_left = pie_rect.right() + 46
        legend_width = max(120, self.width() - legend_left - 28)
        label_width = max(90, int(legend_width * 0.56))
        row_height = 30
        top = 46
        painter.setFont(QFont("Microsoft YaHei UI", 9))

        for index, row in enumerate(rows):
            row_top = top + index * row_height
            color = self.COLORS[index % len(self.COLORS)]
            painter.setPen(Qt.NoPen)
            painter.setBrush(color)
            painter.drawRoundedRect(QRectF(legend_left, row_top + 2, 10, 10), 3, 3)
            painter.setFont(QFont("Microsoft YaHei UI", 9))
            reason = painter.fontMetrics().elidedText(
                str(row["reason"]), Qt.ElideRight, label_width - 24
            )
            label_rect = QRectF(legend_left + 18, row_top, label_width - 18, 16)
            painter.setPen(QColor("#526177"))
            painter.drawText(label_rect, Qt.AlignVCenter, reason)

            painter.setPen(QColor("#17233c"))
            painter.setFont(QFont("Microsoft YaHei UI", 9, QFont.Bold))
            painter.drawText(
                QRectF(legend_left, row_top, legend_width, 16),
                Qt.AlignRight | Qt.AlignVCenter,
                self._format_bar_label(row["total"], total),
            )

            bar_rect = QRectF(
                legend_left, row_top + 18, legend_width, self.BAR_HEIGHT
            )
            self._bar_hitboxes.append((bar_rect, dict(row)))
            painter.setPen(Qt.NoPen)
            bar_path = QPainterPath()
            bar_path.addRoundedRect(bar_rect, 5, 5)
            if self._bar_mode == "split":
                phone_count = max(0, int(row["has_phone"]))
                other_count = max(0, int(row["other"]))
                split_total = phone_count + other_count
                phone_ratio = phone_count / split_total if split_total else 0
                painter.save()
                painter.setClipPath(bar_path)
                painter.fillRect(bar_rect, self.OTHER_COLOR)
                if phone_ratio:
                    phone_rect = QRectF(bar_rect)
                    phone_rect.setWidth(bar_rect.width() * phone_ratio)
                    painter.fillRect(phone_rect, self.PHONE_COLOR)
                painter.restore()
                self._draw_flow_highlight(painter, bar_path, bar_rect)
            else:
                painter.setBrush(QColor("#edf1f6"))
                painter.drawPath(bar_path)
                if int(row["total"]):
                    value_rect = QRectF(bar_rect)
                    value_rect.setWidth(
                        max(10, bar_rect.width() * int(row["total"]) / max(total, 1))
                    )
                    value_path = QPainterPath()
                    value_path.addRoundedRect(value_rect, 5, 5)
                    painter.setBrush(color)
                    painter.drawPath(value_path)
                    self._draw_flow_highlight(painter, value_path, value_rect)

        if len(self._rows) > len(rows):
            painter.setPen(QColor("#8a98aa"))
            painter.setFont(QFont("Microsoft YaHei UI", 8))
            painter.drawText(
                legend_left,
                self.height() - 7,
                f"图表显示前 7 项，完整 {len(self._rows)} 项见下方表格",
            )

    def mouseMoveEvent(self, event):
        slice_index, slice_row = self._slice_at(event.pos())
        if slice_row is not None:
            self._hovered_slice = slice_index
            total = int(slice_row["total"])
            all_total = sum(int(row["total"]) for row in self._rows)
            percent = total / max(all_total, 1) * 100
            self._hover_card.show_details(
                slice_row["reason"],
                [
                    ("总计", f"{total} 条"),
                    ("占比", f"{percent:.1f}%"),
                    ("有电话", f"{slice_row['has_phone']} 条"),
                    ("其他数据", f"{slice_row['other']} 条"),
                ],
                slice_row["color"],
                event.globalPos(),
            )
            self.update()
            return
        for rect, row in self._bar_hitboxes:
            if rect.contains(event.pos()):
                if self._hovered_slice is not None:
                    self._hovered_slice = None
                    self.update()
                self._hover_card.show_details(
                    "电话数据拆分",
                    [
                        ("有电话", f"{row['has_phone']} 条"),
                        ("其他数据", f"{row['other']} 条"),
                    ],
                    self.PHONE_COLOR,
                    event.globalPos(),
                )
                return
        if self._hovered_slice is not None:
            self._hovered_slice = None
            self.update()
        self._hover_card.hide()
        super().mouseMoveEvent(event)


class StationDistributionChart(AnimatedDonutChart):
    """用双环形图对比各站总计数与有电话数在全部站点中的占比。"""

    TOTAL_COLOR = QColor("#3478f6")
    PHONE_COLOR = QColor("#20a66a")
    COLORS = (
        QColor("#3478f6"),
        QColor("#20a66a"),
        QColor("#e1a928"),
        QColor("#e35d6a"),
        QColor("#8c6bd8"),
        QColor("#ed874c"),
        QColor("#28a7a1"),
    )

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows = []
        self._legend_hitboxes = []
        self.setMinimumHeight(280)

    def set_rows(self, rows):
        self._rows = list(rows)
        self._hovered_slice = None
        self._hover_card.hide()
        self.update()

    @staticmethod
    def _format_share(value):
        return f"{float(value):.1f}%"

    def _draw_series_donut(
        self,
        painter,
        outer,
        value_key,
        share_key,
        title,
        center_label,
    ):
        inner = outer.adjusted(
            outer.width() * 0.28,
            outer.height() * 0.28,
            -outer.width() * 0.28,
            -outer.height() * 0.28,
        )
        total = sum(int(row[value_key]) for row in self._rows)
        painter.setPen(QColor("#526177"))
        painter.setFont(QFont("Microsoft YaHei UI", 9, QFont.Bold))
        painter.drawText(
            QRectF(outer.left(), outer.top() - 27, outer.width(), 20),
            Qt.AlignCenter,
            title,
        )

        if total:
            start_degrees = 90.0
            for index, row in enumerate(self._rows):
                value = int(row[value_key])
                span_degrees = -(value / total * 360)
                if value:
                    color = self.COLORS[index % len(self.COLORS)]
                    self._slice_hitboxes.append(
                        {
                            "outer": QRectF(outer),
                            "inner": QRectF(inner),
                            "start": start_degrees,
                            "sweep": abs(span_degrees),
                            "payload": {
                                "station": row["station"],
                                "series": title,
                                "value": value,
                                "share": row[share_key],
                                "color": color,
                            },
                        }
                    )
                    slice_index = len(self._slice_hitboxes) - 1
                    self._draw_donut_slice(
                        painter,
                        outer,
                        inner,
                        start_degrees,
                        span_degrees,
                        color,
                        slice_index,
                    )
                start_degrees += span_degrees
        else:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor("#e9eef5"))
            painter.drawEllipse(outer)
            painter.setBrush(QColor("#ffffff"))
            painter.drawEllipse(inner)

        painter.setPen(QColor("#17233c"))
        painter.setFont(QFont("Microsoft YaHei UI", 17, QFont.Bold))
        painter.drawText(
            QRectF(inner.left(), inner.top() - 5, inner.width(), inner.height()),
            Qt.AlignCenter,
            str(total),
        )
        painter.setPen(QColor("#708096"))
        painter.setFont(QFont("Microsoft YaHei UI", 8))
        painter.drawText(
            QRectF(inner.left(), inner.center().y() + 14, inner.width(), 18),
            Qt.AlignHCenter | Qt.AlignTop,
            center_label,
        )

    def paintEvent(self, event):
        self._slice_hitboxes = []
        self._legend_hitboxes = []
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        bounds = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        painter.setPen(QPen(QColor("#e4eaf2"), 1))
        painter.setBrush(QColor("#ffffff"))
        painter.drawRoundedRect(bounds, 10, 10)

        painter.setPen(QColor("#17233c"))
        painter.setFont(QFont("Microsoft YaHei UI", 11, QFont.Bold))
        painter.drawText(20, 29, "各站业务分布")
        painter.setPen(QColor("#708096"))
        painter.setFont(QFont("Microsoft YaHei UI", 9))
        painter.drawText(130, 29, "占全部站点对应数据的比例")

        if not self._rows:
            painter.setPen(QColor("#8a98aa"))
            painter.drawText(self.rect(), Qt.AlignCenter, "暂无站点数据")
            return

        chart_area_width = min(450, max(360, int(self.width() * 0.42)))
        chart_size = min(
            160,
            max(120, self.height() - 115),
            max(120, int((chart_area_width - 70) / 2)),
        )
        first_left = 30
        second_left = first_left + chart_size + 30
        chart_top = 82
        self._draw_series_donut(
            painter,
            QRectF(first_left, chart_top, chart_size, chart_size),
            "total",
            "total_share",
            "各站总计数占比",
            "总计数",
        )
        self._draw_series_donut(
            painter,
            QRectF(second_left, chart_top, chart_size, chart_size),
            "has_phone",
            "phone_share",
            "各站有电话数占比",
            "有电话数",
        )

        legend_left = second_left + chart_size + 40
        legend_width = max(270, self.width() - legend_left - 22)
        total_column = legend_left + legend_width - 310
        phone_column = legend_left + legend_width - 150
        painter.setFont(QFont("Microsoft YaHei UI", 8, QFont.Bold))
        painter.setPen(QColor("#718096"))
        painter.drawText(legend_left + 18, 52, "站点")
        painter.setPen(self.TOTAL_COLOR)
        painter.drawText(
            QRectF(total_column, 40, 145, 18),
            Qt.AlignRight | Qt.AlignVCenter,
            "总计数 / 占比",
        )
        painter.setPen(self.PHONE_COLOR)
        painter.drawText(
            QRectF(phone_column, 40, 145, 18),
            Qt.AlignRight | Qt.AlignVCenter,
            "有电话数 / 占比",
        )

        visible_rows = self._rows[:7]
        row_height = max(
            29,
            min(38, int((self.height() - 58) / max(len(visible_rows), 1))),
        )
        top = 59
        label_width = max(90, total_column - legend_left - 24)
        painter.setFont(QFont("Microsoft YaHei UI", 9))

        for index, row in enumerate(visible_rows):
            row_top = top + index * row_height
            hitbox = QRectF(
                legend_left, row_top, legend_width, max(27, row_height - 2)
            )
            self._legend_hitboxes.append((hitbox, dict(row)))
            if index % 2:
                painter.setPen(Qt.NoPen)
                painter.setBrush(QColor("#f7f9fd"))
                painter.drawRoundedRect(hitbox, 6, 6)

            color = self.COLORS[index % len(self.COLORS)]
            painter.setPen(Qt.NoPen)
            painter.setBrush(color)
            painter.drawRoundedRect(
                QRectF(legend_left + 2, row_top + row_height / 2 - 5, 10, 10),
                3,
                3,
            )
            painter.setPen(QColor("#526177"))
            station = painter.fontMetrics().elidedText(
                str(row["station"]), Qt.ElideRight, label_width
            )
            painter.drawText(
                QRectF(legend_left + 18, row_top, label_width, row_height - 2),
                Qt.AlignLeft | Qt.AlignVCenter,
                station,
            )
            painter.setPen(self.TOTAL_COLOR)
            painter.drawText(
                QRectF(total_column, row_top, 145, row_height - 2),
                Qt.AlignRight | Qt.AlignVCenter,
                f'{row["total"]} 条 · {self._format_share(row["total_share"])}',
            )
            painter.setPen(self.PHONE_COLOR)
            painter.drawText(
                QRectF(phone_column, row_top, 145, row_height - 2),
                Qt.AlignRight | Qt.AlignVCenter,
                f'{row["has_phone"]} 条 · {self._format_share(row["phone_share"])}',
            )

        if len(self._rows) > len(visible_rows):
            painter.setPen(QColor("#8a98aa"))
            painter.setFont(QFont("Microsoft YaHei UI", 8))
            painter.drawText(
                20,
                self.height() - 7,
                f"图表显示前 7 个站点，完整 {len(self._rows)} 个站点见下方表格",
            )

    def mouseMoveEvent(self, event):
        slice_index, payload = self._slice_at(event.pos())
        if payload is not None:
            self._hovered_slice = slice_index
            self._hover_card.show_details(
                payload["station"],
                [
                    ("统计指标", payload["series"]),
                    ("数量", f'{payload["value"]} 条'),
                    ("占比", self._format_share(payload["share"])),
                ],
                payload["color"],
                event.globalPos(),
            )
            self.update()
            return
        for rect, row in self._legend_hitboxes:
            if rect.contains(event.pos()):
                if self._hovered_slice is not None:
                    self._hovered_slice = None
                    self.update()
                self._hover_card.show_details(
                    row["station"],
                    [
                        ("总计数", f'{row["total"]} 条'),
                        ("总数占比", self._format_share(row["total_share"])),
                        ("有电话数", f'{row["has_phone"]} 条'),
                        ("有电话占比", self._format_share(row["phone_share"])),
                    ],
                    self.COLORS[
                        self._rows.index(row) % len(self.COLORS)
                    ],
                    event.globalPos(),
                )
                return
        if self._hovered_slice is not None:
            self._hovered_slice = None
            self.update()
        self._hover_card.hide()
        super().mouseMoveEvent(event)


class StatisticsPage(QWidget):
    def __init__(self, database: Database, account: Account, dependency_warning="", parent=None):
        super().__init__(parent)
        self.database = database
        self.account = account

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 22)
        layout.setSpacing(10)

        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title = QLabel("数据仪表盘")
        title.setObjectName("PageTitle")
        title_box.addWidget(title)
        subtitle = QLabel(
            "按站点查看完成类型或违规原因统计。" if account.is_admin
            else "默认显示当前站点，可切换查看全部或其他站点的数据。"
        )
        subtitle.setObjectName("Muted")
        title_box.addWidget(subtitle)
        header.addLayout(title_box)
        header.addStretch()
        refresh = QPushButton("刷新统计")
        refresh.clicked.connect(self.refresh)
        header.addWidget(refresh)
        layout.addLayout(header)

        if dependency_warning:
            warning = QLabel(dependency_warning)
            warning.setWordWrap(True)
            warning.setStyleSheet(
                "color:#9a6415;background:#fff7e5;border:1px solid #f4d79b;"
                "padding:10px;border-radius:8px;"
            )
            layout.addWidget(warning)

        filter_card = QFrame()
        filter_card.setObjectName("Card")
        filter_layout = QHBoxLayout(filter_card)
        filter_layout.setContentsMargins(16, 10, 16, 10)
        filter_layout.addWidget(QLabel("站点"))
        self.station_combo = QComboBox()
        self.station_combo.setMinimumWidth(210)
        filter_layout.addWidget(self.station_combo)
        filter_layout.addSpacing(22)
        filter_layout.addWidget(QLabel("数据分类"))
        self.category_combo = QComboBox()
        self.category_combo.setMinimumWidth(180)
        self.category_combo.addItem("各站分布", "station_distribution")
        self.category_combo.addItem("按完成类型", "completion")
        self.category_combo.addItem("按违规类型", "violation")
        filter_layout.addWidget(self.category_combo)
        filter_layout.addStretch()
        layout.addWidget(filter_card)

        self._active_category = self.category_combo.currentData()
        self._station_before_distribution = None
        self._has_saved_station_before_distribution = False
        self._populate_station_options()
        if self._active_category == "station_distribution":
            self._station_before_distribution = self.station_combo.currentData()
            self._has_saved_station_before_distribution = True
        self.station_combo.currentIndexChanged.connect(self.refresh)
        self.category_combo.currentIndexChanged.connect(self._on_category_changed)

        self.kpi_layout = QGridLayout()
        self.kpi_layout.setSpacing(10)
        layout.addLayout(self.kpi_layout)

        self.detail_tabs = QTabWidget()
        self.detail_tabs.setObjectName("DashboardTabs")
        self.detail_tabs.setDocumentMode(True)
        self.detail_tabs.setMinimumHeight(350)

        self.chart_tab = QWidget()
        chart_layout = QVBoxLayout(self.chart_tab)
        chart_layout.setContentsMargins(12, 12, 12, 12)
        chart_layout.setSpacing(0)
        self.distribution_chart = WorkflowDistributionChart()
        self.violation_chart = ViolationReasonChart()
        self.station_distribution_chart = StationDistributionChart()
        self.violation_mode_button = self.violation_chart.mode_button
        chart_layout.addWidget(self.distribution_chart)
        chart_layout.addWidget(self.violation_chart)
        chart_layout.addWidget(self.station_distribution_chart)
        chart_layout.addStretch(1)
        self.detail_tabs.addTab(self.chart_tab, "图表分析")

        self.data_tab = QWidget()
        data_layout = QVBoxLayout(self.data_tab)
        data_layout.setContentsMargins(14, 14, 14, 14)
        data_layout.setSpacing(12)

        data_header = QFrame()
        data_header.setObjectName("DataSummaryHeader")
        data_header.setStyleSheet(
            """
            QFrame#DataSummaryHeader {
                background: #f7f9fd;
                border: 1px solid #e2e8f2;
                border-radius: 10px;
            }
            QLabel#DataTitle {
                color: #17233c;
                font-size: 16px;
                font-weight: 700;
                border: none;
                background: transparent;
            }
            QLabel#DataDescription {
                color: #718096;
                font-size: 12px;
                border: none;
                background: transparent;
            }
            QLabel#DataCountBadge {
                color: #1c5ed6;
                background: #edf4ff;
                border: 1px solid #cfe0ff;
                border-radius: 12px;
                padding: 4px 11px;
                font-weight: 700;
            }
            """
        )
        data_header_layout = QHBoxLayout(data_header)
        data_header_layout.setContentsMargins(16, 11, 16, 11)
        data_header_layout.setSpacing(12)
        data_title_layout = QVBoxLayout()
        data_title_layout.setContentsMargins(0, 0, 0, 0)
        data_title_layout.setSpacing(3)
        self.data_title = QLabel("完整数据")
        self.data_title.setObjectName("DataTitle")
        self.data_description = QLabel("按当前筛选条件展示详细统计")
        self.data_description.setObjectName("DataDescription")
        data_title_layout.addWidget(self.data_title)
        data_title_layout.addWidget(self.data_description)
        data_header_layout.addLayout(data_title_layout)
        data_header_layout.addStretch()
        self.data_count_label = QLabel("0 行")
        self.data_count_label.setObjectName("DataCountBadge")
        self.data_count_label.setAlignment(Qt.AlignCenter)
        data_header_layout.addWidget(self.data_count_label)
        data_layout.addWidget(data_header)

        self.summary_table = QTableWidget()
        self.summary_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.summary_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.summary_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.summary_table.setAlternatingRowColors(True)
        self.summary_table.setShowGrid(False)
        self.summary_table.setWordWrap(False)
        self.summary_table.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.summary_table.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.summary_table.verticalHeader().setVisible(False)
        self.summary_table.verticalHeader().setDefaultSectionSize(42)
        self.summary_table.horizontalHeader().setMinimumHeight(42)
        self.summary_table.horizontalHeader().setMinimumSectionSize(80)
        self.summary_table.setStyleSheet(
            """
            QTableWidget {
                background: #ffffff;
                alternate-background-color: #f8faff;
                border: 1px solid #e1e7f0;
                border-radius: 10px;
                color: #26344d;
                selection-background-color: #e8f1ff;
                selection-color: #173b73;
            }
            QTableWidget::item {
                border-bottom: 1px solid #edf1f6;
                padding: 7px 10px;
            }
            QHeaderView::section {
                background: #f2f5fa;
                color: #53627a;
                border: none;
                border-bottom: 1px solid #dde4ee;
                padding: 8px 10px;
                font-weight: 700;
            }
            QTableCornerButton::section {
                background: #f2f5fa;
                border: none;
                border-bottom: 1px solid #dde4ee;
            }
            """
        )
        data_layout.addWidget(self.summary_table)
        self.detail_tabs.addTab(self.data_tab, "完整数据")

        layout.addWidget(self.detail_tabs, 1)
        self.refresh()

    def _populate_station_options(self):
        had_options = self.station_combo.count() > 0
        current_id = self.station_combo.currentData()
        self.station_combo.blockSignals(True)
        self.station_combo.clear()
        self.station_combo.addItem("全部站点", None)
        order = {username: index for index, (_, username) in enumerate(DEFAULT_STATION_USERS)}
        accounts = [account for account in self.database.list_accounts() if not account.is_admin]
        accounts.sort(key=lambda item: (order.get(item.username, 999), item.name_label))
        for station in accounts:
            self.station_combo.addItem(station.name_label, station.id)
            self.station_combo.setItemData(
                self.station_combo.count() - 1,
                f"账号：{station.username}",
                Qt.ToolTipRole,
            )
        target_id = current_id if had_options else (
            None if self.account.is_admin else self.account.id
        )
        index = self.station_combo.findData(target_id)
        self.station_combo.setCurrentIndex(index if index >= 0 else 0)
        self.station_combo.setEnabled(True)
        self.station_combo.blockSignals(False)

    def _selected_user_id(self):
        return self.station_combo.currentData()

    def _selected_scope(self):
        return self.station_combo.currentText()

    def _on_category_changed(self, _index):
        category = self.category_combo.currentData()
        if category == "station_distribution" and self._active_category != category:
            self._station_before_distribution = self.station_combo.currentData()
            self._has_saved_station_before_distribution = True
        elif (
            self._active_category == "station_distribution"
            and category != "station_distribution"
            and self._has_saved_station_before_distribution
        ):
            station_index = self.station_combo.findData(
                self._station_before_distribution
            )
            self.station_combo.blockSignals(True)
            self.station_combo.setCurrentIndex(station_index if station_index >= 0 else 0)
            self.station_combo.blockSignals(False)
            self._has_saved_station_before_distribution = False
        self._active_category = category
        self.refresh()

    def _clear_kpis(self):
        while self.kpi_layout.count():
            item = self.kpi_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    @staticmethod
    def _metric_card(label, value, unit):
        card = QFrame()
        card.setObjectName("Card")
        box = QVBoxLayout(card)
        box.setContentsMargins(14, 9, 14, 9)
        caption = QLabel(label)
        caption.setObjectName("Muted")
        number = QLabel(f"{value} {unit}")
        number.setObjectName("MetricValue")
        box.addWidget(caption)
        box.addWidget(number)
        return card

    def _add_kpis(self, values):
        for index, (label, value, unit) in enumerate(values):
            self.kpi_layout.addWidget(
                self._metric_card(label, value, unit), index // 4, index % 4
            )

    def _finish_summary_table(self, scope, detail, stretch_columns=(0,)):
        self.data_description.setText(f"{scope} · {detail}")
        self.data_count_label.setText(f"{self.summary_table.rowCount()} 行")
        header = self.summary_table.horizontalHeader()
        header.setStretchLastSection(False)
        for column in range(self.summary_table.columnCount()):
            mode = (
                QHeaderView.Stretch
                if column in stretch_columns
                else QHeaderView.ResizeToContents
            )
            header.setSectionResizeMode(column, mode)
        self.summary_table.scrollToTop()

    @staticmethod
    def _ordered_completion_metrics(metrics):
        """总计保持在首位，其余完成类型与图表使用同一反向顺序。"""
        metrics_by_key = {metric["metric_key"]: metric for metric in metrics}
        ordered_keys = [WORKFLOW_TOTAL_METRIC]
        ordered_keys.extend(
            metric_key for metric_key, _, _ in WorkflowDistributionChart.SEGMENTS
        )
        ordered = [
            metrics_by_key.pop(metric_key)
            for metric_key in ordered_keys
            if metric_key in metrics_by_key
        ]
        ordered.extend(metrics_by_key.values())
        return ordered

    def _get_station_distribution_rows(self):
        default_order = {
            username: index for index, (_, username) in enumerate(DEFAULT_STATION_USERS)
        }
        stations = {}
        for row in self.database.get_all_account_totals():
            if row["role"] != "user":
                continue
            station = stations.setdefault(
                row["user_id"],
                {
                    "user_id": row["user_id"],
                    "station": row["display_name"] or row["username"],
                    "username": row["username"],
                    "total": 0,
                    "has_phone": 0,
                },
            )
            if row["metric_key"] == WORKFLOW_TOTAL_METRIC:
                station["total"] = int(row["total"])
            elif row["metric_key"] == WORKFLOW_HAS_PHONE_METRIC:
                station["has_phone"] = int(row["total"])

        rows = sorted(
            stations.values(),
            key=lambda row: (
                default_order.get(row["username"], 999),
                row["station"],
            ),
        )
        all_total = sum(row["total"] for row in rows)
        all_has_phone = sum(row["has_phone"] for row in rows)
        for row in rows:
            row["total_share"] = row["total"] / all_total * 100 if all_total else 0
            row["phone_share"] = (
                row["has_phone"] / all_has_phone * 100 if all_has_phone else 0
            )
        return rows

    def _render_completion(self, user_id, scope):
        metrics = self._ordered_completion_metrics(
            self.database.get_metric_definitions()
        )
        self.distribution_chart.show()
        self.violation_chart.hide()
        self.station_distribution_chart.hide()

        if user_id is None:
            totals_by_metric = {metric["metric_key"]: 0 for metric in metrics}
            for row in self.database.get_all_account_totals():
                if row["role"] == "user":
                    totals_by_metric[row["metric_key"]] = (
                        totals_by_metric.get(row["metric_key"], 0)
                        + int(row["total"])
                    )
        else:
            totals_by_metric = {
                row["metric_key"]: int(row["total"])
                for row in self.database.get_user_totals(user_id)
            }

        self._add_kpis(
            [
                (
                    metric["label"],
                    totals_by_metric.get(metric["metric_key"], 0),
                    metric["unit"],
                )
                for metric in metrics
            ]
        )
        self.distribution_chart.set_values(totals_by_metric, scope)
        self.summary_table.setColumnCount(3)
        self.summary_table.setHorizontalHeaderLabels(["完成类型", "累计数量", "单位"])
        self.summary_table.setRowCount(len(metrics))
        for row_index, metric in enumerate(metrics):
            for column, value in enumerate(
                [
                    metric["label"],
                    totals_by_metric.get(metric["metric_key"], 0),
                    metric["unit"],
                ]
            ):
                item = QTableWidgetItem(str(value))
                if column == 1:
                    font = item.font()
                    font.setBold(True)
                    item.setFont(font)
                    item.setForeground(QColor("#1c5ed6"))
                    item.setTextAlignment(Qt.AlignCenter)
                elif column == 2:
                    item.setForeground(QColor("#718096"))
                    item.setTextAlignment(Qt.AlignCenter)
                self.summary_table.setItem(row_index, column, item)
        self._finish_summary_table(scope, "完成类型累计明细")

    def _render_violation(self, user_id, scope):
        rows = self.database.get_violation_totals(
            user_id,
            users_only=(user_id is None),
        )
        self.distribution_chart.hide()
        self.violation_chart.show()
        self.station_distribution_chart.hide()

        total = sum(row["total"] for row in rows)
        has_phone = sum(row["has_phone"] for row in rows)
        other = sum(row["other"] for row in rows)
        self._add_kpis(
            [
                ("违规原因条目", total, "条"),
                ("原因种类", len(rows), "种"),
                ("有电话", has_phone, "条"),
                ("其他数据", other, "条"),
            ]
        )
        self.violation_chart.set_rows(rows, scope)
        headers = ["违规原因", "总计", "有电话", "其他数据", "有电话占比"]
        self.summary_table.setColumnCount(len(headers))
        self.summary_table.setHorizontalHeaderLabels(headers)
        self.summary_table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            percent = row["has_phone"] / row["total"] * 100 if row["total"] else 0
            values = [
                row["reason"], row["total"], row["has_phone"], row["other"], f"{percent:.1f}%"
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if column > 0:
                    item.setTextAlignment(Qt.AlignCenter)
                if column == 1:
                    font = item.font()
                    font.setBold(True)
                    item.setFont(font)
                    item.setForeground(QColor("#26344d"))
                elif column == 2:
                    item.setForeground(QColor("#15946c"))
                elif column == 3:
                    item.setForeground(QColor("#d17a22"))
                elif column == 4:
                    item.setForeground(QColor("#1c5ed6"))
                self.summary_table.setItem(row_index, column, item)
        self._finish_summary_table(scope, "违规原因明细")

    def _render_station_distribution(self):
        rows = self._get_station_distribution_rows()
        self.distribution_chart.hide()
        self.violation_chart.hide()
        self.station_distribution_chart.show()
        self.station_distribution_chart.set_rows(rows)

        all_total = sum(row["total"] for row in rows)
        all_has_phone = sum(row["has_phone"] for row in rows)
        overall_phone_rate = all_has_phone / all_total * 100 if all_total else 0
        self._add_kpis(
            [
                ("站点数量", len(rows), "个"),
                ("总计数", all_total, "条"),
                ("有电话数", all_has_phone, "条"),
                ("有电话占总计数", f"{overall_phone_rate:.1f}", "%"),
            ]
        )

        headers = [
            "站点",
            "总计数",
            "总数占比",
            "有电话数",
            "有电话数占比",
        ]
        self.summary_table.setColumnCount(len(headers))
        self.summary_table.setHorizontalHeaderLabels(headers)
        self.summary_table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            values = [
                row["station"],
                row["total"],
                f'{row["total_share"]:.1f}%',
                row["has_phone"],
                f'{row["phone_share"]:.1f}%',
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if column in (1, 2):
                    item.setForeground(QColor("#3478f6"))
                    item.setTextAlignment(Qt.AlignCenter)
                elif column in (3, 4):
                    item.setForeground(QColor("#15946c"))
                    item.setTextAlignment(Qt.AlignCenter)
                if column in (1, 3):
                    font = item.font()
                    font.setBold(True)
                    item.setFont(font)
                self.summary_table.setItem(row_index, column, item)
        self._finish_summary_table(
            "全部站点",
            "各站总计数占比与有电话数占比",
            (0,),
        )

    def refresh(self):
        self._populate_station_options()
        self._clear_kpis()
        category = self.category_combo.currentData()
        is_station_distribution = category == "station_distribution"
        if is_station_distribution:
            self.station_combo.blockSignals(True)
            self.station_combo.setCurrentIndex(0)
            self.station_combo.blockSignals(False)
            self.station_combo.setEnabled(False)
            self.station_combo.setToolTip("各站分布固定统计全部站点")
            self._render_station_distribution()
            return

        self.station_combo.setEnabled(True)
        self.station_combo.setToolTip("")
        user_id = self._selected_user_id()
        scope = self._selected_scope()
        if category == "violation":
            self._render_violation(user_id, scope)
        else:
            self._render_completion(user_id, scope)

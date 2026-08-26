from datetime import date, datetime, timedelta

from PyQt5.QtCore import QPointF, QRectF, Qt
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
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..database import (
    DEFAULT_STATION_USERS,
    WORKFLOW_HAS_PHONE_METRIC,
    WORKFLOW_TOTAL_METRIC,
    Database,
)
from ..models import Account
from .statistics_page import (
    AnimatedDonutChart,
    ChartHoverCard,
    format_hours,
    format_precise_duration,
)
from .loading_dialog import run_ui_with_loading
from .table_utils import make_table_columns_resizable


MILLISECONDS_PER_HOUR = 60 * 60 * 1000


class DashboardMetricIcon(QWidget):
    """Paint a compact vector pictogram without relying on font glyphs."""

    ACCESSIBLE_NAMES = {
        "search": "查询图标",
        "calendar": "日历图标",
        "phone": "电话图标",
        "phone-off": "无电话图标",
        "clock": "时钟图标",
    }

    def __init__(self, icon_kind, accent, background, parent=None):
        super().__init__(parent)
        self.icon_kind = icon_kind
        self.accent = QColor(accent)
        self.background = QColor(background)
        self.setObjectName("DashboardMetricBadge")
        self.setAccessibleName(
            self.ACCESSIBLE_NAMES.get(icon_kind, "统计指标图标")
        )
        self.setFixedSize(42, 42)

    def _icon_pen(self, width=2.4):
        pen = QPen(self.accent, width)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        return pen

    @staticmethod
    def _phone_path():
        path = QPainterPath()
        path.moveTo(12.5, 9.5)
        path.cubicTo(10.7, 10.1, 10.0, 12.0, 10.8, 15.1)
        path.cubicTo(13.1, 23.7, 19.0, 29.6, 27.5, 31.9)
        path.cubicTo(30.5, 32.7, 32.4, 32.0, 33.0, 30.2)
        path.lineTo(33.7, 27.5)
        path.cubicTo(34.0, 26.3, 33.3, 25.1, 32.2, 24.7)
        path.lineTo(27.7, 22.9)
        path.cubicTo(26.5, 22.4, 25.3, 22.8, 24.6, 23.8)
        path.lineTo(23.3, 25.7)
        path.cubicTo(20.2, 24.2, 17.7, 21.7, 16.2, 18.6)
        path.lineTo(18.1, 17.3)
        path.cubicTo(19.1, 16.6, 19.5, 15.4, 19.0, 14.2)
        path.lineTo(17.2, 9.8)
        path.cubicTo(16.8, 8.7, 15.6, 8.0, 14.4, 8.3)
        path.closeSubpath()
        return path

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        painter.setBrush(self.background)
        painter.drawRoundedRect(QRectF(self.rect()), 11, 11)

        painter.setPen(self._icon_pen())
        painter.setBrush(Qt.NoBrush)
        if self.icon_kind == "search":
            painter.drawEllipse(QRectF(10.0, 9.5, 16.5, 16.5))
            painter.drawLine(QPointF(24.1, 23.6), QPointF(32.0, 31.5))
        elif self.icon_kind == "calendar":
            painter.drawRoundedRect(QRectF(9.5, 11.5, 23.0, 21.0), 3, 3)
            painter.drawLine(QPointF(10.5, 17.5), QPointF(31.5, 17.5))
            painter.drawLine(QPointF(15.0, 9.0), QPointF(15.0, 14.0))
            painter.drawLine(QPointF(27.0, 9.0), QPointF(27.0, 14.0))
            painter.setPen(Qt.NoPen)
            painter.setBrush(self.accent)
            painter.drawEllipse(QRectF(18.5, 21.0, 5.0, 5.0))
        elif self.icon_kind in {"phone", "phone-off"}:
            painter.setPen(Qt.NoPen)
            painter.setBrush(self.accent)
            painter.drawPath(self._phone_path())
            if self.icon_kind == "phone-off":
                separator = QPen(self.background, 5.2)
                separator.setCapStyle(Qt.RoundCap)
                painter.setPen(separator)
                painter.drawLine(QPointF(9.5, 32.5), QPointF(32.5, 9.5))
                painter.setPen(self._icon_pen(2.6))
                painter.drawLine(QPointF(9.5, 32.5), QPointF(32.5, 9.5))
        elif self.icon_kind == "clock":
            painter.drawEllipse(QRectF(9.5, 9.5, 23.0, 23.0))
            painter.drawLine(QPointF(21.0, 21.0), QPointF(21.0, 14.0))
            painter.drawLine(QPointF(21.0, 21.0), QPointF(26.5, 24.5))


class DashboardMetricCard(QFrame):
    TONES = {
        "teal": ("#1d8178", "#e7f6f2"),
        "green": ("#2f9d69", "#eaf7ef"),
        "orange": ("#d98b22", "#fff4df"),
        "blue": ("#3478b8", "#eaf3fb"),
        "purple": ("#7654b8", "#f2edfb"),
    }

    def __init__(self, title, icon_kind, tone="teal", parent=None):
        super().__init__(parent)
        self.setObjectName("DashboardMetricCard")
        self.setProperty("tone", tone)
        self.setMinimumHeight(104)
        accent, badge_background = self.TONES.get(tone, self.TONES["teal"])

        layout = QHBoxLayout(self)
        layout.setContentsMargins(15, 13, 15, 13)
        layout.setSpacing(12)

        self.icon_badge = DashboardMetricIcon(
            icon_kind,
            accent,
            badge_background,
        )
        layout.addWidget(self.icon_badge)

        text_layout = QVBoxLayout()
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(2)
        title_label = QLabel(title)
        title_label.setObjectName("DashboardMetricTitle")
        self.value_label = QLabel("0")
        self.value_label.setObjectName("DashboardMetricValue")
        self.value_label.setStyleSheet(f"color:{accent};")
        self.detail_label = QLabel("—")
        self.detail_label.setObjectName("DashboardMetricDetail")
        text_layout.addWidget(title_label)
        text_layout.addWidget(self.value_label)
        text_layout.addWidget(self.detail_label)
        layout.addLayout(text_layout, 1)

    def set_value(self, value, unit="", detail="", detail_color="#6f8583"):
        suffix = f" {unit}" if unit else ""
        self.value_label.setText(f"{value}{suffix}")
        self.detail_label.setText(detail or "—")
        self.detail_label.setStyleSheet(f"color:{detail_color};")


class PhoneDistributionChart(AnimatedDonutChart):
    PHONE_COLOR = QColor("#1d8178")
    NO_PHONE_COLOR = QColor("#f0ad69")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.phone_count = 0
        self.no_phone_count = 0
        self._donut_rect = QRectF()
        self._center_label_rect = QRectF()
        self._center_value_rect = QRectF()
        self.setMinimumHeight(220)

    def set_values(self, phone_count, no_phone_count):
        self.phone_count = max(0, int(phone_count or 0))
        self.no_phone_count = max(0, int(no_phone_count or 0))
        self._hovered_slice = None
        self._hover_card.hide()
        self.update()

    def paintEvent(self, event):
        self._slice_hitboxes = []
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        total = self.phone_count + self.no_phone_count
        available = self.rect().adjusted(18, 16, -18, -16)
        diameter = min(available.height(), max(140, int(available.width() * 0.46)))
        donut = QRectF(
            available.left(),
            available.center().y() - diameter / 2,
            diameter,
            diameter,
        )
        self._donut_rect = donut
        hole = donut.adjusted(
            diameter * 0.25,
            diameter * 0.25,
            -diameter * 0.25,
            -diameter * 0.25,
        )

        rows = (
            ("有电话", self.phone_count, self.PHONE_COLOR),
            ("无电话", self.no_phone_count, self.NO_PHONE_COLOR),
        )
        if total:
            start_degrees = 90.0
            for label, value, color in rows:
                span_degrees = -(value / total * 360)
                if value:
                    self._slice_hitboxes.append(
                        {
                            "outer": QRectF(donut),
                            "inner": QRectF(hole),
                            "start": start_degrees,
                            "sweep": abs(span_degrees),
                            "payload": {
                                "label": label,
                                "value": value,
                                "total": total,
                                "color": color,
                            },
                        }
                    )
                    self._draw_donut_slice(
                        painter,
                        donut,
                        hole,
                        start_degrees,
                        span_degrees,
                        color,
                        len(self._slice_hitboxes) - 1,
                    )
                start_degrees += span_degrees
        else:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor("#e7efed"))
            painter.drawEllipse(donut)
            painter.setBrush(QColor("#ffffff"))
            painter.drawEllipse(hole)

        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#ffffff"))
        painter.drawEllipse(hole)
        label_font = QFont("Microsoft YaHei UI", 9)
        value_font = QFont("Microsoft YaHei UI", 18, QFont.Bold)
        painter.setFont(label_font)
        label_height = painter.fontMetrics().height()
        painter.setFont(value_font)
        value_height = painter.fontMetrics().height()
        text_gap = 6.0
        text_top = hole.center().y() - (
            label_height + text_gap + value_height
        ) / 2
        self._center_label_rect = QRectF(
            hole.left(),
            text_top,
            hole.width(),
            label_height,
        )
        self._center_value_rect = QRectF(
            hole.left(),
            self._center_label_rect.bottom() + text_gap,
            hole.width(),
            value_height,
        )
        painter.setPen(QColor("#607a78"))
        painter.setFont(label_font)
        painter.drawText(self._center_label_rect, Qt.AlignCenter, "总查询量")
        painter.setPen(QColor("#173a3d"))
        painter.setFont(value_font)
        painter.drawText(self._center_value_rect, Qt.AlignCenter, f"{total:,}")

        legend_x = donut.right() + 24
        legend_width = max(100, available.right() - legend_x)
        start_y = available.center().y() - 38
        for index, (label, value, color) in enumerate(rows):
            y = start_y + index * 58
            painter.setPen(Qt.NoPen)
            painter.setBrush(color)
            painter.drawRoundedRect(QRectF(legend_x, y, 12, 12), 3, 3)
            share = value / total * 100 if total else 0
            painter.setPen(QColor("#526e6c"))
            painter.setFont(QFont("Microsoft YaHei UI", 9))
            painter.drawText(
                QRectF(legend_x + 20, y - 5, legend_width - 20, 22),
                Qt.AlignLeft | Qt.AlignVCenter,
                f"{label}  {value:,} 条",
            )
            painter.setPen(color)
            painter.setFont(QFont("Microsoft YaHei UI", 10, QFont.Bold))
            painter.drawText(
                QRectF(legend_x + 20, y + 16, legend_width - 20, 24),
                Qt.AlignLeft | Qt.AlignVCenter,
                f"占比 {share:.1f}%",
            )

    def mouseMoveEvent(self, event):
        index, payload = self._slice_at(event.pos())
        if payload is not None:
            self._hovered_slice = index
            share = payload["value"] / max(payload["total"], 1) * 100
            self._hover_card.show_details(
                payload["label"],
                [
                    ("数量", f'{payload["value"]:,} 条'),
                    ("占比", f"{share:.1f}%"),
                    ("总查询量", f'{payload["total"]:,} 条'),
                ],
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

    def leaveEvent(self, event):
        self._hovered_slice = None
        self._hover_card.hide()
        self.update()
        super().leaveEvent(event)

    def hideEvent(self, event):
        self._hovered_slice = None
        self._hover_card.hide()
        super().hideEvent(event)


class SevenDayTrendChart(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows = []
        self._points = []
        self._hovered_index = None
        self._hover_card = ChartHoverCard(self)
        self.setMouseTracking(True)
        self.setMinimumHeight(220)

    def set_rows(self, rows):
        self._rows = list(rows or [])
        self._points = []
        self._hovered_index = None
        self._hover_card.hide()
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        plot = QRectF(self.rect()).adjusted(48, 28, -22, -38)
        if plot.width() <= 0 or plot.height() <= 0:
            return

        values = [int(row.get("total", 0) or 0) for row in self._rows]
        maximum = max(values, default=0)
        axis_max = max(4, ((maximum + 3) // 4) * 4)
        grid_pen = QPen(QColor("#dfe9e7"), 1, Qt.DashLine)
        label_pen = QPen(QColor("#718785"))
        painter.setFont(QFont("Microsoft YaHei UI", 8))
        for index in range(5):
            y = plot.bottom() - plot.height() * index / 4
            painter.setPen(grid_pen)
            painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            painter.setPen(label_pen)
            painter.drawText(
                QRectF(0, y - 10, plot.left() - 8, 20),
                Qt.AlignRight | Qt.AlignVCenter,
                str(int(axis_max * index / 4)),
            )

        if not self._rows:
            self._points = []
            painter.setPen(QColor("#8ba19f"))
            painter.drawText(plot, Qt.AlignCenter, "暂无近七天数据")
            return

        point_count = len(self._rows)
        spacing = plot.width() / max(1, point_count - 1)
        points = []
        for index, row in enumerate(self._rows):
            value = int(row.get("total", 0) or 0)
            x = plot.left() + spacing * index
            y = plot.bottom() - value / axis_max * plot.height()
            points.append(QPointF(x, y))
            painter.setPen(label_pen)
            painter.drawText(
                QRectF(x - 30, plot.bottom() + 10, 60, 20),
                Qt.AlignCenter,
                str(row.get("label", "")),
            )
        self._points = points

        path = QPainterPath(points[0])
        for point in points[1:]:
            path.lineTo(point)
        fill_path = QPainterPath(path)
        fill_path.lineTo(points[-1].x(), plot.bottom())
        fill_path.lineTo(points[0].x(), plot.bottom())
        fill_path.closeSubpath()
        gradient = QLinearGradient(0, plot.top(), 0, plot.bottom())
        gradient.setColorAt(0, QColor(29, 129, 120, 72))
        gradient.setColorAt(1, QColor(29, 129, 120, 4))
        painter.setPen(Qt.NoPen)
        painter.setBrush(gradient)
        painter.drawPath(fill_path)

        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(QColor("#1d8178"), 2.4))
        painter.drawPath(path)
        if self._hovered_index is not None and self._hovered_index < len(points):
            hovered_point = points[self._hovered_index]
            painter.setPen(QPen(QColor("#9ccfc8"), 1, Qt.DashLine))
            painter.drawLine(
                QPointF(hovered_point.x(), plot.top()),
                QPointF(hovered_point.x(), plot.bottom()),
            )
        for index, (point, value) in enumerate(zip(points, values)):
            painter.setPen(QPen(QColor("#ffffff"), 2))
            painter.setBrush(QColor("#1d8178"))
            radius = 6.5 if index == self._hovered_index else 4.5
            painter.drawEllipse(point, radius, radius)
            painter.setPen(QColor("#315a58"))
            painter.setFont(QFont("Microsoft YaHei UI", 8, QFont.Bold))
            painter.drawText(
                QRectF(point.x() - 24, point.y() - 25, 48, 18),
                Qt.AlignCenter,
                str(value),
            )

    def mouseMoveEvent(self, event):
        nearest = None
        nearest_distance = 14 * 14
        for index, point in enumerate(self._points):
            distance = (
                (point.x() - event.pos().x()) ** 2
                + (point.y() - event.pos().y()) ** 2
            )
            if distance <= nearest_distance:
                nearest = index
                nearest_distance = distance
        if nearest is None:
            if self._hovered_index is not None:
                self._hovered_index = None
                self.update()
            self._hover_card.hide()
            return super().mouseMoveEvent(event)

        self._hovered_index = nearest
        row = self._rows[nearest]
        self._hover_card.show_details(
            f"{row.get('date', '')} 完成详情",
            [
                ("查询总量", f"{int(row.get('total', 0) or 0):,} 条"),
                ("有电话", f"{int(row.get('has_phone', 0) or 0):,} 条"),
                ("无电话", f"{int(row.get('no_phone', 0) or 0):,} 条"),
            ],
            QColor("#1d8178"),
            self.mapToGlobal(event.pos()),
            card_width=280,
        )
        self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        self._hovered_index = None
        self._hover_card.hide()
        self.update()
        super().leaveEvent(event)

    def hideEvent(self, event):
        self._hovered_index = None
        self._hover_card.hide()
        super().hideEvent(event)


class StationShareChart(AnimatedDonutChart):
    COLORS = (
        QColor("#1d8178"),
        QColor("#f0ad69"),
        QColor("#d9a52f"),
        QColor("#df6262"),
        QColor("#779996"),
        QColor("#4a8bc4"),
        QColor("#8064b6"),
        QColor("#46a576"),
    )

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows = []
        self._donuts = []
        self._legend_text_rects = []
        self.setMinimumHeight(220)

    def set_rows(self, rows):
        self._rows = list(rows or [])
        self._donuts = []
        self._legend_text_rects = []
        self._slice_hitboxes = []
        self._hovered_slice = None
        self._hover_card.hide()
        self.update()

    def _draw_donut(self, painter, donut, metric, title, center_value, center_unit):
        values = [max(0, int(row.get(metric, 0) or 0)) for row in self._rows]
        total = sum(values)
        painter.setPen(QColor("#526e6c"))
        painter.setFont(QFont("Microsoft YaHei UI", 9, QFont.Bold))
        painter.drawText(
            QRectF(donut.left() - 8, donut.top() - 28, donut.width() + 16, 22),
            Qt.AlignCenter,
            title,
        )

        diameter = min(donut.width(), donut.height())
        hole = donut.adjusted(
            diameter * 0.25,
            diameter * 0.25,
            -diameter * 0.25,
            -diameter * 0.25,
        )
        start_degrees = 90.0
        if total:
            for index, value in enumerate(values):
                span_degrees = -(value / total * 360)
                color = self.COLORS[index % len(self.COLORS)]
                if value:
                    self._slice_hitboxes.append(
                        {
                            "outer": QRectF(donut),
                            "inner": QRectF(hole),
                            "start": start_degrees,
                            "sweep": abs(span_degrees),
                            "payload": {
                                "metric": metric,
                                "row_index": index,
                                "value": value,
                                "total": total,
                                "color": color,
                            },
                        }
                    )
                    self._draw_donut_slice(
                        painter,
                        donut,
                        hole,
                        start_degrees,
                        span_degrees,
                        color,
                        len(self._slice_hitboxes) - 1,
                    )
                start_degrees += span_degrees
        else:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor("#e7efed"))
            painter.drawEllipse(donut)

        painter.setBrush(QColor("#ffffff"))
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(hole)
        painter.setPen(QColor("#173a3d"))
        painter.setFont(QFont("Microsoft YaHei UI", 15, QFont.Bold))
        painter.drawText(hole.adjusted(0, -10, 0, 0), Qt.AlignCenter, center_value)
        painter.setPen(QColor("#718785"))
        painter.setFont(QFont("Microsoft YaHei UI", 8))
        painter.drawText(hole.adjusted(0, 19, 0, 0), Qt.AlignCenter, center_unit)
        self._donuts.append(
            {
                "metric": metric,
                "rect": QRectF(donut),
                "values": values,
                "total": total,
            }
        )

    def paintEvent(self, event):
        self._slice_hitboxes = []
        self._legend_text_rects = []
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        available = QRectF(self.rect()).adjusted(8, 8, -8, -8)
        if available.width() <= 0 or available.height() <= 0:
            return

        legend_width = min(205, max(165, available.width() * 0.30))
        chart_width = max(240, available.width() - legend_width - 10)
        section_width = chart_width / 2
        diameter = min(
            max(112, available.height() - 48),
            max(112, section_width - 34),
        )
        top = available.center().y() - diameter / 2 + 10
        first = QRectF(
            available.left() + (section_width - diameter) / 2,
            top,
            diameter,
            diameter,
        )
        second = QRectF(
            available.left() + section_width + (section_width - diameter) / 2,
            top,
            diameter,
            diameter,
        )

        self._donuts = []
        total_count = sum(max(0, int(row.get("total", 0) or 0)) for row in self._rows)
        total_ms = sum(max(0, int(row.get("total_ms", 0) or 0)) for row in self._rows)
        self._draw_donut(
            painter,
            first,
            "total",
            "总数量占比",
            f"{total_count:,}",
            "条",
        )
        self._draw_donut(
            painter,
            second,
            "total_ms",
            "总用时占比",
            f"{total_ms / MILLISECONDS_PER_HOUR:.1f}",
            "小时",
        )

        legend_x = available.left() + chart_width + 12
        station_font = QFont("Microsoft YaHei UI", 8, QFont.Bold)
        detail_font = QFont("Microsoft YaHei UI", 8)
        painter.setFont(station_font)
        station_line_height = painter.fontMetrics().height()
        painter.setFont(detail_font)
        detail_line_height = painter.fontMetrics().height()
        line_gap = 1
        row_gap = 5
        row_height = station_line_height + line_gap + detail_line_height + row_gap
        footer_height = 20
        visible_count = min(6, len(self._rows))
        if visible_count * row_height > available.height():
            visible_count = max(1, int(available.height() // row_height))
        if len(self._rows) > visible_count:
            visible_count = max(
                1,
                min(
                    visible_count,
                    int(max(0, available.height() - footer_height) // row_height),
                ),
            )
        visible_rows = self._rows[:visible_count]
        legend_height = len(visible_rows) * row_height
        has_hidden_rows = len(self._rows) > len(visible_rows)
        legend_block_height = legend_height + (footer_height if has_hidden_rows else 0)
        start_y = available.center().y() - legend_block_height / 2
        for index, row in enumerate(visible_rows):
            y = start_y + index * row_height
            color = self.COLORS[index % len(self.COLORS)]
            painter.setPen(Qt.NoPen)
            painter.setBrush(color)
            painter.drawRoundedRect(
                QRectF(
                    legend_x,
                    y + max(0, (station_line_height - 10) / 2),
                    10,
                    10,
                ),
                2,
                2,
            )
            station_rect = QRectF(
                legend_x + 16,
                y,
                legend_width - 16,
                station_line_height,
            )
            detail_rect = QRectF(
                legend_x + 16,
                y + station_line_height + line_gap,
                legend_width - 16,
                detail_line_height,
            )
            self._legend_text_rects.append((station_rect, detail_rect))
            painter.setPen(QColor("#526e6c"))
            painter.setFont(station_font)
            painter.drawText(
                station_rect,
                Qt.AlignLeft | Qt.AlignVCenter,
                str(row.get("station", "")),
            )
            total_share = row.get("total", 0) / total_count * 100 if total_count else 0
            time_share = row.get("total_ms", 0) / total_ms * 100 if total_ms else 0
            painter.setPen(color)
            painter.setFont(detail_font)
            painter.drawText(
                detail_rect,
                Qt.AlignLeft | Qt.AlignVCenter,
                f"量 {total_share:.1f}% · 时 {time_share:.1f}%",
            )
        if has_hidden_rows:
            painter.setPen(QColor("#718785"))
            painter.setFont(detail_font)
            painter.drawText(
                QRectF(
                    legend_x,
                    start_y + legend_height,
                    legend_width,
                    footer_height,
                ),
                Qt.AlignLeft | Qt.AlignVCenter,
                f"另有 {len(self._rows) - len(visible_rows)} 个站点",
            )

    def mouseMoveEvent(self, event):
        slice_index, payload = self._slice_at(event.pos())
        if payload is not None:
            self._hovered_slice = slice_index
            row = self._rows[payload["row_index"]]
            share = payload["value"] / max(payload["total"], 1) * 100
            is_duration = payload["metric"] == "total_ms"
            details = (
                [
                    ("小时数", format_hours(payload["value"])),
                    ("精确耗时", format_precise_duration(payload["value"])),
                    ("占比", f"{share:.1f}%"),
                ]
                if is_duration
                else [
                    ("数量", f'{payload["value"]:,} 条'),
                    ("占比", f"{share:.1f}%"),
                ]
            )
            series = "总用时占比" if is_duration else "总数量占比"
            self._hover_card.show_details(
                f'{row["station"]} · {series}',
                details,
                payload["color"],
                event.globalPos(),
                card_width=300 if is_duration else 240,
            )
            self.update()
            return
        if self._hovered_slice is not None:
            self._hovered_slice = None
            self.update()
        self._hover_card.hide()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        self._hovered_slice = None
        self._hover_card.hide()
        self.update()
        super().leaveEvent(event)

    def hideEvent(self, event):
        self._hovered_slice = None
        self._hover_card.hide()
        super().hideEvent(event)


class DashboardPage(QWidget):
    """一眼可读的业务概览，详细统计由侧边栏数据中心承载。"""

    def __init__(
        self,
        database: Database,
        account: Account,
        parent=None,
        *,
        client_preferences=None,
        account_key=None,
    ):
        super().__init__(parent)
        self.database = database
        self.account = account
        self.client_preferences = client_preferences
        self.account_key = str(account_key or account.username or "")
        self.metric_cards = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 18, 22, 20)
        layout.setSpacing(11)

        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title = QLabel("仪表盘")
        title.setObjectName("PageTitle")
        subtitle = QLabel("运输业务核心指标概览")
        subtitle.setObjectName("Muted")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addLayout(title_box)
        header.addStretch()

        scope_group = QFrame()
        scope_group.setObjectName("DashboardFilterGroup")
        scope_layout = QHBoxLayout(scope_group)
        scope_layout.setContentsMargins(10, 5, 8, 5)
        scope_layout.setSpacing(7)
        scope_label = QLabel("统计范围")
        scope_label.setObjectName("DashboardFilterLabel")
        scope_layout.addWidget(scope_label)
        self.station_combo = QComboBox()
        self.station_combo.setMinimumWidth(145)
        self.station_combo.currentIndexChanged.connect(self.refresh)
        scope_layout.addWidget(self.station_combo)
        self.yellow_only_check = QCheckBox("只看黄牌车辆")
        saved_yellow_only = (
            self.client_preferences.statistics_yellow_only(self.account_key)
            if self.client_preferences is not None
            else True
        )
        self.yellow_only_check.setChecked(saved_yellow_only)
        self.yellow_only_check.setToolTip(
            "自动记住上次选择；历史统计数据按黄牌车辆处理"
        )
        self.yellow_only_check.toggled.connect(self._toggle_yellow_only)
        scope_layout.addWidget(self.yellow_only_check)
        header.addWidget(scope_group)

        self.updated_label = QLabel("")
        self.updated_label.setObjectName("Muted")
        header.addWidget(self.updated_label)
        refresh_button = QPushButton("刷新数据")
        refresh_button.clicked.connect(self._refresh_with_loading)
        header.addWidget(refresh_button)
        layout.addLayout(header)

        card_layout = QGridLayout()
        card_layout.setHorizontalSpacing(10)
        cards = (
            ("total", "总查询量", "search", "teal"),
            ("today", "今日查询量", "calendar", "blue"),
            ("phone", "有电话数量", "phone", "green"),
            ("no_phone", "无电话数量", "phone-off", "orange"),
            ("time", "总用时", "clock", "purple"),
        )
        for column, (key, title_text, icon_kind, tone) in enumerate(cards):
            card = DashboardMetricCard(title_text, icon_kind, tone)
            self.metric_cards[key] = card
            card_layout.addWidget(card, 0, column)
            card_layout.setColumnStretch(column, 1)
        layout.addLayout(card_layout)

        analysis_layout = QGridLayout()
        analysis_layout.setHorizontalSpacing(10)
        analysis_layout.setVerticalSpacing(10)
        analysis_layout.setColumnStretch(0, 4)
        analysis_layout.setColumnStretch(1, 6)

        phone_panel, phone_body = self._panel("电话数据占比")
        self.phone_chart = PhoneDistributionChart()
        phone_body.addWidget(self.phone_chart)
        analysis_layout.addWidget(phone_panel, 0, 0)

        trend_panel, trend_body = self._panel("近 7 天完成趋势")
        self.trend_chart = SevenDayTrendChart()
        trend_body.addWidget(self.trend_chart)
        analysis_layout.addWidget(trend_panel, 0, 1)

        violation_panel, violation_body = self._panel("违规原因 TOP5")
        self.violation_table = self._table(
            ["排名", "违规原因", "数量", "占比"]
        )
        violation_body.addWidget(self.violation_table)
        analysis_layout.addWidget(violation_panel, 1, 0)

        self.station_today_toggle = QPushButton("查看今日统计")
        self.station_today_toggle.setObjectName("DashboardPanelAction")
        self.station_today_toggle.setCheckable(True)
        self.station_today_toggle.setToolTip(
            "仅切换各站查询统计的今日/累计口径"
        )
        self.station_view_toggle = QPushButton("查看占比图")
        self.station_view_toggle.setObjectName("DashboardPanelAction")
        self.station_view_toggle.setCheckable(True)
        station_actions = QWidget()
        station_actions_layout = QHBoxLayout(station_actions)
        station_actions_layout.setContentsMargins(0, 0, 0, 0)
        station_actions_layout.setSpacing(8)
        station_actions_layout.addWidget(self.station_today_toggle)
        station_actions_layout.addWidget(self.station_view_toggle)
        station_panel, station_body = self._panel(
            "各站查询统计",
            station_actions,
        )
        self.station_view_stack = QStackedWidget()
        self.station_table = self._table(
            ["站点", "查询量", "有电话", "无电话", "总用时"]
        )
        self.station_share_chart = StationShareChart()
        self.station_view_stack.addWidget(self.station_table)
        self.station_view_stack.addWidget(self.station_share_chart)
        station_body.addWidget(self.station_view_stack)
        saved_station_view = (
            self.client_preferences.dashboard_station_view(self.account_key)
            if self.client_preferences is not None
            else "table"
        )
        self.station_view_toggle.setChecked(saved_station_view == "chart")
        self._toggle_station_view(
            saved_station_view == "chart",
            persist=False,
        )
        self.station_view_toggle.toggled.connect(self._toggle_station_view)
        self.station_today_toggle.toggled.connect(self._toggle_station_today)
        analysis_layout.addWidget(station_panel, 1, 1)
        analysis_layout.setRowStretch(0, 1)
        analysis_layout.setRowStretch(1, 1)
        layout.addLayout(analysis_layout, 1)

        self.refresh()

    def _refresh_with_loading(self, _checked=False):
        return run_ui_with_loading(self, "正在加载数据…", self.refresh)

    @staticmethod
    def _panel(title, action=None):
        panel = QFrame()
        panel.setObjectName("DashboardPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(13, 11, 13, 12)
        layout.setSpacing(8)
        heading_layout = QHBoxLayout()
        heading_layout.setContentsMargins(0, 0, 0, 0)
        heading_layout.setSpacing(8)
        heading = QLabel(title)
        heading.setObjectName("DashboardPanelTitle")
        heading_layout.addWidget(heading)
        heading_layout.addStretch()
        if action is not None:
            heading_layout.addWidget(action)
        layout.addLayout(heading_layout)
        body = QVBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        layout.addLayout(body, 1)
        return panel, body

    def _toggle_station_view(self, show_charts, *, persist=True):
        self.station_view_stack.setCurrentIndex(1 if show_charts else 0)
        self.station_view_toggle.setText(
            "查看数据表" if show_charts else "查看占比图"
        )
        if persist and self.client_preferences is not None and self.account_key:
            self.client_preferences.set_dashboard_station_view(
                self.account_key,
                "chart" if show_charts else "table",
            )

    def _toggle_station_today(self, today_only):
        self.station_today_toggle.setText(
            "查看累计统计" if today_only else "查看今日统计"
        )
        self._refresh_station_statistics()

    def _toggle_yellow_only(self, checked):
        if self.client_preferences is not None and self.account_key:
            self.client_preferences.set_statistics_yellow_only(
                self.account_key,
                bool(checked),
            )
        self.refresh()

    @staticmethod
    def _table(headers):
        table = QTableWidget()
        table.setObjectName("DashboardTable")
        table.setColumnCount(len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setSelectionMode(QAbstractItemView.NoSelection)
        table.setAlternatingRowColors(True)
        table.setShowGrid(False)
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setDefaultSectionSize(34)
        table.horizontalHeader().setMinimumHeight(34)
        make_table_columns_resizable(table)
        return table

    def _populate_station_filter(self):
        had_options = self.station_combo.count() > 0
        current_id = self.station_combo.currentData()
        default_order = {
            username: index for index, (_, username) in enumerate(DEFAULT_STATION_USERS)
        }
        stations = [
            account
            for account in self.database.list_accounts()
            if account.is_station and not account.is_test
        ]
        restricted_online_scope = (
            self.account.server_account_id is not None
            and not self.account.can_view_shared_stats
        )
        hidden_non_statistic_scope = (
            not self.account.statistics_enabled
            and not self.account.can_view_shared_stats
        )
        if restricted_online_scope:
            stations = [
                account for account in stations if account.id == self.account.id
            ]
        stations.sort(
            key=lambda item: (
                default_order.get(item.username, 999),
                item.name_label,
            )
        )

        self.station_combo.blockSignals(True)
        self.station_combo.clear()
        aggregate_label = (
            "本路段"
            if self.account.effective_data_scope == "road"
            else "全部站点"
        )
        self.station_combo.addItem(
            "不参与统计" if hidden_non_statistic_scope else aggregate_label,
            self.account.id if hidden_non_statistic_scope else None,
        )
        for station in stations:
            self.station_combo.addItem(station.name_label, station.id)
        available_ids = {station.id for station in stations}
        if hidden_non_statistic_scope:
            target_id = self.account.id
        elif had_options and current_id in available_ids:
            target_id = current_id
        elif had_options and current_id is None:
            target_id = None
        else:
            target_id = (
                None if self.account.can_view_shared_stats else self.account.id
            )
        target_index = self.station_combo.findData(target_id)
        self.station_combo.setCurrentIndex(target_index if target_index >= 0 else 0)
        self.station_combo.setEnabled(not restricted_online_scope)
        self.station_combo.blockSignals(False)

    def _scope(self):
        user_id = self.station_combo.currentData()
        return (int(user_id), False) if user_id is not None else (None, True)

    def _metric_totals(self, start_date=None, end_date=None):
        user_id, users_only = self._scope()
        yellow_only = self.yellow_only_check.isChecked()
        if users_only:
            totals = {}
            for row in self.database.get_all_account_totals(
                start_date,
                end_date,
                yellow_only=yellow_only,
            ):
                if row["role"] != "user" or row["account_type"] != "station":
                    continue
                totals[row["metric_key"]] = (
                    totals.get(row["metric_key"], 0) + int(row["total"])
                )
            return totals
        return {
            row["metric_key"]: int(row["total"])
            for row in self.database.get_user_totals(
                user_id,
                start_date,
                end_date,
                yellow_only=yellow_only,
            )
        }

    @staticmethod
    def _comparison_text(today_total, yesterday_total):
        if yesterday_total:
            change = (today_total - yesterday_total) / yesterday_total * 100
            sign = "+" if change >= 0 else ""
            color = "#2f9d69" if change >= 0 else "#c35d45"
            return f"较昨日 {sign}{change:.1f}%", color
        difference = today_total - yesterday_total
        sign = "+" if difference > 0 else ""
        return f"较昨日 {sign}{difference} 条", "#6f8583"

    def _station_rows(self, start_date=None, end_date=None):
        default_order = {
            username: index for index, (_, username) in enumerate(DEFAULT_STATION_USERS)
        }
        stations = {}
        yellow_only = self.yellow_only_check.isChecked()
        for row in self.database.get_all_account_totals(
            start_date,
            end_date,
            yellow_only=yellow_only,
        ):
            if row["role"] != "user" or row["account_type"] != "station":
                continue
            station = stations.setdefault(
                row["user_id"],
                {
                    "user_id": row["user_id"],
                    "username": row["username"],
                    "station": row["display_name"] or row["username"],
                    "total": 0,
                    "phone": 0,
                },
            )
            if row["metric_key"] == WORKFLOW_TOTAL_METRIC:
                station["total"] = int(row["total"])
            elif row["metric_key"] == WORKFLOW_HAS_PHONE_METRIC:
                station["phone"] = int(row["total"])
        rows = list(stations.values())
        rows.sort(
            key=lambda item: (
                default_order.get(item["username"], 999),
                item["station"],
            )
        )
        for row in rows:
            timing = self.database.get_workflow_timing_totals(
                row["user_id"],
                start_date=start_date,
                end_date=end_date,
                yellow_only=yellow_only,
            )
            row["no_phone"] = max(0, row["total"] - row["phone"])
            row["total_ms"] = int(timing["total_ms"])
        return rows

    def _refresh_station_statistics(self):
        today = date.today() if self.station_today_toggle.isChecked() else None
        station_rows = self._station_rows(start_date=today, end_date=today)
        self.station_share_chart.set_rows(station_rows)
        self.station_table.setRowCount(len(station_rows))
        for row_index, row in enumerate(station_rows):
            values = (
                row["station"],
                f"{row['total']:,}",
                f"{row['phone']:,}",
                f"{row['no_phone']:,}",
                f"{row['total_ms'] / MILLISECONDS_PER_HOUR:.1f} 小时",
            )
            for column, value in enumerate(values):
                self._set_table_item(
                    self.station_table,
                    row_index,
                    column,
                    value,
                    Qt.AlignCenter if column else Qt.AlignLeft | Qt.AlignVCenter,
                    "#1d8178" if column in (1, 2) else None,
                    bold=column == 1,
                )

    @staticmethod
    def _set_table_item(table, row, column, value, alignment=None, color=None, bold=False):
        item = QTableWidgetItem(str(value))
        if alignment is not None:
            item.setTextAlignment(alignment)
        if color:
            item.setForeground(QColor(color))
        if bold:
            font = item.font()
            font.setBold(True)
            item.setFont(font)
        table.setItem(row, column, item)

    def refresh(self):
        self._populate_station_filter()
        today = date.today()
        yesterday = today - timedelta(days=1)
        week_start = today - timedelta(days=6)
        totals = self._metric_totals()
        total_count = int(totals.get(WORKFLOW_TOTAL_METRIC, 0))
        phone_count = int(totals.get(WORKFLOW_HAS_PHONE_METRIC, 0))
        no_phone_count = max(0, total_count - phone_count)

        user_id, users_only = self._scope()
        daily_rows = self.database.get_daily_metric_totals(
            WORKFLOW_TOTAL_METRIC,
            user_id=user_id,
            users_only=users_only,
            start_date=week_start,
            end_date=today,
            yellow_only=self.yellow_only_check.isChecked(),
        )
        daily_phone_rows = self.database.get_daily_metric_totals(
            WORKFLOW_HAS_PHONE_METRIC,
            user_id=user_id,
            users_only=users_only,
            start_date=week_start,
            end_date=today,
            yellow_only=self.yellow_only_check.isChecked(),
        )
        daily_totals = {row["date"]: row["total"] for row in daily_rows}
        daily_phone_totals = {
            row["date"]: row["total"] for row in daily_phone_rows
        }
        today_total = int(daily_totals.get(today.isoformat(), 0))
        yesterday_total = int(daily_totals.get(yesterday.isoformat(), 0))
        comparison, comparison_color = self._comparison_text(
            today_total,
            yesterday_total,
        )
        timing = self.database.get_workflow_timing_totals(
            user_id,
            users_only=users_only,
            yellow_only=self.yellow_only_check.isChecked(),
        )

        phone_share = phone_count / total_count * 100 if total_count else 0
        no_phone_share = no_phone_count / total_count * 100 if total_count else 0
        self.metric_cards["total"].set_value(
            f"{total_count:,}", detail="累计完成数据"
        )
        self.metric_cards["today"].set_value(
            f"{today_total:,}", detail=comparison, detail_color=comparison_color
        )
        self.metric_cards["phone"].set_value(
            f"{phone_count:,}", detail=f"占比 {phone_share:.1f}%"
        )
        self.metric_cards["no_phone"].set_value(
            f"{no_phone_count:,}", detail=f"占比 {no_phone_share:.1f}%"
        )
        self.metric_cards["time"].set_value(
            f"{timing['total_ms'] / MILLISECONDS_PER_HOUR:.1f}",
            unit="小时",
            detail=f"有效用时 {timing['active_ms'] / MILLISECONDS_PER_HOUR:.1f} 小时",
        )
        self.phone_chart.set_values(phone_count, no_phone_count)

        trend_rows = []
        for offset in range(7):
            current = week_start + timedelta(days=offset)
            current_total = int(daily_totals.get(current.isoformat(), 0))
            current_phone = int(
                daily_phone_totals.get(current.isoformat(), 0)
            )
            trend_rows.append(
                {
                    "date": current.isoformat(),
                    "label": current.strftime("%m-%d"),
                    "total": current_total,
                    "has_phone": current_phone,
                    "no_phone": max(0, current_total - current_phone),
                }
            )
        self.trend_chart.set_rows(trend_rows)

        all_violation_rows = self.database.get_violation_totals(
            user_id,
            users_only=users_only,
            yellow_only=self.yellow_only_check.isChecked(),
        )
        violation_total = sum(row["total"] for row in all_violation_rows)
        violation_rows = all_violation_rows[:5]
        self.violation_table.setRowCount(len(violation_rows))
        for row_index, row in enumerate(violation_rows):
            share = row["total"] / violation_total * 100 if violation_total else 0
            values = (row_index + 1, row["reason"], row["total"], f"{share:.1f}%")
            for column, value in enumerate(values):
                self._set_table_item(
                    self.violation_table,
                    row_index,
                    column,
                    value,
                    Qt.AlignCenter if column != 1 else Qt.AlignLeft | Qt.AlignVCenter,
                    "#1d8178" if column in (2, 3) else None,
                    bold=column == 2,
                )

        self._refresh_station_statistics()

        self.updated_label.setText(
            f"更新于 {datetime.now():%Y-%m-%d %H:%M:%S}"
        )

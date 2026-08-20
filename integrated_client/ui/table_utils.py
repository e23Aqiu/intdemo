from __future__ import annotations

from collections.abc import Mapping, Sequence

from PyQt5 import sip
from PyQt5.QtCore import QEvent, QObject, Qt, QTimer
from PyQt5.QtWidgets import QAbstractItemView, QHeaderView


class _TableColumnWidthController(QObject):
    """Fit columns once, then preserve interactive user resizing."""

    def __init__(self, table):
        super().__init__(table)
        self.table = table
        self.initial_widths: dict[int, int] = {}
        self.minimum_width = 72
        self._fitted = False
        self._fitted_column_count = 0
        self._fit_scheduled = False
        self._model = None
        table.installEventFilter(self)
        table.viewport().installEventFilter(self)
        table.destroyed.connect(self._table_destroyed)

    def _table_is_alive(self):
        return self.table is not None and not sip.isdeleted(self.table)

    def _disconnect_model(self):
        model = self._model
        self._model = None
        if model is None or sip.isdeleted(model):
            return
        for signal_name in ("modelReset", "columnsInserted", "columnsRemoved"):
            try:
                getattr(model, signal_name).disconnect(
                    self._model_structure_changed
                )
            except (TypeError, RuntimeError):
                pass

    def _table_destroyed(self, *_args):
        self._disconnect_model()
        self.table = None
        self._fit_scheduled = False

    def configure(self, initial_widths, minimum_width):
        self.initial_widths = dict(initial_widths)
        self.minimum_width = max(1, int(minimum_width))
        self._fitted = False
        self._fitted_column_count = 0
        self._connect_model()
        self.schedule_fit()

    def _column_count(self):
        if not self._table_is_alive():
            return 0
        try:
            column_count = getattr(self.table, "columnCount", None)
            if callable(column_count):
                return int(column_count())
            model = self.table.model()
            return int(model.columnCount()) if model is not None else 0
        except RuntimeError:
            return 0

    def _connect_model(self):
        if not self._table_is_alive():
            self._disconnect_model()
            return
        try:
            model = self.table.model()
        except RuntimeError:
            self._disconnect_model()
            return
        if model is None:
            self._disconnect_model()
            return
        if model is self._model and not sip.isdeleted(model):
            return
        self._disconnect_model()
        self._model = model
        try:
            model.modelReset.connect(self._model_structure_changed)
            model.columnsInserted.connect(self._model_structure_changed)
            model.columnsRemoved.connect(self._model_structure_changed)
        except RuntimeError:
            self._model = None

    def _model_structure_changed(self, *_args):
        if not self._table_is_alive():
            self._disconnect_model()
            return
        if self._column_count() != self._fitted_column_count:
            self._fitted = False
            self.schedule_fit()

    def refit(self):
        if not self._table_is_alive():
            return
        self._fitted = False
        self._fitted_column_count = 0
        self.schedule_fit()

    def schedule_fit(self):
        if self._fit_scheduled or not self._table_is_alive():
            return
        self._fit_scheduled = True
        QTimer.singleShot(0, self.fit_once)

    @staticmethod
    def _distribute_widths(requested, available, minimum):
        count = len(requested)
        if not count:
            return []
        minimum_total = minimum * count
        if available <= minimum_total:
            return [minimum] * count

        remaining = available - minimum_total
        weights = [max(1, int(width) - minimum) for width in requested]
        weight_total = sum(weights)
        additions = [remaining * weight // weight_total for weight in weights]
        widths = [minimum + addition for addition in additions]
        for index in range(available - sum(widths)):
            widths[index % count] += 1
        return widths

    def fit_once(self):
        self._fit_scheduled = False
        if not self._table_is_alive():
            return
        self._connect_model()
        column_count = self._column_count()
        if self._fitted or column_count <= 0:
            return
        viewport_width = int(self.table.viewport().width())
        if viewport_width <= 1 or not self.table.isVisible():
            return

        header = self.table.horizontalHeader()
        available = max(1, viewport_width - 1)
        # Keep ordinary tables comfortably resizable, but lower the minimum
        # proportionally in narrow views so their initial layout never spills
        # outside the frame merely because it contains many columns.
        effective_minimum = min(
            self.minimum_width,
            max(24, available // column_count),
        )
        header.setMinimumSectionSize(effective_minimum)
        requested = [
            max(effective_minimum, int(self.initial_widths.get(column, 110)))
            for column in range(column_count)
        ]
        widths = self._distribute_widths(
            requested,
            available,
            effective_minimum,
        )
        header.setStretchLastSection(False)
        try:
            for column, width in enumerate(widths):
                header.resizeSection(column, width)
        finally:
            header.setStretchLastSection(True)
        self._fitted = True
        self._fitted_column_count = column_count

    def eventFilter(self, watched, event):
        if not self._table_is_alive():
            return False
        if event.type() in {QEvent.Show, QEvent.Resize, QEvent.LayoutRequest}:
            if not self._fitted:
                self._connect_model()
                self.schedule_fit()
        return super().eventFilter(watched, event)


def make_table_columns_resizable(
    table,
    initial_widths: Sequence[int] | Mapping[int, int] | None = None,
    *,
    minimum_width: int = 72,
) -> None:
    """Apply the application's fill-without-gaps interactive table policy."""

    header = table.horizontalHeader()
    header.setMinimumSectionSize(int(minimum_width))
    header.setStretchLastSection(True)
    header.setSectionResizeMode(QHeaderView.Interactive)
    header.setHighlightSections(False)

    table.setShowGrid(False)
    table.setWordWrap(False)
    table.setTextElideMode(Qt.ElideRight)
    table.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
    table.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)

    if initial_widths is None:
        widths = {}
    elif isinstance(initial_widths, Mapping):
        widths = {int(column): int(width) for column, width in initial_widths.items()}
    else:
        widths = {column: int(width) for column, width in enumerate(initial_widths)}

    controller = getattr(table, "_intdemo_column_width_controller", None)
    if controller is None:
        controller = _TableColumnWidthController(table)
        table._intdemo_column_width_controller = controller
    controller.configure(widths, minimum_width)


def refit_table_columns(table) -> None:
    """Restore the default fill-without-gaps layout after loading new data."""
    controller = getattr(table, "_intdemo_column_width_controller", None)
    if controller is not None:
        controller.refit()

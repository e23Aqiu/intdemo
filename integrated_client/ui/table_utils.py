from __future__ import annotations

from collections.abc import Mapping, Sequence

from PyQt5.QtWidgets import QHeaderView


def make_table_columns_resizable(
    table,
    initial_widths: Sequence[int] | Mapping[int, int] | None = None,
    *,
    minimum_width: int = 72,
) -> None:
    """Give every data column a useful initial width and a draggable divider."""

    header = table.horizontalHeader()
    header.setMinimumSectionSize(int(minimum_width))
    header.setStretchLastSection(False)
    header.setSectionResizeMode(QHeaderView.Interactive)

    if initial_widths is None:
        widths = {}
    elif isinstance(initial_widths, Mapping):
        widths = initial_widths
    else:
        widths = dict(enumerate(initial_widths))

    metrics = header.fontMetrics()
    for column in range(table.columnCount()):
        item = table.horizontalHeaderItem(column)
        label_width = metrics.horizontalAdvance(item.text()) + 34 if item else 0
        requested = int(widths.get(column, max(110, label_width)))
        header.resizeSection(column, max(int(minimum_width), requested, label_width))

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5 import sip
from PyQt5.QtCore import QCoreApplication, QEvent
from PyQt5.QtGui import QStandardItemModel
from PyQt5.QtWidgets import QApplication, QTableView

from integrated_client.ui.table_utils import make_table_columns_resizable


class TableUtilsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_model_signal_is_safe_after_table_has_been_deleted(self):
        model = QStandardItemModel(1, 1)
        table = QTableView()
        table.setModel(model)
        make_table_columns_resizable(table)
        controller = table._intdemo_column_width_controller

        table.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.app.processEvents()

        self.assertTrue(sip.isdeleted(table))
        controller._model_structure_changed()
        controller.fit_once()
        model.setColumnCount(2)


if __name__ == "__main__":
    unittest.main()

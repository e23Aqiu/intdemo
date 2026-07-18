from PyQt5.QtCore import QEvent, QObject, Qt, QTimer
from PyQt5.QtGui import QCursor
from PyQt5.QtWidgets import QApplication, QWidget


class DisabledCursorFilter(QObject):
    """让所有禁用控件统一显示禁止指针，并在启用后恢复原指针。"""

    MANAGED_PROPERTY = "_intdemo_disabled_cursor_managed"
    PREVIOUS_CURSOR_PROPERTY = "_intdemo_previous_cursor"
    PREVIOUS_EXPLICIT_PROPERTY = "_intdemo_previous_cursor_explicit"
    SYNC_EVENTS = {
        QEvent.EnabledChange,
        QEvent.Enter,
        QEvent.Polish,
        QEvent.Show,
    }

    def __init__(self, application):
        super().__init__(application)
        self.application = application
        self._override_active = False
        self._hover_timer = QTimer(self)
        self._hover_timer.setInterval(40)
        self._hover_timer.timeout.connect(self._sync_hover_cursor)
        self._hover_timer.start()
        application.aboutToQuit.connect(self.stop)

    @classmethod
    def sync_widget(cls, widget):
        managed = bool(widget.property(cls.MANAGED_PROPERTY))
        if not widget.isEnabled() and not managed:
            widget.setProperty(
                cls.PREVIOUS_EXPLICIT_PROPERTY,
                widget.testAttribute(Qt.WA_SetCursor),
            )
            widget.setProperty(cls.PREVIOUS_CURSOR_PROPERTY, widget.cursor())
            widget.setCursor(Qt.ForbiddenCursor)
            widget.setProperty(cls.MANAGED_PROPERTY, True)
        elif widget.isEnabled() and managed:
            previous_cursor = widget.property(cls.PREVIOUS_CURSOR_PROPERTY)
            if (
                widget.property(cls.PREVIOUS_EXPLICIT_PROPERTY)
                and previous_cursor is not None
            ):
                widget.setCursor(previous_cursor)
            else:
                widget.unsetCursor()
            widget.setProperty(cls.MANAGED_PROPERTY, False)
            widget.setProperty(cls.PREVIOUS_CURSOR_PROPERTY, None)
            widget.setProperty(cls.PREVIOUS_EXPLICIT_PROPERTY, None)

    def eventFilter(self, watched, event):
        if not isinstance(watched, QWidget):
            return False
        if event.type() in self.SYNC_EVENTS:
            self.sync_widget(watched)
            if event.type() == QEvent.EnabledChange:
                for child in watched.findChildren(QWidget):
                    self.sync_widget(child)
        elif event.type() == QEvent.ChildAdded:
            child = event.child()
            if isinstance(child, QWidget):
                self.sync_widget(child)
        return False

    def sync_hover_target(self, target):
        """强制覆盖系统指针，绕过 Windows 不向禁用控件派发进入事件的问题。"""
        disabled = isinstance(target, QWidget) and not target.isEnabled()
        if disabled and not self._override_active:
            QApplication.setOverrideCursor(QCursor(Qt.ForbiddenCursor))
            self._override_active = True
        elif not disabled and self._override_active:
            QApplication.restoreOverrideCursor()
            self._override_active = False

    def _sync_hover_cursor(self):
        self.sync_hover_target(QApplication.widgetAt(QCursor.pos()))

    def stop(self):
        self._hover_timer.stop()
        if self._override_active:
            QApplication.restoreOverrideCursor()
            self._override_active = False


def install_disabled_cursor_filter(application):
    """为当前 QApplication 安装一次全局禁用指针规则。"""
    filter_instance = getattr(application, "_intdemo_disabled_cursor_filter", None)
    if filter_instance is None:
        filter_instance = DisabledCursorFilter(application)
        application.installEventFilter(filter_instance)
        application._intdemo_disabled_cursor_filter = filter_instance
    for widget in application.allWidgets():
        filter_instance.sync_widget(widget)
    return filter_instance


APP_STYLESHEET = """
QWidget {
    color: #243047;
    font-family: "Microsoft YaHei UI", "Microsoft YaHei", sans-serif;
    font-size: 13px;
}
QMainWindow, QDialog {
    background: #f4f7fb;
}
QFrame#Sidebar {
    background: #17233c;
    border: none;
}
QLabel#BrandTitle {
    color: white;
    font-size: 20px;
    font-weight: 700;
}
QLabel#BrandSubTitle, QLabel#SidebarUser {
    color: #aebbd1;
}
QPushButton#NavButton {
    color: #cbd5e5;
    background: transparent;
    border: none;
    border-radius: 8px;
    padding: 11px 14px;
    text-align: left;
    font-size: 14px;
}
QPushButton#NavButton:hover {
    background: #243453;
    color: white;
}
QPushButton#NavButton:checked {
    background: #3478f6;
    color: white;
    font-weight: 700;
}
QFrame#TopBar, QFrame#Card, QGroupBox {
    background: white;
    border: 1px solid #e4eaf2;
    border-radius: 10px;
}
QFrame#TopBar {
    border-radius: 0;
    border-left: none;
    border-right: none;
    border-top: none;
}
QFrame#Card {
    padding: 8px;
}
QLabel#PageTitle {
    font-size: 21px;
    font-weight: 700;
    color: #17233c;
}
QLabel#Muted {
    color: #708096;
}
QLabel#MetricValue {
    color: #1c5ed6;
    font-size: 28px;
    font-weight: 700;
}
QGroupBox {
    margin-top: 12px;
    padding: 14px 10px 10px 10px;
    font-weight: 600;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 5px;
}
QLineEdit, QComboBox, QTextEdit, QTableWidget, QTableView {
    background: white;
    border: 1px solid #d9e1ec;
    border-radius: 6px;
    padding: 6px;
    selection-background-color: #3478f6;
}
QLineEdit:focus, QComboBox:focus, QTextEdit:focus {
    border: 1px solid #3478f6;
}
QPushButton {
    background: #eef3fa;
    border: 1px solid #d9e1ec;
    border-radius: 6px;
    padding: 7px 14px;
}
QPushButton:hover {
    background: #e2eaf5;
}
QPushButton#ViolationModeButton:checked {
    color: white;
    background: #3478f6;
    border-color: #3478f6;
    font-weight: 600;
}
QPushButton#ViolationModeButton:checked:hover {
    background: #2868db;
}
QPushButton#PrimaryButton {
    color: white;
    background: #3478f6;
    border-color: #3478f6;
    font-weight: 600;
}
QPushButton#PrimaryButton:hover {
    background: #2868db;
}
QPushButton#DangerButton {
    color: white;
    background: #e45454;
    border-color: #e45454;
}
QHeaderView::section {
    background: #f1f5fa;
    color: #526177;
    border: none;
    border-bottom: 1px solid #dce4ef;
    padding: 8px;
    font-weight: 600;
}
QTableWidget, QTableView {
    gridline-color: #edf1f6;
    alternate-background-color: #f8fafd;
    selection-color: white;
}
QTabWidget#DashboardTabs::pane {
    background: white;
    border: 1px solid #dfe6ef;
    border-radius: 9px;
    top: -1px;
}
QTabWidget#DashboardTabs QTabBar::tab {
    color: #64748b;
    background: #eaf0f7;
    border: 1px solid #d9e2ee;
    border-bottom: none;
    border-top-left-radius: 7px;
    border-top-right-radius: 7px;
    min-width: 118px;
    padding: 9px 18px;
    margin-right: 4px;
}
QTabWidget#DashboardTabs QTabBar::tab:hover {
    color: #245fc7;
    background: #f1f5fb;
}
QTabWidget#DashboardTabs QTabBar::tab:selected {
    color: #1c5ed6;
    background: white;
    border-color: #cfd9e7;
    font-weight: 700;
}
QProgressBar {
    border: 1px solid #d9e1ec;
    border-radius: 6px;
    background: white;
    text-align: center;
    min-height: 18px;
}
QProgressBar::chunk {
    background: #38b779;
    border-radius: 5px;
}
QScrollBar:vertical {
    background: #f1f4f8;
    width: 10px;
    margin: 0;
}
QScrollBar::handle:vertical {
    background: #bcc8d8;
    border-radius: 5px;
    min-height: 28px;
}
"""

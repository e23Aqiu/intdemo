import sys
from pathlib import Path

from PyQt5.QtCore import QEvent, QObject, QPoint, QRectF, Qt, QTimer
from PyQt5.QtGui import QCursor, QPainterPath, QRegion
from PyQt5.QtWidgets import QApplication, QComboBox, QFrame, QWidget


class DisabledCursorFilter(QObject):
    """让所有禁用控件统一显示禁止指针，并在启用后恢复原指针。"""

    MANAGED_PROPERTY = "_intdemo_disabled_cursor_managed"
    PREVIOUS_CURSOR_PROPERTY = "_intdemo_previous_cursor"
    PREVIOUS_EXPLICIT_PROPERTY = "_intdemo_previous_cursor_explicit"
    COMBO_OWNER_PROPERTY = "_intdemo_combo_owner"
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
        if isinstance(widget, QComboBox):
            popup = widget.view().window()
            if isinstance(popup, QFrame):
                popup.setFrameShape(QFrame.NoFrame)
                popup.setLineWidth(0)
                popup.setContentsMargins(0, 0, 0, 0)
                popup.setWindowFlag(Qt.FramelessWindowHint, True)
                popup.setWindowFlag(Qt.NoDropShadowWindowHint, True)
                popup.setAttribute(Qt.WA_TranslucentBackground, True)
                popup.setProperty(cls.COMBO_OWNER_PROPERTY, widget)
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
            if event.type() == QEvent.Show:
                for combo_child in watched.findChildren(QComboBox):
                    self.sync_widget(combo_child)
                combo = watched.property(self.COMBO_OWNER_PROPERTY)
                if isinstance(combo, QComboBox):
                    QTimer.singleShot(
                        0,
                        lambda popup=watched, owner=combo: self._position_combo_popup(
                            popup, owner
                        ),
                    )
            if event.type() == QEvent.EnabledChange:
                for child in watched.findChildren(QWidget):
                    self.sync_widget(child)
        elif event.type() == QEvent.ChildAdded:
            child = event.child()
            if isinstance(child, QWidget):
                self.sync_widget(child)
        return False

    @staticmethod
    def _position_combo_popup(popup, combo):
        """将列表向控件方向微移，用弹窗内边距抵消可见空隙。"""
        try:
            if not popup.isVisible() or not combo.isVisible():
                return
            combo_top_left = combo.mapToGlobal(QPoint(0, 0))
            screen = QApplication.screenAt(combo_top_left)
            if screen is None:
                screen = QApplication.primaryScreen()
            available = screen.availableGeometry()
            view_margins = combo.view().contentsMargins()
            top_overlap = view_margins.top() + 1
            bottom_overlap = view_margins.bottom() + 1

            x = combo_top_left.x()
            if x + popup.width() > available.right() + 1:
                x = available.right() - popup.width() + 1
            x = max(available.left(), x)

            below_y = combo_top_left.y() + combo.height() - top_overlap
            above_y = combo_top_left.y() - popup.height() + bottom_overlap
            if below_y + popup.height() <= available.bottom() + 1:
                y = below_y
            elif above_y >= available.top():
                y = above_y
            else:
                y = max(
                    available.top(),
                    min(below_y, available.bottom() - popup.height() + 1),
                )
            popup.move(x, y)
            path = QPainterPath()
            path.addRoundedRect(
                QRectF(popup.rect()).adjusted(0, 0, -0.5, -0.5),
                8,
                8,
            )
            popup.setMask(QRegion(path.toFillPolygon().toPolygon()))
        except RuntimeError:
            pass

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


def _control_asset_path(name):
    if getattr(sys, "frozen", False):
        base = Path(sys._MEIPASS) / "integrated_client" / "ui" / "assets"
    else:
        base = Path(__file__).resolve().parent / "assets"
    return (base / name).as_posix()


APP_STYLESHEET = """
QWidget {
    color: #243047;
    font-family: "Microsoft YaHei UI", "Microsoft YaHei", "Noto Sans CJK SC", "WenQuanYi Micro Hei", "DejaVu Sans", sans-serif;
    font-size: 13px;
}
QMainWindow, QDialog {
    background: #f4f7fb;
}
QFrame#Sidebar {
    background: qlineargradient(
        x1: 0, y1: 0, x2: 0, y2: 1,
        stop: 0 #123f42,
        stop: 0.58 #0d3439,
        stop: 1 #092a31
    );
    border: none;
    border-right: 1px solid #1f4e51;
}
QLabel#BrandBadge {
    color: #17393b;
    background: #f0ad69;
    border: 1px solid #ffc98f;
    border-radius: 13px;
    font-size: 20px;
    font-weight: 800;
}
QLabel#BrandTitle {
    color: #f4fbfa;
    font-size: 17px;
    font-weight: 700;
}
QLabel#BrandSubTitle {
    color: #8eb7b5;
    font-size: 10px;
    font-weight: 600;
}
QLabel#SidebarSectionLabel {
    color: #729b99;
    padding: 0 10px;
    font-size: 11px;
    font-weight: 700;
}
QFrame#SidebarSyncCard {
    background: #0e373b;
    border: 1px solid #28565a;
    border-radius: 10px;
}
QFrame#SidebarSyncCard[state="online"] {
    border-color: #287565;
    background: #103c3d;
}
QFrame#SidebarSyncCard[state="offline"],
QFrame#SidebarSyncCard[state="error"],
QFrame#SidebarSyncCard[state="reauth_required"] {
    border-color: #76584b;
}
QLabel#SyncStateDot {
    color: #78a4a2;
    border: none;
    background: transparent;
    font-size: 11px;
}
QLabel#SyncStateDot[state="online"] { color: #59d3a6; }
QLabel#SyncStateDot[state="syncing"] { color: #74b9ff; }
QLabel#SyncStateDot[state="offline"] { color: #f0ad69; }
QLabel#SyncStateDot[state="error"],
QLabel#SyncStateDot[state="reauth_required"] { color: #ff787d; }
QLabel#SyncStateTitle {
    color: #eef9f7;
    border: none;
    background: transparent;
    font-size: 11px;
    font-weight: 700;
}
QLabel#SyncPendingBadge {
    color: #a9c9c6;
    background: #173f43;
    border: 1px solid #2b5c5f;
    border-radius: 7px;
    padding: 1px 5px;
    font-size: 9px;
    font-weight: 600;
}
QLabel#SyncPendingBadge[hasPending="true"] {
    color: #ffd29a;
    background: #4a3b2d;
    border-color: #76583e;
}
QPushButton#SyncActionButton {
    color: #d6e9e7;
    background: #173f43;
    border: 1px solid #2b5c5f;
    border-radius: 7px;
    padding: 6px 8px;
    min-height: 18px;
    font-size: 10px;
    font-weight: 700;
}
QPushButton#SyncActionButton:hover {
    color: white;
    background: #205156;
    border-color: #3a706f;
}
QPushButton#SyncActionButton[hasPending="true"] {
    color: #ffd29a;
    background: #4a3b2d;
    border-color: #76583e;
}
QLabel#SyncDetail {
    color: #91b9b6;
    border: none;
    background: transparent;
    font-size: 9px;
}
QLabel#SyncError {
    color: #ffc1bf;
    background: #4c3031;
    border: 1px solid #724647;
    border-radius: 5px;
    padding: 3px 5px;
    font-size: 9px;
}
QFrame#SidebarProfile {
    background: #123b40;
    border: 1px solid #28565a;
    border-radius: 12px;
}
QLabel#SidebarAvatar {
    color: #17393b;
    background: #f0ad69;
    border: 1px solid #ffc98f;
    border-radius: 18px;
    font-size: 15px;
    font-weight: 800;
}
QLabel#SidebarUser {
    color: #f3faf9;
    font-size: 13px;
    font-weight: 700;
}
QLabel#SidebarRole {
    color: #8fb2b1;
    font-size: 10px;
}
QPushButton#NavButton {
    color: #c4d9d7;
    background: transparent;
    border: 1px solid transparent;
    border-radius: 10px;
    padding: 10px 13px;
    text-align: left;
    font-size: 14px;
    font-weight: 500;
}
QPushButton#NavButton:hover {
    color: #f5fbfa;
    background: #174b4e;
    border-color: #285e60;
}
QPushButton#NavButton:checked {
    color: #ffffff;
    background: #1d8178;
    border: 1px solid #2e988b;
    border-left: 4px solid #8ce3d1;
    padding-left: 10px;
    font-weight: 700;
}
QPushButton#NavButton[hasUpdate="true"] {
    color: #ffd4a3;
    border-color: #8c6541;
    background: #3e3b31;
    font-weight: 700;
}
QPushButton#NavButton[hasMessage="true"] {
    color: #ffd4a3;
    border-color: #8c6541;
    background: #3e3b31;
    font-weight: 700;
}
QPushButton#NavGroupButton {
    color: #c4d9d7;
    background: transparent;
    border: 1px solid transparent;
    border-radius: 10px;
    padding: 10px 13px;
    text-align: left;
    font-size: 14px;
    font-weight: 600;
}
QPushButton#NavGroupButton:hover,
QPushButton#NavGroupButton[active="true"] {
    color: #ffffff;
    background: #174b4e;
    border-color: #285e60;
}
QWidget#DataNavContainer {
    background: transparent;
}
QPushButton#NavSubButton {
    color: #a9c4c2;
    background: transparent;
    border: none;
    border-left: 2px solid #28565a;
    border-radius: 0 8px 8px 0;
    margin-left: 19px;
    padding: 8px 12px 8px 17px;
    text-align: left;
    font-size: 13px;
    font-weight: 500;
}
QPushButton#NavSubButton:hover {
    color: #f5fbfa;
    background: #143f43;
    border-left-color: #4d8f89;
}
QPushButton#NavSubButton:checked {
    color: #ffffff;
    background: #17665f;
    border-left: 3px solid #8ce3d1;
    padding-left: 16px;
    font-weight: 700;
}
QFrame#TopBar, QFrame#Card, QGroupBox {
    background: white;
    border: 1px solid #e4eaf2;
    border-radius: 10px;
}
QFrame#DashboardMetricCard,
QFrame#DashboardPanel {
    background: #ffffff;
    border: 1px solid #dce8e6;
    border-radius: 10px;
}
QFrame#DashboardMetricCard[tone="teal"] { border-top: 3px solid #1d8178; }
QFrame#DashboardMetricCard[tone="green"] { border-top: 3px solid #43ad75; }
QFrame#DashboardMetricCard[tone="orange"] { border-top: 3px solid #f0ad69; }
QFrame#DashboardMetricCard[tone="blue"] { border-top: 3px solid #4a8bc4; }
QFrame#DashboardMetricCard[tone="purple"] { border-top: 3px solid #8064b6; }
QLabel#DashboardMetricTitle {
    color: #607a78;
    font-size: 12px;
    font-weight: 600;
    border: none;
    background: transparent;
}
QLabel#DashboardMetricValue {
    font-size: 23px;
    font-weight: 800;
    border: none;
    background: transparent;
}
QLabel#DashboardMetricDetail {
    color: #718785;
    font-size: 10px;
    border: none;
    background: transparent;
}
QLabel#DashboardPanelTitle {
    color: #173a3d;
    font-size: 14px;
    font-weight: 700;
    border: none;
    background: transparent;
}
QPushButton#DashboardPanelAction {
    min-height: 24px;
    padding: 3px 10px;
    color: #1d8178;
    background: #edf8f5;
    border: 1px solid #c9e4de;
    border-radius: 6px;
    font-size: 11px;
    font-weight: 700;
}
QPushButton#DashboardPanelAction:hover {
    color: #17665f;
    background: #e3f4ef;
    border-color: #9ed2c7;
}
QPushButton#DashboardPanelAction:checked {
    color: #ffffff;
    background: #1d8178;
    border-color: #1d8178;
}
QPushButton#TimingBasisToggleButton {
    min-height: 26px;
    min-width: 104px;
    padding: 4px 13px;
    color: #17665f;
    background: #edf8f5;
    border: 1px solid #b9ddd6;
    border-radius: 13px;
    font-size: 12px;
    font-weight: 700;
}
QPushButton#TimingBasisToggleButton:hover {
    background: #e0f2ed;
    border-color: #7fbaaf;
}
QPushButton#TimingBasisToggleButton:checked {
    color: #ffffff;
    background: #d68b3f;
    border-color: #c4772f;
}
QTableWidget#DashboardTable {
    background: #ffffff;
    alternate-background-color: #f7faf9;
    border: 1px solid #e1ebe9;
    border-radius: 7px;
    color: #365654;
    selection-background-color: transparent;
}
QTableWidget#DashboardTable::item {
    border-bottom: 1px solid #ebf2f0;
    padding: 4px 7px;
}
QTableWidget#DashboardTable QHeaderView::section {
    color: #536e6c;
    background: #eef5f3;
    border: none;
    border-bottom: 1px solid #d8e7e3;
    padding: 6px 8px;
    font-weight: 700;
}
QFrame#TopBar {
    border-radius: 0;
    border-left: none;
    border-right: none;
    border-top: none;
}
QPushButton#AnnouncementHornButton {
    color: #315d5c;
    background: #edf6f4;
    border: 1px solid #d3e7e3;
    border-radius: 10px;
    padding: 0;
}
QPushButton#AnnouncementHornButton:hover {
    background: #dff0ec;
    border-color: #9dcfc6;
}
QPushButton#AnnouncementHornButton[hasUnread="true"] {
    background: #fff4ec;
    border-color: #f0b888;
}
QPushButton#AnnouncementTickerButton {
    color: #365654;
    background: transparent;
    border: none;
    border-radius: 8px;
    padding: 7px 10px;
    text-align: left;
    font-size: 13px;
    font-weight: 600;
}
QPushButton#AnnouncementTickerButton:hover {
    color: #173a3d;
    background: #edf4f3;
}
QPushButton#AnnouncementHornButton:disabled,
QPushButton#AnnouncementTickerButton:disabled {
    color: #8a9998;
    background: transparent;
    border-color: transparent;
}
QTextBrowser#AnnouncementBody {
    color: #334155;
    background: white;
    border: 1px solid #dfe6f1;
    border-radius: 8px;
    padding: 12px;
}
QLabel#AnnouncementListCount {
    color: #176f68;
    background: #e7f6f2;
    border: 1px solid #bfe1d9;
    border-radius: 12px;
    padding: 5px 12px;
    font-weight: 700;
}
QListWidget#AnnouncementArchiveList {
    color: #365654;
    background: #f7faf9;
    border: 1px solid #d8e7e3;
    border-radius: 11px;
    padding: 7px;
    outline: none;
}
QListWidget#AnnouncementArchiveList::item {
    background: #ffffff;
    border: 1px solid #e1ebe9;
    border-radius: 9px;
    margin: 3px;
    padding: 9px 11px;
}
QListWidget#AnnouncementArchiveList::item:hover {
    background: #edf7f4;
    border-color: #b7dcd5;
}
QListWidget#AnnouncementArchiveList::item:selected {
    color: #145f58;
    background: #def2ed;
    border-color: #78bdb1;
}
QFrame#AnnouncementListPreview {
    background: #ffffff;
    border: 1px solid #d8e7e3;
    border-radius: 11px;
}
QLabel#AnnouncementPreviewState {
    color: #647c7b;
    background: #edf4f3;
    border: 1px solid #d5e6e2;
    border-radius: 10px;
    padding: 3px 9px;
    font-size: 11px;
    font-weight: 700;
}
QLabel#AnnouncementPreviewState[unread="true"] {
    color: #a85a1d;
    background: #fff3e8;
    border-color: #efc49f;
}
QLabel#AnnouncementPreviewTitle {
    color: #173a3d;
    background: transparent;
    border: none;
    font-size: 19px;
    font-weight: 700;
}
QLabel#AnnouncementPreviewTicker {
    color: #315d5c;
    background: #edf6f4;
    border: 1px solid #d3e7e3;
    border-radius: 8px;
    padding: 9px 11px;
    font-weight: 600;
}
QWidget#MessageBoxPanel {
    background: #f7f9fc;
    border: 1px solid #dce4ef;
    border-radius: 10px;
}
QLabel#MessageBoxIcon {
    background: transparent;
    border: none;
}
QLabel#MessageBoxTitle {
    color: #17233c;
    background: transparent;
    border: none;
    font-size: 15px;
    font-weight: 700;
}
QLabel#MessageBoxText {
    color: #526177;
    background: transparent;
    border: none;
    font-size: 13px;
}
QFrame#Card {
    padding: 8px;
}
QFrame#DashboardFilterGroup {
    background: #f7f9fd;
    border: 1px solid #dfe6f1;
    border-radius: 8px;
}
QFrame#DashboardFilterGroup:hover {
    background: #f3f7fd;
    border-color: #c8d6e8;
}
QLabel#DashboardFilterLabel {
    color: #526177;
    background: transparent;
    border: none;
    font-weight: 700;
}
QFrame#SettingCard {
    background: #f7f9fd;
    border: 1px solid #dfe6f1;
    border-radius: 10px;
}
QLabel#SettingCardTitle {
    color: #17233c;
    font-size: 16px;
    font-weight: 700;
    background: transparent;
    border: none;
}
QLabel#SettingCardDescription {
    color: #708096;
    font-size: 12px;
    background: transparent;
    border: none;
}
QLabel#SettingFieldLabel {
    color: #526177;
    font-weight: 600;
    background: transparent;
    border: none;
}
QLabel#ProfileValue,
QLabel#UpdateVersion,
QLabel#UpdateLatestVersion {
    color: #173a3d;
    font-size: 15px;
    font-weight: 700;
    background: transparent;
    border: none;
}
QLabel#UpdateVersion {
    color: #245fc7;
    font-size: 18px;
}
QLabel#MandatoryUpdateBadge {
    color: #b52f3a;
    background: #fff0f1;
    border: 1px solid #f2c4c8;
    border-radius: 8px;
    padding: 3px 8px;
    font-weight: 700;
}
QLabel#UpdateState {
    color: #526177;
    background: transparent;
    border: none;
}
QTextBrowser#UpdateNotes {
    color: #334155;
    background: white;
    border: 1px solid #dfe6f1;
    border-radius: 8px;
    padding: 8px;
}
QLabel#LoadingMessage {
    color: #173a3d;
    font-size: 16px;
    font-weight: 700;
    background: transparent;
    border: none;
}
QLabel#ModeHint {
    color: #1c5ed6;
    background: #edf4ff;
    border: 1px solid #d5e4ff;
    border-radius: 7px;
    padding: 9px 10px;
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
QLineEdit, QComboBox, QSpinBox, QDateEdit, QTextEdit, QTableWidget, QTableView {
    background: white;
    border: 1px solid #d9e1ec;
    border-radius: 6px;
    padding: 6px;
    selection-background-color: #3478f6;
}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDateEdit:focus, QTextEdit:focus {
    border: 1px solid #3478f6;
}
QComboBox {
    min-height: 20px;
    padding: 7px 38px 7px 11px;
}
QComboBox:hover {
    border-color: #aebdd1;
    background: #fbfcfe;
}
QComboBox:on {
    border-color: #3478f6;
    background: white;
}
QComboBox::drop-down:on {
    background: #edf3fb;
    border-left-color: #d5e0ee;
}
QComboBox:disabled {
    color: #99a5b7;
    background: #f2f5f9;
    border-color: #e1e7ef;
}
QComboBox::drop-down {
    subcontrol-origin: padding;
    subcontrol-position: top right;
    width: 32px;
    background: #f7f9fd;
    border: none;
    border-left: 1px solid #e2e8f1;
    border-top-right-radius: 6px;
    border-bottom-right-radius: 6px;
}
QComboBox::drop-down:hover {
    background: #edf3fb;
}
QComboBox::down-arrow {
    image: url(__COMBO_ARROW__);
    width: 12px;
    height: 8px;
}
QComboBox QAbstractItemView {
    color: #243047;
    background: white;
    border: 1px solid #d7e0eb;
    border-radius: 8px;
    padding: 5px;
    outline: none;
    selection-color: #174a9c;
    selection-background-color: #e8f1ff;
}
QCheckBox {
    spacing: 9px;
    min-height: 25px;
}
QCheckBox:hover {
    color: #1c5ed6;
}
QCheckBox:disabled {
    color: #99a5b7;
}
QCheckBox::indicator {
    width: 18px;
    height: 18px;
    background: white;
    border: 1px solid #b9c5d5;
    border-radius: 5px;
}
QCheckBox::indicator:hover {
    border-color: #5c8fe9;
    background: #f3f7ff;
}
QCheckBox::indicator:checked {
    image: url(__CHECK_MARK__);
    background: #3478f6;
    border-color: #3478f6;
}
QCheckBox::indicator:checked:hover {
    background: #2868db;
    border-color: #2868db;
}
QCheckBox::indicator:indeterminate {
    image: url(__CHECK_MINUS__);
    background: #3478f6;
    border-color: #3478f6;
}
QCheckBox::indicator:disabled {
    background: #eef2f6;
    border-color: #d4dce7;
}
QCheckBox::indicator:checked:disabled,
QCheckBox::indicator:indeterminate:disabled {
    background: #aeb9c8;
    border-color: #aeb9c8;
}
QSpinBox {
    min-height: 20px;
    padding: 7px 34px 7px 10px;
}
QSpinBox:hover {
    border-color: #aebdd1;
    background: #fbfcfe;
}
QSpinBox:disabled {
    color: #99a5b7;
    background: #f2f5f9;
    border-color: #e1e7ef;
}
QSpinBox::up-button,
QSpinBox::down-button {
    subcontrol-origin: border;
    width: 27px;
    background: #f7f9fd;
    border: none;
    border-left: 1px solid #e2e8f1;
}
QSpinBox::up-button {
    subcontrol-position: top right;
    border-top-right-radius: 6px;
    border-bottom: 1px solid #e2e8f1;
}
QSpinBox::down-button {
    subcontrol-position: bottom right;
    border-bottom-right-radius: 6px;
}
QSpinBox::up-button:hover,
QSpinBox::down-button:hover {
    background: #eaf1fb;
}
QSpinBox::up-arrow {
    image: url(__SPIN_UP__);
    width: 9px;
    height: 6px;
}
QSpinBox::down-arrow {
    image: url(__SPIN_DOWN__);
    width: 9px;
    height: 6px;
}
QDateEdit {
    min-height: 20px;
    padding: 7px 34px 7px 10px;
}
QDateEdit:hover {
    border-color: #aebdd1;
    background: #fbfcfe;
}
QDateEdit:disabled {
    color: #99a5b7;
    background: #f2f5f9;
    border-color: #e1e7ef;
}
QDateEdit::drop-down {
    subcontrol-origin: border;
    subcontrol-position: top right;
    width: 28px;
    background: #f7f9fd;
    border: none;
    border-left: 1px solid #e2e8f1;
    border-top-right-radius: 6px;
    border-bottom-right-radius: 6px;
}
QDateEdit::drop-down:hover {
    background: #eaf1fb;
}
QDateEdit::down-arrow {
    image: url(__COMBO_ARROW__);
    width: 12px;
    height: 8px;
}
QCalendarWidget {
    background: white;
    border: 1px solid #cfd9e7;
    border-radius: 10px;
}
QCalendarWidget QWidget#qt_calendar_navigationbar {
    background: #3478f6;
    border: none;
    border-top-left-radius: 9px;
    border-top-right-radius: 9px;
    min-height: 40px;
}
QCalendarWidget QToolButton {
    color: white;
    background: transparent;
    border: none;
    border-radius: 6px;
    padding: 5px 9px;
    font-size: 13px;
    font-weight: 700;
}
QCalendarWidget QToolButton:hover,
QCalendarWidget QToolButton:pressed {
    background: #2868db;
}
QCalendarWidget QToolButton::menu-indicator {
    image: none;
    width: 0;
    height: 0;
}
QCalendarWidget QToolButton#qt_calendar_prevmonth,
QCalendarWidget QToolButton#qt_calendar_nextmonth {
    min-width: 28px;
    padding: 2px 5px 5px 5px;
    font-family: "Segoe UI Symbol", "Microsoft YaHei UI";
    font-size: 25px;
    font-weight: 400;
}
QCalendarWidget QSpinBox#qt_calendar_yearedit {
    color: white;
    background: #2868db;
    border: 1px solid #5d91ed;
    border-radius: 5px;
    padding: 3px 24px 3px 7px;
    min-height: 20px;
}
QCalendarWidget QHeaderView::section {
    color: #64748b;
    background: #f2f6fb;
    border: none;
    border-bottom: 1px solid #e5ebf3;
    padding: 6px 2px;
    font-weight: 600;
}
QCalendarWidget QAbstractItemView {
    color: #243047;
    background: white;
    border: none;
    outline: none;
    selection-color: white;
    selection-background-color: #3478f6;
}
QCalendarWidget QAbstractItemView::item {
    border: none;
    border-radius: 6px;
    padding: 4px;
}
QCalendarWidget QAbstractItemView::item:hover {
    color: #174a9c;
    background: #e8f1ff;
    border: 1px solid #b8d0fb;
}
QCalendarWidget QAbstractItemView::item:selected {
    color: white;
    background: #3478f6;
}
QCalendarWidget QMenu {
    color: #243047;
    background: white;
    border: 1px solid #d7e0eb;
    border-radius: 7px;
    padding: 5px;
}
QCalendarWidget QMenu::item {
    border-radius: 5px;
    padding: 6px 18px;
}
QCalendarWidget QMenu::item:selected {
    color: #174a9c;
    background: #e8f1ff;
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
QWidget#WindowControls {
    background: transparent;
    border: none;
}
QPushButton#WindowMinimizeButton,
QPushButton#WindowMaximizeButton,
QPushButton#WindowCloseButton {
    color: #526177;
    background: transparent;
    border: none;
    border-radius: 7px;
    padding: 0;
    font-family: "Segoe UI Symbol", "Microsoft YaHei UI";
    font-size: 18px;
    font-weight: 400;
}
QPushButton#WindowMinimizeButton:hover,
QPushButton#WindowMaximizeButton:hover {
    color: #17233c;
    background: #eaf0f7;
}
QPushButton#WindowCloseButton:hover {
    color: white;
    background: #e45454;
}
QPushButton#WindowMinimizeButton:pressed,
QPushButton#WindowMaximizeButton:pressed {
    background: #dce5f1;
}
QPushButton#WindowCloseButton:pressed {
    background: #c93f47;
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
QPushButton#UpdateButton {
    color: #245fc7;
    background: #edf4ff;
    border-color: #c8daf8;
    font-weight: 600;
}
QPushButton#UpdateButton:hover {
    color: #174ea6;
    background: #dfeaff;
    border-color: #adc7f1;
}
QPushButton#AnomalyButton {
    color: #9a6415;
    background: #fff7e5;
    border-color: #f4d79b;
    font-weight: 600;
}
QPushButton#AnomalyButton:hover {
    color: #81520f;
    background: #ffefc7;
    border-color: #e6bf69;
}
QPushButton#AnomalyButton:checked {
    color: white;
    background: #d18400;
    border-color: #d18400;
}
QPushButton#AnomalyButton:checked:hover {
    background: #b97200;
    border-color: #b97200;
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
QTableWidget#AnnouncementManagementTable,
QTableWidget#AnnouncementMessageTable {
    background: #ffffff;
    alternate-background-color: #f6faf9;
    border: 1px solid #d8e7e3;
    border-radius: 9px;
    selection-background-color: #dff2ed;
    selection-color: #173a3d;
}
QTableWidget#AnnouncementManagementTable::item,
QTableWidget#AnnouncementMessageTable::item {
    border-bottom: 1px solid #e8f1ef;
    padding: 7px 9px;
}
QTableWidget#AnnouncementManagementTable::item:selected,
QTableWidget#AnnouncementMessageTable::item:selected {
    border-top: 1px solid #b8d8d1;
    border-bottom: 1px solid #b8d8d1;
}
QListWidget#AnnouncementChoiceList,
QListWidget#AnnouncementAttachmentList {
    background: #fbfdfc;
    alternate-background-color: #f2f8f6;
    border: 1px solid #cfe1dd;
    border-radius: 9px;
    padding: 5px;
    outline: none;
}
QListWidget#AnnouncementChoiceList::item,
QListWidget#AnnouncementAttachmentList::item {
    color: #294f50;
    border: 1px solid transparent;
    border-radius: 6px;
    padding: 5px 9px;
}
QListWidget#AnnouncementChoiceList::item:hover,
QListWidget#AnnouncementAttachmentList::item:hover {
    background: #e7f6f2;
    border-color: #c1dfd8;
}
QListWidget#AnnouncementAttachmentList::item:selected {
    color: #173a3d;
    background: #d9f0ea;
    border-color: #9fd1c6;
}
QToolButton#RichTextToolButton {
    color: #244b4d;
    background: #f5faf8;
    border: 1px solid #d2e4df;
    border-radius: 6px;
    padding: 3px;
}
QToolButton#RichTextToolButton:hover {
    background: #e7f6f2;
    border-color: #9fd1c6;
}
QToolButton#RichTextToolButton:pressed,
QToolButton#RichTextToolButton:checked {
    color: #0f655d;
    background: #d6eee8;
    border-color: #69aaa1;
}
QLabel#UpdateSpeed {
    color: #526e6d;
    font-size: 12px;
    font-weight: 600;
}
QLabel#OfflineBusinessBadge {
    color: #8a4b10;
    background: #fff3dc;
    border: 1px solid #efcf91;
    border-radius: 8px;
    padding: 8px 14px;
    font-weight: 700;
}
QTabWidget#DashboardTabs::pane,
QTabWidget#WorkflowTabs::pane,
QTabWidget#AnnouncementTabs::pane {
    background: white;
    border: 1px solid #dfe6ef;
    border-radius: 9px;
    border-top-left-radius: 0;
    top: -1px;
}
QTabWidget#DashboardTabs QTabBar::tab,
QTabWidget#WorkflowTabs QTabBar::tab,
QTabWidget#AnnouncementTabs QTabBar::tab {
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
QTabWidget#DashboardTabs QTabBar::tab:hover,
QTabWidget#WorkflowTabs QTabBar::tab:hover,
QTabWidget#AnnouncementTabs QTabBar::tab:hover {
    color: #245fc7;
    background: #f1f5fb;
}
QTabWidget#DashboardTabs QTabBar::tab:selected,
QTabWidget#WorkflowTabs QTabBar::tab:selected,
QTabWidget#AnnouncementTabs QTabBar::tab:selected {
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
QProgressBar#BrowserCheckProgress {
    min-height: 7px;
    max-height: 7px;
    border: none;
    border-radius: 3px;
    background: #e3ebf7;
}
QProgressBar#BrowserCheckProgress::chunk {
    border-radius: 3px;
    background: #3478f6;
}
QScrollArea#PageScrollArea {
    background: transparent;
    border: none;
}
QScrollArea#PageScrollArea > QWidget > QWidget {
    background: transparent;
}
QScrollBar:vertical {
    background: transparent;
    width: 12px;
    margin: 3px 2px;
}
QScrollBar::handle:vertical {
    background: #b9c6d7;
    border-radius: 4px;
    min-height: 32px;
}
QScrollBar::handle:vertical:hover {
    background: #91a5bf;
}
QScrollBar::handle:vertical:pressed {
    background: #718aa9;
}
QScrollBar:horizontal {
    background: transparent;
    height: 12px;
    margin: 2px 3px;
}
QScrollBar::handle:horizontal {
    background: #b9c6d7;
    border-radius: 4px;
    min-width: 32px;
}
QScrollBar::handle:horizontal:hover {
    background: #91a5bf;
}
QScrollBar::handle:horizontal:pressed {
    background: #718aa9;
}
QScrollBar::add-line,
QScrollBar::sub-line {
    width: 0;
    height: 0;
    background: transparent;
    border: none;
}
QScrollBar::add-page,
QScrollBar::sub-page {
    background: transparent;
}
QAbstractScrollArea::corner {
    background: #f4f7fb;
    border: none;
}
QWidget:disabled {
    color: #a3adba;
}
QLabel:disabled,
QGroupBox:disabled,
QCheckBox:disabled {
    color: #a3adba;
}
QLabel#PageTitle:disabled,
QLabel#Muted:disabled,
QLabel#MetricValue:disabled,
QLabel#SettingCardTitle:disabled,
QLabel#SettingCardDescription:disabled,
QLabel#SettingFieldLabel:disabled {
    color: #a3adba;
}
QLabel#ModeHint:disabled {
    color: #a3adba;
    background: #f3f5f8;
    border-color: #e7ebf1;
}
QPushButton:disabled,
QPushButton#PrimaryButton:disabled,
QPushButton#UpdateButton:disabled,
QPushButton#AnomalyButton:disabled,
QPushButton#DangerButton:disabled,
QPushButton#ViolationModeButton:checked:disabled {
    color: #aab3bf;
    background: #f3f5f8;
    border-color: #e5eaf0;
}
QPushButton#NavButton:disabled {
    color: #698b89;
    background: transparent;
    border: none;
}
QLineEdit:disabled,
QComboBox:disabled,
QSpinBox:disabled,
QDateEdit:disabled,
QTextEdit:disabled,
QTableWidget:disabled,
QTableView:disabled {
    color: #a3adba;
    background: #f5f7fa;
    border-color: #e6ebf1;
    selection-background-color: #cbd3de;
}
QComboBox::drop-down:disabled,
QDateEdit::drop-down:disabled,
QSpinBox::up-button:disabled,
QSpinBox::down-button:disabled {
    background: #eef2f6;
    border-color: #e3e8ef;
}
QFrame#Card:disabled,
QFrame#SettingCard:disabled,
QGroupBox:disabled {
    background: #fafbfc;
    border-color: #edf0f4;
}
QTabBar::tab:disabled {
    color: #aab3bf;
    background: #f1f4f7;
    border-color: #e5e9ef;
}
QProgressBar:disabled {
    color: #a3adba;
    background: #f5f7fa;
    border-color: #e6ebf1;
}
QProgressBar::chunk:disabled {
    background: #c7d0dc;
}
"""

# Keep the widget rules together while applying the product palette from one
# place.  This also covers Qt's nested controls (calendar menus, spin buttons,
# table selections and scrollbars) so dialogs and content pages stay visually
# consistent with the sidebar.
_CONTENT_PALETTE = {
    "#243047": "#173a3d",
    "#f4f7fb": "#f3f7f6",
    "#e4eaf2": "#d8e7e3",
    "#f7f9fc": "#f5f8f7",
    "#dce4ef": "#d8e7e3",
    "#17233c": "#173a3d",
    "#526177": "#526e6d",
    "#f7f9fd": "#f5f8f7",
    "#dfe6f1": "#d8e7e3",
    "#f3f7fd": "#edf7f5",
    "#c8d6e8": "#b8d8d1",
    "#708096": "#647c7b",
    "#1c5ed6": "#176f68",
    "#edf4ff": "#e7f6f2",
    "#d5e4ff": "#bfe1d9",
    "#d9e1ec": "#d3e3df",
    "#3478f6": "#1d8178",
    "#aebdd1": "#9fbdb8",
    "#fbfcfe": "#fbfdfc",
    "#edf3fb": "#e9f4f1",
    "#d5e0ee": "#cfdfdb",
    "#99a5b7": "#91a6a3",
    "#f2f5f9": "#f1f5f4",
    "#e1e7ef": "#dde7e4",
    "#e2e8f1": "#dbe7e4",
    "#d7e0eb": "#d3e2df",
    "#174a9c": "#145f58",
    "#e8f1ff": "#e1f2ee",
    "#b9c5d5": "#adc4c0",
    "#5c8fe9": "#4a9c91",
    "#f3f7ff": "#edf8f5",
    "#2868db": "#166d66",
    "#eef2f6": "#edf2f1",
    "#d4dce7": "#d3dedb",
    "#aeb9c8": "#a6b7b3",
    "#eaf1fb": "#e5f1ee",
    "#cfd9e7": "#cadeda",
    "#5d91ed": "#69aaa1",
    "#64748b": "#617978",
    "#f2f6fb": "#f1f6f4",
    "#e5ebf3": "#e1ebe8",
    "#b8d0fb": "#9fd7cc",
    "#eef3fa": "#e9f2f0",
    "#e2eaf5": "#dce9e6",
    "#eaf0f7": "#e5efed",
    "#dce5f1": "#d7e5e2",
    "#f1f5fa": "#eef5f3",
    "#edf1f6": "#e8f1ef",
    "#f8fafd": "#f7faf9",
    "#dfe6ef": "#dae7e4",
    "#d9e2ee": "#d4e3df",
    "#245fc7": "#176f68",
    "#f1f5fb": "#edf6f4",
    "#38b779": "#1f9b77",
    "#e3ebf7": "#dcebe8",
    "#b9c6d7": "#aec7c2",
    "#91a5bf": "#82aaa3",
    "#718aa9": "#618f88",
}

for _old_color, _new_color in _CONTENT_PALETTE.items():
    APP_STYLESHEET = APP_STYLESHEET.replace(_old_color, _new_color)

APP_STYLESHEET = (
    APP_STYLESHEET.replace("__COMBO_ARROW__", _control_asset_path("chevron-down.svg"))
    .replace("__CHECK_MARK__", _control_asset_path("check.svg"))
    .replace("__CHECK_MINUS__", _control_asset_path("minus.svg"))
    .replace("__SPIN_UP__", _control_asset_path("chevron-up.svg"))
    .replace("__SPIN_DOWN__", _control_asset_path("chevron-down.svg"))
)

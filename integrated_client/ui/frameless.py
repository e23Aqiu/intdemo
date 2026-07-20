"""Borderless window helpers that do not add a visible title bar.

The application already has its own top-level visual structure.  These helpers
only provide native window hit testing and a small set of controls that can be
embedded into that structure.
"""

import ctypes
import math
import sys
from ctypes import wintypes

from PyQt5.QtCore import QEvent, QPoint, QRect, Qt
from PyQt5.QtGui import QCursor
from PyQt5.QtWidgets import (
    QAbstractButton,
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox as QtMessageBox,
    QPushButton,
    QScrollBar,
    QSlider,
    QStyle,
    QTabBar,
    QVBoxLayout,
    QWidget,
)


WM_GETMINMAXINFO = 0x0024
WM_NCCALCSIZE = 0x0083
WM_NCHITTEST = 0x0084
WM_NCACTIVATE = 0x0086
WVR_REDRAW = 0x0300
MONITOR_DEFAULTTONEAREST = 0x00000002
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWA_BORDER_COLOR = 34
DWMWCP_ROUND = 2
DWMWA_COLOR_NONE = 0xFFFFFFFE
GWL_STYLE = -16
WS_CAPTION = 0x00C00000
WS_SYSMENU = 0x00080000
WS_THICKFRAME = 0x00040000
WS_MINIMIZEBOX = 0x00020000
WS_MAXIMIZEBOX = 0x00010000
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020
HTCLIENT = 1
HTCAPTION = 2
HTLEFT = 10
HTRIGHT = 11
HTTOP = 12
HTTOPLEFT = 13
HTTOPRIGHT = 14
HTBOTTOM = 15
HTBOTTOMLEFT = 16
HTBOTTOMRIGHT = 17


class MINMAXINFO(ctypes.Structure):
    _fields_ = (
        ("ptReserved", wintypes.POINT),
        ("ptMaxSize", wintypes.POINT),
        ("ptMaxPosition", wintypes.POINT),
        ("ptMinTrackSize", wintypes.POINT),
        ("ptMaxTrackSize", wintypes.POINT),
    )


class MONITORINFO(ctypes.Structure):
    _fields_ = (
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    )


class FramelessWindowMixin:
    """Add drag and resize behavior to a Qt frameless top-level window."""

    resize_border_width = 7

    def _initialize_frameless_window(
        self,
        resizable=True,
        enforce_minimum_size=False,
    ):
        self._frameless_resizable = bool(resizable)
        self._frameless_enforce_minimum_size = bool(enforce_minimum_size)
        self._frameless_drag_regions = []
        self._frameless_drag_height = 0
        self.setWindowFlag(Qt.FramelessWindowHint, True)

    def register_window_drag_region(self, widget):
        """Use an existing widget as a caption without changing its layout."""
        if widget not in self._frameless_drag_regions:
            self._frameless_drag_regions.append(widget)
        widget.installEventFilter(self)
        for child in widget.findChildren(QWidget):
            child.installEventFilter(self)

    @staticmethod
    def _is_interactive_caption_child(widget):
        while widget is not None:
            if widget.property("framelessNoDrag"):
                return True
            if isinstance(
                widget,
                (
                    QAbstractButton,
                    QAbstractSpinBox,
                    QComboBox,
                    QLineEdit,
                    QScrollBar,
                    QSlider,
                    QTabBar,
                ),
            ):
                return True
            widget = widget.parentWidget()
        return False

    def _point_in_drag_region(self, local_point):
        child = self.childAt(local_point)
        if self._is_interactive_caption_child(child):
            return False
        if 0 <= local_point.y() < self._frameless_drag_height:
            return True
        for widget in tuple(self._frameless_drag_regions):
            if widget is None or not widget.isVisible():
                continue
            top_left = widget.mapTo(self, QPoint(0, 0))
            if QRect(top_left, widget.size()).contains(local_point):
                return True
        return False

    def _window_hit_test(self, local_point):
        """Return a Windows HT* value for a point in window coordinates."""
        if self._frameless_resizable and not self.isMaximized():
            margin = self.resize_border_width
            left = local_point.x() < margin
            right = local_point.x() >= self.width() - margin
            top = local_point.y() < margin
            bottom = local_point.y() >= self.height() - margin
            if top and left:
                return HTTOPLEFT
            if top and right:
                return HTTOPRIGHT
            if bottom and left:
                return HTBOTTOMLEFT
            if bottom and right:
                return HTBOTTOMRIGHT
            if left:
                return HTLEFT
            if right:
                return HTRIGHT
            if top:
                return HTTOP
            if bottom:
                return HTBOTTOM
        if self._point_in_drag_region(local_point):
            return HTCAPTION
        return HTCLIENT

    @staticmethod
    def _uses_windows_qpa():
        application = QApplication.instance()
        return (
            sys.platform == "win32"
            and application is not None
            and application.platformName().lower() == "windows"
        )

    @staticmethod
    def _windows_nccalcsize_result(wparam):
        return WVR_REDRAW if wparam else 0

    def nativeEvent(self, event_type, message):
        if self._uses_windows_qpa():
            try:
                native_message = wintypes.MSG.from_address(int(message))
                if native_message.message == WM_NCCALCSIZE:
                    # WS_THICKFRAME is kept for native edge resizing, but its
                    # visible non-client inset must not consume application UI.
                    # When the client rectangle grows, keeping the old client
                    # image leaves the newly exposed area to be painted later.
                    # Requesting a full redraw prevents that area from briefly
                    # showing the DWM background during live edge resizing.
                    return True, self._windows_nccalcsize_result(
                        native_message.wParam
                    )
                if native_message.message == WM_NCACTIVATE:
                    # Do not let Windows repaint the retained resize frame when
                    # a native dialog takes focus.  Its default inactive frame
                    # otherwise appears as a thick white border around the app.
                    return True, 1
                if native_message.message == WM_GETMINMAXINFO:
                    self._update_maximized_work_area(native_message)
                    return True, 0
                if native_message.message == WM_NCHITTEST:
                    local_point = self.mapFromGlobal(QCursor.pos())
                    return True, self._window_hit_test(local_point)
            except (TypeError, ValueError, OSError):
                pass
        return super().nativeEvent(event_type, message)

    def _update_maximized_work_area(self, native_message):
        """Keep a borderless maximized window inside the taskbar work area."""
        minmax = MINMAXINFO.from_address(native_message.lParam)
        self._update_minimum_track_size(minmax)

        user32 = ctypes.windll.user32
        monitor_from_window = user32.MonitorFromWindow
        monitor_from_window.argtypes = (wintypes.HWND, wintypes.DWORD)
        monitor_from_window.restype = wintypes.HANDLE
        get_monitor_info = user32.GetMonitorInfoW
        get_monitor_info.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(MONITORINFO),
        )
        get_monitor_info.restype = wintypes.BOOL
        monitor = monitor_from_window(native_message.hWnd, MONITOR_DEFAULTTONEAREST)
        if not monitor:
            return
        monitor_info = MONITORINFO()
        monitor_info.cbSize = ctypes.sizeof(MONITORINFO)
        if not get_monitor_info(monitor, ctypes.byref(monitor_info)):
            return
        work = monitor_info.rcWork
        screen = monitor_info.rcMonitor
        minmax.ptMaxPosition.x = work.left - screen.left
        minmax.ptMaxPosition.y = work.top - screen.top
        minmax.ptMaxSize.x = work.right - work.left
        minmax.ptMaxSize.y = work.bottom - work.top

    def _update_minimum_track_size(self, minmax):
        """Apply the Qt minimum size to native edge-resize operations."""
        if not self._frameless_enforce_minimum_size:
            return
        scale = max(1.0, float(self.devicePixelRatioF()))
        minmax.ptMinTrackSize.x = max(
            minmax.ptMinTrackSize.x,
            math.ceil(self.minimumWidth() * scale),
        )
        minmax.ptMinTrackSize.y = max(
            minmax.ptMinTrackSize.y,
            math.ceil(self.minimumHeight() * scale),
        )

    def _apply_windows_resize_style(self):
        """Keep the native resize frame while leaving the caption removed."""
        if not self._uses_windows_qpa():
            return
        hwnd = int(self.winId())
        user32 = ctypes.windll.user32
        get_window_long = user32.GetWindowLongW
        get_window_long.argtypes = (wintypes.HWND, ctypes.c_int)
        get_window_long.restype = wintypes.LONG
        set_window_long = user32.SetWindowLongW
        set_window_long.argtypes = (wintypes.HWND, ctypes.c_int, wintypes.LONG)
        set_window_long.restype = wintypes.LONG
        set_window_pos = user32.SetWindowPos
        set_window_pos.argtypes = (
            wintypes.HWND,
            wintypes.HWND,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.UINT,
        )
        set_window_pos.restype = wintypes.BOOL
        style = get_window_long(hwnd, GWL_STYLE)
        style |= WS_SYSMENU | WS_MINIMIZEBOX | WS_MAXIMIZEBOX
        style &= ~WS_CAPTION
        if self._frameless_resizable:
            style |= WS_THICKFRAME
        else:
            style &= ~WS_THICKFRAME
        set_window_long(hwnd, GWL_STYLE, style)
        set_window_pos(
            hwnd,
            0,
            0,
            0,
            0,
            0,
            SWP_NOMOVE
            | SWP_NOSIZE
            | SWP_NOZORDER
            | SWP_NOACTIVATE
            | SWP_FRAMECHANGED,
        )
        self._apply_windows_rounded_corners(hwnd)

    @staticmethod
    def _apply_windows_rounded_corners(hwnd):
        """Keep Windows 11 rounded corners without its colored outer border."""
        try:
            set_attribute = ctypes.windll.dwmapi.DwmSetWindowAttribute
            set_attribute.argtypes = (
                wintypes.HWND,
                wintypes.DWORD,
                ctypes.c_void_p,
                wintypes.DWORD,
            )
            set_attribute.restype = ctypes.c_long
            corner_preference = ctypes.c_int(DWMWCP_ROUND)
            set_attribute(
                hwnd,
                DWMWA_WINDOW_CORNER_PREFERENCE,
                ctypes.byref(corner_preference),
                ctypes.sizeof(corner_preference),
            )
            border_color = ctypes.c_uint(DWMWA_COLOR_NONE)
            set_attribute(
                hwnd,
                DWMWA_BORDER_COLOR,
                ctypes.byref(border_color),
                ctypes.sizeof(border_color),
            )
        except (AttributeError, OSError):
            # Older Windows versions do not expose corner preferences.
            pass

    def showEvent(self, event):
        super().showEvent(event)
        self._apply_windows_resize_style()

    def eventFilter(self, watched, event):
        current = watched if isinstance(watched, QWidget) else None
        in_drag_region = False
        while current is not None:
            if current in self._frameless_drag_regions:
                in_drag_region = True
                break
            current = current.parentWidget()
        if in_drag_region:
            if not self._is_interactive_caption_child(watched):
                if (
                    event.type() == QEvent.MouseButtonDblClick
                    and event.button() == Qt.LeftButton
                ):
                    self.showNormal() if self.isMaximized() else self.showMaximized()
                    return True
                if (
                    event.type() == QEvent.MouseButtonPress
                    and event.button() == Qt.LeftButton
                    and not self._uses_windows_qpa()
                ):
                    handle = self.windowHandle()
                    if handle is not None and hasattr(handle, "startSystemMove"):
                        handle.startSystemMove()
                        return True
        return super().eventFilter(watched, event)


class WindowControls(QWidget):
    """Minimize/maximize/close buttons for embedding in existing content."""

    def __init__(
        self,
        window,
        parent=None,
        *,
        show_minimize=True,
        show_maximize=True,
    ):
        super().__init__(parent)
        self._window = window
        self.setObjectName("WindowControls")
        self.setProperty("framelessNoDrag", True)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        self.minimize_button = self._make_button(
            "WindowMinimizeButton", "−", "最小化"
        )
        self.maximize_button = self._make_button(
            "WindowMaximizeButton", "□", "最大化"
        )
        self.close_button = self._make_button("WindowCloseButton", "×", "关闭")

        self.minimize_button.setVisible(show_minimize)
        self.maximize_button.setVisible(show_maximize)
        self.minimize_button.clicked.connect(window.showMinimized)
        self.maximize_button.clicked.connect(self.toggle_maximized)
        self.close_button.clicked.connect(window.close)

        layout.addWidget(self.minimize_button)
        layout.addWidget(self.maximize_button)
        layout.addWidget(self.close_button)
        window.installEventFilter(self)
        self._sync_maximize_button()

    @staticmethod
    def _make_button(object_name, text, tooltip):
        button = QPushButton(text)
        button.setObjectName(object_name)
        button.setToolTip(tooltip)
        button.setProperty("framelessNoDrag", True)
        button.setFixedSize(38, 34)
        button.setFocusPolicy(Qt.NoFocus)
        return button

    def toggle_maximized(self):
        if self._window.isMaximized():
            self._window.showNormal()
        else:
            self._window.showMaximized()
        self._sync_maximize_button()

    def _sync_maximize_button(self):
        maximized = self._window.isMaximized()
        self.maximize_button.setText("❐" if maximized else "□")
        self.maximize_button.setToolTip("还原" if maximized else "最大化")

    def eventFilter(self, watched, event):
        if watched is self._window and event.type() == QEvent.WindowStateChange:
            self._sync_maximize_button()
        return super().eventFilter(watched, event)


class FramelessMainWindow(FramelessWindowMixin, QMainWindow):
    """A borderless main window; it deliberately adds no visual widgets."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._initialize_frameless_window(
            resizable=True,
            enforce_minimum_size=True,
        )


class FramelessDialog(FramelessWindowMixin, QDialog):
    """A borderless dialog with controls overlaid on its existing content."""

    def __init__(
        self,
        parent=None,
        *,
        resizable=False,
        show_minimize=False,
        show_maximize=False,
    ):
        super().__init__(parent)
        self._initialize_frameless_window(resizable=resizable)
        self._frameless_drag_height = 42
        self.window_controls = WindowControls(
            self,
            self,
            show_minimize=show_minimize,
            show_maximize=show_maximize,
        )
        self.window_controls.adjustSize()
        self._position_window_controls()

    def _position_window_controls(self):
        controls = self.window_controls
        controls.adjustSize()
        controls.move(max(0, self.width() - controls.width() - 8), 7)
        controls.raise_()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._position_window_controls()

    def showEvent(self, event):
        super().showEvent(event)
        self._position_window_controls()


class FramelessMessageBox(FramelessDialog):
    """Small application-native replacement for QMessageBox."""

    NoButton = QtMessageBox.NoButton
    Ok = QtMessageBox.Ok
    Save = QtMessageBox.Save
    Discard = QtMessageBox.Discard
    Cancel = QtMessageBox.Cancel
    Close = QtMessageBox.Close
    Yes = QtMessageBox.Yes
    No = QtMessageBox.No
    Retry = QtMessageBox.Retry
    Ignore = QtMessageBox.Ignore
    Information = QtMessageBox.Information
    Warning = QtMessageBox.Warning
    Critical = QtMessageBox.Critical
    Question = QtMessageBox.Question

    STANDARD_BUTTON_TEXT = {
        Ok: "确定",
        Yes: "是",
        No: "否",
        Cancel: "取消",
        Close: "关闭",
        Save: "保存",
        Discard: "不保存",
        Retry: "重试",
        Ignore: "忽略",
    }
    BUTTON_ORDER = (Ok, Save, Discard, Yes, No, Retry, Ignore, Cancel, Close)
    ICONS = {
        Information: QStyle.SP_MessageBoxInformation,
        Warning: QStyle.SP_MessageBoxWarning,
        Critical: QStyle.SP_MessageBoxCritical,
        Question: QStyle.SP_MessageBoxQuestion,
    }

    def __init__(self, parent=None):
        super().__init__(parent, resizable=False)
        self.setObjectName("FramelessMessageBox")
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setModal(True)
        self.setMinimumWidth(360)
        self._standard_buttons = self.NoButton
        self._buttons = {}

        self.background_panel = QWidget(self)
        self.background_panel.setObjectName("MessageBoxPanel")
        self.background_panel.setAttribute(Qt.WA_StyledBackground, True)
        self.background_panel.setGeometry(self.rect())
        self.background_panel.lower()

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 18, 18)
        root.setSpacing(16)

        content = QHBoxLayout()
        content.setSpacing(14)
        self.icon_label = QLabel()
        self.icon_label.setObjectName("MessageBoxIcon")
        self.icon_label.setFixedSize(36, 36)
        self.icon_label.setAlignment(Qt.AlignCenter)
        content.addWidget(self.icon_label, 0, Qt.AlignTop)

        text_layout = QVBoxLayout()
        text_layout.setSpacing(7)
        self.title_label = QLabel()
        self.title_label.setObjectName("MessageBoxTitle")
        self.title_label.setTextFormat(Qt.PlainText)
        self.title_label.setWordWrap(True)
        self.message_label = QLabel()
        self.message_label.setObjectName("MessageBoxText")
        self.message_label.setTextFormat(Qt.PlainText)
        self.message_label.setWordWrap(True)
        self.message_label.setMaximumWidth(480)
        text_layout.addWidget(self.title_label)
        text_layout.addWidget(self.message_label)
        content.addLayout(text_layout, 1)
        content.addSpacing(28)
        root.addLayout(content)

        self.button_layout = QHBoxLayout()
        self.button_layout.setSpacing(8)
        self.button_layout.addStretch()
        root.addLayout(self.button_layout)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        panel = getattr(self, "background_panel", None)
        if panel is not None:
            panel.setGeometry(self.rect())
            panel.lower()

    def setText(self, text):
        self.title_label.setText(str(text))

    def text(self):
        return self.title_label.text()

    def setInformativeText(self, text):
        self.message_label.setText(str(text))
        self.message_label.setVisible(bool(text))

    def informativeText(self):
        return self.message_label.text()

    def setIcon(self, icon):
        standard_pixmap = self.ICONS.get(icon)
        if standard_pixmap is None:
            self.icon_label.clear()
            self.icon_label.hide()
            return
        pixmap = self.style().standardIcon(standard_pixmap).pixmap(32, 32)
        self.icon_label.setPixmap(pixmap)
        self.icon_label.show()

    def setStandardButtons(self, buttons):
        for button in self._buttons.values():
            self.button_layout.removeWidget(button)
            button.deleteLater()
        self._buttons = {}
        self._standard_buttons = buttons
        for standard_button in self.BUTTON_ORDER:
            if not (buttons & standard_button):
                continue
            button = QPushButton(self.STANDARD_BUTTON_TEXT[standard_button])
            button.clicked.connect(
                lambda checked=False, result=standard_button: self.done(result)
            )
            self._buttons[standard_button] = button
            self.button_layout.addWidget(button)

    def standardButtons(self):
        return self._standard_buttons

    def button(self, standard_button):
        return self._buttons.get(standard_button)

    def setDefaultButton(self, default_button):
        if isinstance(default_button, QPushButton):
            button = default_button
        else:
            button = self.button(default_button)
        if button is not None:
            button.setDefault(True)
            button.setFocus()

    @classmethod
    def _execute(
        cls,
        parent,
        title,
        text,
        icon,
        buttons,
        default_button=NoButton,
    ):
        box = cls(parent)
        box.setWindowTitle(title)
        if title:
            box.setText(title)
            box.setInformativeText(text)
        else:
            box.setText(text)
        box.setIcon(icon)
        box.setStandardButtons(buttons)
        if default_button != cls.NoButton:
            box.setDefaultButton(default_button)
        return box.exec_()

    @classmethod
    def information(
        cls,
        parent,
        title,
        text,
        buttons=Ok,
        defaultButton=NoButton,
    ):
        return cls._execute(
            parent, title, text, cls.Information, buttons, defaultButton
        )

    @classmethod
    def warning(
        cls,
        parent,
        title,
        text,
        buttons=Ok,
        defaultButton=NoButton,
    ):
        return cls._execute(
            parent, title, text, cls.Warning, buttons, defaultButton
        )

    @classmethod
    def critical(
        cls,
        parent,
        title,
        text,
        buttons=Ok,
        defaultButton=NoButton,
    ):
        return cls._execute(
            parent, title, text, cls.Critical, buttons, defaultButton
        )

    @classmethod
    def question(
        cls,
        parent,
        title,
        text,
        buttons=Yes | No,
        defaultButton=NoButton,
    ):
        return cls._execute(
            parent, title, text, cls.Question, buttons, defaultButton
        )

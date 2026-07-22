from dataclasses import replace

from PyQt5.QtCore import QSize, Qt, pyqtSignal
from PyQt5.QtGui import QIcon
from PyQt5.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..config import APP_NAME, APP_VERSION
from ..database import Database
from ..models import Account
from ..timing import WorkflowTimingService
from ..tools.transport_tool import DDDDOCR_IMPORT_ERROR
from .account_page import AccountPage
from .auth_dialogs import PasswordDialog
from .frameless import (
    FramelessMainWindow,
    FramelessMessageBox as QMessageBox,
    WindowControls,
)
from .personal_center_page import PersonalCenterPage
from .statistics_page import StatisticsPage
from .theme import _control_asset_path, install_disabled_cursor_filter
from .workflow_page import WorkflowPage


class MainWindow(FramelessMainWindow):
    logout_requested = pyqtSignal()
    window_closed = pyqtSignal()
    PAGE_CANVAS_SIZE = QSize(1220, 700)

    def __init__(self, database: Database, account: Account, parent=None):
        super().__init__(parent)
        application = QApplication.instance()
        if application is not None:
            install_disabled_cursor_filter(application)
        self.database = database
        self.account = account
        self._prepared_to_close = False
        self._nav_buttons = {}
        self._pages = {}

        self.setWindowTitle(f"{APP_NAME} - {account.name_label}")
        self.setMinimumSize(800, 600)
        self.resize(1450, 920)

        root = QWidget()
        self.setCentralWidget(root)
        root_layout = QHBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        sidebar = self._build_sidebar()
        root_layout.addWidget(sidebar)

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)
        content_layout.addWidget(self._build_top_bar())

        self.stack = QStackedWidget()
        self.stack.setMinimumSize(self.PAGE_CANVAS_SIZE)
        self.page_scroll_area = QScrollArea()
        self.page_scroll_area.setObjectName("PageScrollArea")
        self.page_scroll_area.setFrameShape(QFrame.NoFrame)
        self.page_scroll_area.setWidgetResizable(True)
        self.page_scroll_area.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self.page_scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.page_scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.page_scroll_area.setWidget(self.stack)
        content_layout.addWidget(self.page_scroll_area, 1)
        root_layout.addWidget(content, 1)

        warning = ""
        if DDDDOCR_IMPORT_ERROR:
            warning = (
                "当前环境的 ddddocr/onnxruntime 未能加载。客户端和人工验证码模式可正常使用，"
                "自动验证码识别需要修复运行环境后再启用。"
            )
        self.statistics_page = StatisticsPage(database, account, warning)
        self.workflow_timing = WorkflowTimingService(database, account.id)
        self.workflow_page = WorkflowPage(
            self._record_workflow_summary,
            timing_service=self.workflow_timing,
        )
        self.personal_center_page = PersonalCenterPage(account)
        self.personal_center_page.change_password_requested.connect(self._change_password)
        self.personal_center_page.logout_requested.connect(self._request_logout)

        self._add_page("home", self.statistics_page)
        self._add_page("workflow", self.workflow_page)
        if account.is_admin:
            self.account_page = AccountPage(database, account)
            self.account_page.account_name_changed.connect(
                self._account_name_changed
            )
            self.account_page.account_permission_changed.connect(
                self._account_data_changed
            )
            self.account_page.account_deleted.connect(
                self._account_data_changed
            )
            self.account_page.station_data_changed.connect(
                self._account_data_changed
            )
            self._add_page("accounts", self.account_page)
        else:
            self.account_page = None
        self._add_page("personal", self.personal_center_page)

        self.show_page("home" if account.is_admin else "workflow")

    def _build_sidebar(self):
        sidebar = QFrame()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(230)
        self.sidebar = sidebar
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(16, 22, 16, 16)
        layout.setSpacing(8)

        brand_row = QHBoxLayout()
        brand_row.setContentsMargins(2, 0, 2, 0)
        brand_row.setSpacing(11)
        self.sidebar_brand_badge = QLabel("运")
        self.sidebar_brand_badge.setObjectName("BrandBadge")
        self.sidebar_brand_badge.setAlignment(Qt.AlignCenter)
        self.sidebar_brand_badge.setFixedSize(44, 44)
        brand_row.addWidget(self.sidebar_brand_badge)

        brand_text = QVBoxLayout()
        brand_text.setContentsMargins(0, 0, 0, 0)
        brand_text.setSpacing(1)
        brand = QLabel("运输业务平台")
        brand.setObjectName("BrandTitle")
        sub = QLabel(f"INTDEMO  ·  v{APP_VERSION}")
        sub.setObjectName("BrandSubTitle")
        brand_text.addWidget(brand)
        brand_text.addWidget(sub)
        brand_row.addLayout(brand_text, 1)
        layout.addLayout(brand_row)
        layout.addSpacing(25)

        nav_label = QLabel("工作导航")
        nav_label.setObjectName("SidebarSectionLabel")
        layout.addWidget(nav_label)
        layout.addSpacing(2)

        nav_items = [
            ("home", "数据仪表盘", "nav-dashboard.svg"),
            ("workflow", "一键业务处理", "nav-workflow.svg"),
        ]
        if self.account.is_admin:
            nav_items.append(("accounts", "账号管理", "nav-accounts.svg"))
        nav_items.append(("personal", "个人中心", "nav-user.svg"))

        for key, text, icon_name in nav_items:
            button = QPushButton(text)
            button.setObjectName("NavButton")
            button.setCheckable(True)
            button.setIcon(QIcon(_control_asset_path(icon_name)))
            button.setIconSize(QSize(19, 19))
            button.setMinimumHeight(46)
            button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            button.clicked.connect(lambda checked=False, page_key=key: self.show_page(page_key))
            self._nav_buttons[key] = button
            layout.addWidget(button)

        layout.addStretch()

        status = QLabel("●  本地数据已连接")
        status.setObjectName("SidebarStatus")
        layout.addWidget(status)

        profile = QFrame()
        profile.setObjectName("SidebarProfile")
        profile_layout = QHBoxLayout(profile)
        profile_layout.setContentsMargins(11, 10, 11, 10)
        profile_layout.setSpacing(10)
        self.sidebar_avatar = QLabel(self._sidebar_avatar_text(self.account.name_label))
        self.sidebar_avatar.setObjectName("SidebarAvatar")
        self.sidebar_avatar.setAlignment(Qt.AlignCenter)
        self.sidebar_avatar.setFixedSize(36, 36)
        profile_layout.addWidget(self.sidebar_avatar)

        identity_layout = QVBoxLayout()
        identity_layout.setContentsMargins(0, 0, 0, 0)
        identity_layout.setSpacing(1)
        self.sidebar_user = QLabel(self.account.name_label)
        self.sidebar_user.setObjectName("SidebarUser")
        self.sidebar_user.setToolTip(self.account.name_label)
        self.sidebar_role = QLabel(
            f"{self.account.role_label}  ·  {self.account.username}"
        )
        self.sidebar_role.setObjectName("SidebarRole")
        identity_layout.addWidget(self.sidebar_user)
        identity_layout.addWidget(self.sidebar_role)
        profile_layout.addLayout(identity_layout, 1)
        layout.addWidget(profile)
        return sidebar

    @staticmethod
    def _sidebar_avatar_text(name):
        text = str(name or "").strip()
        return text[:1].upper() if text else "用"

    def _build_top_bar(self):
        bar = QFrame()
        bar.setObjectName("TopBar")
        bar.setFixedHeight(62)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(22, 10, 22, 10)
        self.page_title = QLabel("工作台")
        self.page_title.setStyleSheet("font-size:16px;font-weight:700;color:#173a3d;")
        layout.addWidget(self.page_title)
        layout.addStretch()
        self.window_controls = WindowControls(self, bar)
        layout.addWidget(self.window_controls)
        self.register_window_drag_region(bar)
        return bar

    def _add_page(self, key, widget):
        self._pages[key] = widget
        self.stack.addWidget(widget)

    def _account_name_changed(self, account_id, display_name):
        self.statistics_page.refresh()
        if account_id != self.account.id:
            return
        self.account = replace(self.account, display_name=display_name)
        self.statistics_page.account = self.account
        self.personal_center_page.account = self.account
        self.personal_center_page.name_value.setText(self.account.name_label)
        if self.account_page is not None:
            self.account_page.current_account = self.account
        self.sidebar_user.setText(self.account.name_label)
        self.sidebar_user.setToolTip(self.account.name_label)
        self.sidebar_avatar.setText(
            self._sidebar_avatar_text(self.account.name_label)
        )
        self.setWindowTitle(f"{APP_NAME} - {self.account.name_label}")

    def _account_data_changed(self, *_args):
        self.statistics_page.refresh()

    def show_page(self, key):
        if key not in self._pages:
            return
        titles = {
            "home": "数据仪表盘",
            "workflow": "一键业务处理",
            "accounts": "账号管理",
            "personal": "个人中心",
        }
        self.stack.setCurrentWidget(self._pages[key])
        self.page_title.setText(titles.get(key, APP_NAME))
        for page_key, button in self._nav_buttons.items():
            button.setChecked(page_key == key)
        if key == "home":
            self.statistics_page.refresh()
        elif key == "accounts" and self.account_page:
            self.account_page.refresh()

    def _record_activity(self, metric_key, amount=1, source=""):
        try:
            self.database.record_activity(
                self.account.id,
                metric_key,
                amount=amount,
                source=source,
            )
            if self.stack.currentWidget() is self.statistics_page:
                self.statistics_page.refresh()
        except Exception as exc:
            QMessageBox.warning(self, "统计记录失败", f"业务结果已产生，但统计写入失败：\n{exc}")

    def _record_workflow_summary(self, counts, source="", details=None, task_id=None):
        try:
            self.database.record_activity_batch(
                self.account.id,
                counts,
                source=source,
                details=details,
                task_id=task_id,
            )
            if self.stack.currentWidget() is self.statistics_page:
                self.statistics_page.refresh()
            return True
        except Exception as exc:
            QMessageBox.warning(self, "统计记录失败", f"完整流程已完成，但统计写入失败：\n{exc}")
            return False

    def _change_password(self):
        PasswordDialog(
            self.database,
            self.account.id,
            require_current=True,
            parent=self,
        ).exec_()

    def _shutdown_tools(self):
        if not self.workflow_page.shutdown(8000):
            QMessageBox.warning(
                self,
                "任务仍在结束",
                "浏览器操作尚未完全结束，请稍后再次退出，避免损坏正在处理的数据。",
            )
            return False
        return True

    def _prepare_close(self):
        if self._prepared_to_close:
            return True
        if not self._shutdown_tools():
            return False
        self._prepared_to_close = True
        return True

    def _request_logout(self):
        reply = QMessageBox.question(
            self,
            "退出登录",
            "确定退出当前账号吗？正在执行的任务将被停止。",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes or not self._prepare_close():
            return
        self.logout_requested.emit()

    def closeEvent(self, event):
        if not self._prepare_close():
            event.ignore()
            return
        event.accept()
        self.window_closed.emit()

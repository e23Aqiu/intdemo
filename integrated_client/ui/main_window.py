from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..config import APP_NAME, APP_VERSION
from ..database import Database
from ..models import Account
from ..tools.transport_tool import DDDDOCR_IMPORT_ERROR
from .account_page import AccountPage
from .auth_dialogs import PasswordDialog
from .statistics_page import StatisticsPage
from .workflow_page import WorkflowPage


class MainWindow(QMainWindow):
    logout_requested = pyqtSignal()
    window_closed = pyqtSignal()

    def __init__(self, database: Database, account: Account, parent=None):
        super().__init__(parent)
        self.database = database
        self.account = account
        self._prepared_to_close = False
        self._nav_buttons = {}
        self._pages = {}

        self.setWindowTitle(f"{APP_NAME} - {account.name_label}")
        self.setMinimumSize(1280, 820)
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
        content_layout.addWidget(self.stack, 1)
        root_layout.addWidget(content, 1)

        warning = ""
        if DDDDOCR_IMPORT_ERROR:
            warning = (
                "当前环境的 ddddocr/onnxruntime 未能加载。客户端和人工验证码模式可正常使用，"
                "自动验证码识别需要修复运行环境后再启用。"
            )
        self.statistics_page = StatisticsPage(database, account, warning)
        self.workflow_page = WorkflowPage(self._record_workflow_summary)

        self._add_page("home", self.statistics_page)
        self._add_page("workflow", self.workflow_page)
        if account.is_admin:
            self.account_page = AccountPage(database, account)
            self._add_page("accounts", self.account_page)
        else:
            self.account_page = None

        self.show_page("home")

    def _build_sidebar(self):
        sidebar = QFrame()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(230)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(18, 24, 18, 18)
        layout.setSpacing(7)

        brand = QLabel("运输业务平台")
        brand.setObjectName("BrandTitle")
        sub = QLabel(f"一体化客户端  v{APP_VERSION}")
        sub.setObjectName("BrandSubTitle")
        layout.addWidget(brand)
        layout.addWidget(sub)
        layout.addSpacing(24)

        nav_items = [
            ("home", "▦  数据仪表盘"),
            ("workflow", "▣  一键业务处理"),
        ]
        if self.account.is_admin:
            nav_items.append(("accounts", "♙  账号管理"))

        for key, text in nav_items:
            button = QPushButton(text)
            button.setObjectName("NavButton")
            button.setCheckable(True)
            button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            button.clicked.connect(lambda checked=False, page_key=key: self.show_page(page_key))
            self._nav_buttons[key] = button
            layout.addWidget(button)

        layout.addStretch()
        identity_lines = [self.account.name_label]
        if self.account.name_label != self.account.username:
            identity_lines.append(self.account.username)
        identity_lines.append(self.account.role_label)
        user = QLabel("\n".join(identity_lines))
        user.setObjectName("SidebarUser")
        layout.addWidget(user)
        logout = QPushButton("退出登录")
        logout.setObjectName("NavButton")
        logout.clicked.connect(self._request_logout)
        layout.addWidget(logout)
        return sidebar

    def _build_top_bar(self):
        bar = QFrame()
        bar.setObjectName("TopBar")
        bar.setFixedHeight(62)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(22, 10, 22, 10)
        self.page_title = QLabel("工作台")
        self.page_title.setStyleSheet("font-size:16px;font-weight:700;color:#17233c;")
        layout.addWidget(self.page_title)
        layout.addStretch()
        identity = QLabel(
            f"{self.account.role_label} · {self.account.name_label}（{self.account.username}）"
        )
        identity.setObjectName("Muted")
        layout.addWidget(identity)
        change_password = QPushButton("修改密码")
        change_password.clicked.connect(self._change_password)
        layout.addWidget(change_password)
        return bar

    def _add_page(self, key, widget):
        self._pages[key] = widget
        self.stack.addWidget(widget)

    def show_page(self, key):
        if key not in self._pages:
            return
        titles = {
            "home": "数据仪表盘",
            "workflow": "一键业务处理",
            "accounts": "账号管理",
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
        PasswordDialog(self.database, self.account.id, parent=self).exec_()

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

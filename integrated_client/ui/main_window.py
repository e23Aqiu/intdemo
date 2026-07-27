import os
import threading
from dataclasses import replace

from PyQt5.QtCore import QProcess, QSize, Qt, QTimer, pyqtSignal
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
from .dashboard_page import DashboardPage
from .frameless import (
    FramelessMainWindow,
    WindowControls,
)
from .frameless import (
    FramelessMessageBox as QMessageBox,
)
from .online_account_page import OnlineAccountPage
from .personal_center_page import PersonalCenterPage
from .statistics_page import StatisticsPage
from .theme import _control_asset_path, install_disabled_cursor_filter
from .workflow_page import WorkflowPage


class MainWindow(FramelessMainWindow):
    logout_requested = pyqtSignal()
    window_closed = pyqtSignal()
    PAGE_CANVAS_SIZE = QSize(1220, 700)
    DATA_CENTER_VIEWS = {
        "data_station": "station_distribution",
        "data_timing": "timing",
        "data_completion": "completion",
        "data_violation": "violation",
    }

    def __init__(
        self,
        database: Database,
        account: Account,
        parent=None,
        session_manager=None,
        sync_coordinator=None,
        update_coordinator=None,
    ):
        super().__init__(parent)
        application = QApplication.instance()
        if application is not None:
            install_disabled_cursor_filter(application)
        self.database = database
        self.account = account
        self.session_manager = session_manager
        self.sync_coordinator = sync_coordinator
        self.update_coordinator = update_coordinator
        self.available_update = None
        self._prepared_to_close = False
        self._hard_exit_timer = None
        self._nav_buttons = {}
        self._pages = {}
        self._reauth_scheduled = False

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
        self.statistics_page.set_sidebar_navigation(True)
        self.dashboard_page = DashboardPage(database, account)
        self.workflow_timing = WorkflowTimingService(database, account.id)
        self.workflow_page = WorkflowPage(
            self._record_workflow_summary,
            timing_service=self.workflow_timing,
        )
        self.personal_center_page = PersonalCenterPage(account)
        self.personal_center_page.change_password_requested.connect(self._change_password)
        self.personal_center_page.logout_requested.connect(self._request_logout)

        self._add_page("home", self.dashboard_page)
        self._add_page("statistics", self.statistics_page)
        self._add_page("workflow", self.workflow_page)
        if account.is_admin:
            if self.session_manager is not None:
                self.account_page = OnlineAccountPage(
                    database,
                    account,
                    self.session_manager,
                )
            else:
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

        if self.sync_coordinator is not None:
            self.sync_coordinator.status_changed.connect(
                self._update_sync_status
            )
            self.sync_coordinator.data_changed.connect(
                self._online_data_changed
            )
            self._update_sync_status(self.sync_coordinator.engine.status())
        if self.update_coordinator is not None:
            self.update_coordinator.update_available.connect(
                self._update_available
            )
            self.update_coordinator.state_changed.connect(
                self._update_download_state
            )
            self.update_coordinator.download_completed.connect(
                self._update_downloaded
            )

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
        brand = QLabel(APP_NAME.replace("查询工具", "\n查询工具"))
        brand.setObjectName("BrandTitle")
        brand.setWordWrap(True)
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

        primary_nav_items = [
            ("home", "数据仪表盘", "nav-dashboard.svg"),
            ("workflow", "一键业务处理", "nav-workflow.svg"),
        ]
        for key, text, icon_name in primary_nav_items:
            button = self._create_nav_button(key, text)
            button.setIcon(QIcon(_control_asset_path(icon_name)))
            button.setIconSize(QSize(19, 19))
            layout.addWidget(button)

        self.data_nav_toggle = QPushButton("数据中心    ▸")
        self.data_nav_toggle.setObjectName("NavGroupButton")
        self.data_nav_toggle.setCheckable(True)
        self.data_nav_toggle.setChecked(False)
        self.data_nav_toggle.setIcon(QIcon(_control_asset_path("nav-data.svg")))
        self.data_nav_toggle.setIconSize(QSize(19, 19))
        self.data_nav_toggle.setMinimumHeight(46)
        self.data_nav_toggle.clicked.connect(self._toggle_data_navigation)
        layout.addWidget(self.data_nav_toggle)

        self.data_nav_container = QWidget()
        self.data_nav_container.setObjectName("DataNavContainer")
        data_nav_layout = QVBoxLayout(self.data_nav_container)
        data_nav_layout.setContentsMargins(0, 0, 0, 2)
        data_nav_layout.setSpacing(3)
        data_nav_items = [
            ("data_station", "全站分布"),
            ("data_timing", "用时效率"),
            ("data_completion", "完成类型"),
            ("data_violation", "违规原因"),
        ]
        for key, text in data_nav_items:
            data_nav_layout.addWidget(
                self._create_nav_button(key, text, object_name="NavSubButton")
            )
        self.data_nav_container.hide()
        layout.addWidget(self.data_nav_container)

        trailing_nav_items = []
        if self.account.is_admin:
            trailing_nav_items.append(("accounts", "账号管理", "nav-accounts.svg"))
        trailing_nav_items.append(("personal", "个人中心", "nav-user.svg"))
        for key, text, icon_name in trailing_nav_items:
            button = self._create_nav_button(key, text)
            button.setIcon(QIcon(_control_asset_path(icon_name)))
            button.setIconSize(QSize(19, 19))
            layout.addWidget(button)

        layout.addStretch()

        sync_card = QFrame()
        sync_card.setObjectName("SidebarSyncCard")
        self.sync_status_card = sync_card
        sync_layout = QVBoxLayout(sync_card)
        sync_layout.setContentsMargins(11, 9, 11, 9)
        sync_layout.setSpacing(5)

        sync_header = QHBoxLayout()
        sync_header.setContentsMargins(0, 0, 0, 0)
        sync_header.setSpacing(5)
        self.sync_state_dot = QLabel("●")
        self.sync_state_dot.setObjectName("SyncStateDot")
        self.sync_state_dot.setProperty("state", "local")
        self.sync_state_dot.setFixedWidth(12)
        sync_header.addWidget(self.sync_state_dot)
        self.sync_state_title = QLabel(
            "等待连接" if self.sync_coordinator is not None else "本地模式"
        )
        self.sync_state_title.setObjectName("SyncStateTitle")
        sync_header.addWidget(self.sync_state_title)
        sync_header.addStretch()
        self.sync_pending_badge = QLabel("待上传 0")
        self.sync_pending_badge.setObjectName("SyncPendingBadge")
        self.sync_pending_badge.setProperty("hasPending", False)
        self.sync_pending_badge.setVisible(self.sync_coordinator is not None)
        sync_header.addWidget(self.sync_pending_badge)
        sync_layout.addLayout(sync_header)

        self.sync_detail_label = QLabel(
            "数据仅保存在本机"
            if self.sync_coordinator is None
            else "正在读取同步状态"
        )
        self.sync_detail_label.setObjectName("SyncDetail")
        self.sync_detail_label.setWordWrap(True)
        sync_layout.addWidget(self.sync_detail_label)

        self.sync_error_label = QLabel("")
        self.sync_error_label.setObjectName("SyncError")
        self.sync_error_label.setWordWrap(True)
        self.sync_error_label.hide()
        sync_layout.addWidget(self.sync_error_label)
        # Compatibility alias for older UI automation.
        self.sync_status_label = self.sync_detail_label
        layout.addWidget(sync_card)

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

    def _create_nav_button(self, key, text, object_name="NavButton"):
        button = QPushButton(text)
        button.setObjectName(object_name)
        button.setCheckable(True)
        button.setMinimumHeight(46 if object_name == "NavButton" else 38)
        button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        button.clicked.connect(
            lambda checked=False, page_key=key: self.show_page(page_key)
        )
        self._nav_buttons[key] = button
        return button

    def _toggle_data_navigation(self, expanded):
        self.data_nav_container.setVisible(bool(expanded))
        self.data_nav_toggle.setText(
            "数据中心    ▾" if expanded else "数据中心    ▸"
        )

    def _set_data_navigation_active(self, active):
        self.data_nav_toggle.setProperty("active", bool(active))
        self.data_nav_toggle.style().unpolish(self.data_nav_toggle)
        self.data_nav_toggle.style().polish(self.data_nav_toggle)

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
        self.update_button = QPushButton("检查更新")
        self.update_button.setObjectName("UpdateButton")
        self.update_button.setVisible(self.update_coordinator is not None)
        self.update_button.clicked.connect(self._update_button_clicked)
        layout.addWidget(self.update_button)
        self.sync_retry_button = QPushButton("立即同步")
        self.sync_retry_button.setObjectName("PrimaryButton")
        self.sync_retry_button.setVisible(self.sync_coordinator is not None)
        self.sync_retry_button.clicked.connect(self._retry_sync)
        layout.addWidget(self.sync_retry_button)
        self.window_controls = WindowControls(self, bar)
        layout.addWidget(self.window_controls)
        self.register_window_drag_region(bar)
        return bar

    def _update_available(self, update):
        self.available_update = update
        self.update_button.setText(f"发现新版本 v{update.version}")
        self.update_button.setEnabled(True)
        self.update_button.show()

    def _update_button_clicked(self):
        if self.available_update is not None:
            self._download_available_update()
        elif self.update_coordinator is not None:
            self.update_coordinator.check(manual=True)

    def _download_available_update(self):
        update = self.available_update
        if update is None or self.update_coordinator is None:
            return
        notes = update.notes or "本次更新包含功能改进和问题修复。"
        reply = QMessageBox.question(
            self,
            "下载程序更新",
            f"发现新版本 v{update.version}。\n\n{notes}\n\n"
            "是否现在下载？下载完成后会再次询问是否安装。",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            self.update_coordinator.download(update)

    def _update_download_state(self, state, message):
        if state == "checking":
            self.update_button.setEnabled(False)
            self.update_button.setText(message)
        elif state == "up_to_date":
            self.update_button.setEnabled(True)
            self.update_button.setText(f"已是最新 v{APP_VERSION}")
            QTimer.singleShot(2500, self._reset_update_button)
        elif state == "check_error":
            self.update_button.setEnabled(True)
            self.update_button.setText("重新检查更新")
            QMessageBox.warning(self, "检查更新失败", message)
        elif state == "downloading":
            self.update_button.setEnabled(False)
            self.update_button.setText(message)
        elif state == "download_error":
            self.update_button.setEnabled(True)
            update = self.available_update
            if update is not None:
                self.update_button.setText(f"重新下载 v{update.version}")
            QMessageBox.warning(self, "更新下载失败", message)

    def _reset_update_button(self):
        if self.available_update is None and hasattr(self, "update_button"):
            self.update_button.setText("检查更新")

    def _update_downloaded(self, installer_path):
        update = self.available_update
        version = update.version if update is not None else "新版本"
        self.update_button.setEnabled(True)
        self.update_button.setText(f"安装 v{version}")
        reply = QMessageBox.question(
            self,
            "安装程序更新",
            "更新包已经完成 SHA-256 校验。\n\n"
            "安装前程序需要安全结束当前任务并退出，是否现在安装？",
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        if not self._prepare_close():
            return
        launched = QProcess.startDetached(
            str(installer_path),
            ["/SP-", "/CLOSEAPPLICATIONS"],
        )
        if isinstance(launched, tuple):
            launched = launched[0]
        if not launched:
            self._prepared_to_close = False
            QMessageBox.critical(
                self,
                "无法启动安装包",
                f"请手动运行：\n{installer_path}",
            )
            return
        self.close()

    def _retry_sync(self):
        if self.sync_coordinator is not None:
            self.sync_coordinator.retry_now()

    def _update_sync_status(self, status):
        if not hasattr(self, "sync_status_card"):
            return
        labels = {
            "online": "在线",
            "syncing": "同步中",
            "offline": "离线",
            "error": "同步错误",
            "reauth_required": "登录已失效",
        }
        state_label = labels.get(status.state, status.state)
        self.sync_state_title.setText(state_label)
        for widget in (self.sync_status_card, self.sync_state_dot):
            widget.setProperty("state", status.state)
            widget.style().unpolish(widget)
            widget.style().polish(widget)

        pending_text = f"待上传 {status.pending_count}"
        if status.quarantined_count:
            pending_text += f" / 隔离 {status.quarantined_count}"
        self.sync_pending_badge.setText(pending_text)
        self.sync_pending_badge.setProperty(
            "hasPending",
            bool(status.pending_count or status.quarantined_count),
        )
        self.sync_pending_badge.style().unpolish(self.sync_pending_badge)
        self.sync_pending_badge.style().polish(self.sync_pending_badge)

        details = []
        if (
            status.state == "offline"
            and self.session_manager is not None
            and self.session_manager.state is not None
        ):
            expires = self.session_manager.state.offline_expires_at
            details.append(
                f"离线授权至 {str(expires).replace('T', ' ')[:19]}"
            )
        if status.last_sync_at:
            details.append(
                f"上次同步 {str(status.last_sync_at).replace('T', ' ')[:19]}"
            )
        elif status.state == "syncing":
            details.append("正在与服务器核对数据")
        else:
            details.append("尚未完成首次同步")
        self.sync_detail_label.setText("\n".join(details))

        if status.error:
            concise_error = str(status.error).replace("\r", " ").replace("\n", " ")
            if len(concise_error) > 62:
                concise_error = concise_error[:59] + "…"
            self.sync_error_label.setText(concise_error)
            self.sync_error_label.setToolTip(str(status.error))
            self.sync_error_label.show()
        else:
            self.sync_error_label.clear()
            self.sync_error_label.setToolTip("")
            self.sync_error_label.hide()
        self.sync_status_card.setToolTip(status.error or "")
        if hasattr(self, "sync_retry_button"):
            self.sync_retry_button.setEnabled(status.state != "syncing")
            self.sync_retry_button.setText(
                "立即同步"
                if not status.quarantined_count
                else f"立即同步（隔离 {status.quarantined_count}）"
            )
            quarantined = self.database.get_sync_quarantined_items(limit=5)
            self.sync_retry_button.setToolTip(
                "\n".join(
                    f"{row['kind']}: {row['last_error_message']}"
                    for row in quarantined
                )
            )
        if status.state == "reauth_required" and not self._reauth_scheduled:
            self._reauth_scheduled = True
            QTimer.singleShot(0, self._require_reauthentication)

    def _require_reauthentication(self):
        QMessageBox.warning(
            self,
            "登录已失效",
            "账号、密码或设备授权已在服务器端变更。"
            "为保护数据，客户端将退出到登录页面。",
        )
        if self._prepare_close():
            self.logout_requested.emit()

    def _online_data_changed(self):
        refreshed_account = self.database.get_account(self.account.id)
        if refreshed_account is not None:
            self.account = refreshed_account
            self.dashboard_page.account = refreshed_account
            self.statistics_page.account = refreshed_account
            self.personal_center_page.account = refreshed_account
            if self.account_page is not None:
                self.account_page.current_account = refreshed_account
        self.dashboard_page.refresh()
        self.statistics_page.refresh()
        if self.account_page is not None and isinstance(
            self.account_page, OnlineAccountPage
        ) and self.stack.currentWidget() is self.account_page:
            self.account_page.refresh()

    def _add_page(self, key, widget):
        self._pages[key] = widget
        self.stack.addWidget(widget)

    def _account_name_changed(self, account_id, display_name):
        self.dashboard_page.refresh()
        self.statistics_page.refresh()
        if account_id != self.account.id:
            return
        self.account = replace(self.account, display_name=display_name)
        self.dashboard_page.account = self.account
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
        self.dashboard_page.refresh()
        self.statistics_page.refresh()

    def show_page(self, key):
        data_view = self.DATA_CENTER_VIEWS.get(key)
        page_key = "statistics" if data_view else key
        if page_key not in self._pages:
            return
        titles = {
            "home": "仪表盘",
            "data_station": "全站分布",
            "data_timing": "用时效率",
            "data_completion": "完成类型",
            "data_violation": "违规原因",
            "workflow": "一键业务处理",
            "accounts": "账号管理",
            "personal": "个人中心",
        }
        if data_view:
            self.statistics_page.set_navigation_view(data_view)
            if not self.data_nav_toggle.isChecked():
                self.data_nav_toggle.setChecked(True)
                self._toggle_data_navigation(True)
        self.stack.setCurrentWidget(self._pages[page_key])
        self.page_title.setText(titles.get(key, APP_NAME))
        for page_key, button in self._nav_buttons.items():
            button.setChecked(page_key == key)
        self._set_data_navigation_active(bool(data_view))
        if key == "home":
            self.dashboard_page.refresh()
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
            if self.stack.currentWidget() is self.dashboard_page:
                self.dashboard_page.refresh()
            elif self.stack.currentWidget() is self.statistics_page:
                self.statistics_page.refresh()
            if self.sync_coordinator is not None:
                self._update_sync_status(self.sync_coordinator.engine.status())
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
            if self.stack.currentWidget() is self.dashboard_page:
                self.dashboard_page.refresh()
            elif self.stack.currentWidget() is self.statistics_page:
                self.statistics_page.refresh()
            if self.sync_coordinator is not None:
                self._update_sync_status(self.sync_coordinator.engine.status())
            return True
        except Exception as exc:
            QMessageBox.warning(self, "统计记录失败", f"完整流程已完成，但统计写入失败：\n{exc}")
            return False

    def _change_password(self):
        dialog = PasswordDialog(
            self.database,
            self.account.id,
            require_current=True,
            parent=self,
            session_manager=self.session_manager,
        )
        if dialog.exec_() == dialog.Accepted and dialog.account is not None:
            self.account = dialog.account

    def _shutdown_tools(self):
        if self.workflow_page.shutdown(8000):
            return True

        dialog = QMessageBox(self)
        dialog.setWindowTitle("任务仍在结束")
        dialog.setText("任务仍在结束")
        dialog.setInformativeText(
            "浏览器操作尚未完全结束。你可以继续等待，或先保存所有"
            "已完成记录和计时状态，再强制结束卡住的任务。\n\n"
            "当前正在处理的一条记录可能需要下次重新执行。"
        )
        dialog.setIcon(QMessageBox.Warning)
        dialog.setStandardButtons(QMessageBox.Save | QMessageBox.Cancel)
        force_button = dialog.button(QMessageBox.Save)
        wait_button = dialog.button(QMessageBox.Cancel)
        force_button.setText("保存并强制退出")
        force_button.setObjectName("DangerButton")
        wait_button.setText("继续等待")
        dialog.setDefaultButton(wait_button)
        result = dialog.exec_()
        dialog.deleteLater()
        if result != QMessageBox.Save:
            return False

        if not self.workflow_page.force_shutdown():
            details = getattr(
                self.workflow_page,
                "last_force_shutdown_error",
                "",
            )
            QMessageBox.critical(
                self,
                "数据保护失败",
                details or "已完成数据未能安全保存，程序没有强制退出。",
            )
            return False

        if self.workflow_page.has_running_shutdown_threads():
            self._schedule_hard_exit_fallback()
        return True

    def _schedule_hard_exit_fallback(self, delay_seconds=0.5):
        """仅在线程拒绝 terminate() 时兜底，确保强制退出一定完成。"""
        if self._hard_exit_timer is not None:
            return
        timer = threading.Timer(delay_seconds, os._exit, args=(0,))
        timer.daemon = True
        timer.start()
        self._hard_exit_timer = timer

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

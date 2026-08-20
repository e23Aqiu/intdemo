import os
import threading
from dataclasses import replace

from PyQt5.QtCore import (
    QProcess,
    QProcessEnvironment,
    QSize,
    Qt,
    QTimer,
    pyqtSignal,
)
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
from ..online.api import ApiResponseError
from ..online.captcha_learning import CaptchaLearningService
from ..platform_support import (
    supports_self_update,
    update_install_command,
    update_install_environment,
)
from ..preferences import ClientPreferences
from ..timing import WorkflowTimingService
from ..tools.transport_tool import DDDDOCR_IMPORT_ERROR
from .account_page import AccountPage
from .announcement_page import (
    AnnouncementAdminPage,
    AnnouncementDetailDialog,
    AnnouncementHornButton,
    AnnouncementListDialog,
    AnnouncementTickerButton,
    ContactConversationDialog,
    ContactHistoryDialog,
    UnreadMessageBubble,
    start_api_task,
)
from .auth_dialogs import PasswordDialog, ReconnectDialog
from .dashboard_page import DashboardPage
from .frameless import (
    FramelessMainWindow,
    WindowControls,
)
from .frameless import (
    FramelessMessageBox as QMessageBox,
)
from .machine_learning_page import MachineLearningPage
from .loading_dialog import run_ui_with_loading
from .online_account_page import OnlineAccountPage
from .personal_center_page import PersonalCenterPage
from .statistics_page import StatisticsPage
from .theme import _control_asset_path, install_disabled_cursor_filter
from .update_dialog import UpdatePromptDialog
from .workflow_page import WorkflowPage


class _CurrentPageStack(QStackedWidget):
    """Size a scrollable stack from the page the user can currently see."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.currentChanged.connect(lambda _index: self.updateGeometry())

    def sizeHint(self):
        current = self.currentWidget()
        return current.sizeHint() if current is not None else super().sizeHint()

    def minimumSizeHint(self):
        current = self.currentWidget()
        return (
            current.minimumSizeHint()
            if current is not None
            else super().minimumSizeHint()
        )


class _ClickableFrame(QFrame):
    clicked = pyqtSignal()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self.rect().contains(event.pos()):
            self.clicked.emit()
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event):
        if event.key() in {Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space}:
            self.clicked.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class MainWindow(FramelessMainWindow):
    logout_requested = pyqtSignal()
    window_closed = pyqtSignal()
    PAGE_CANVAS_SIZE = QSize(1220, 700)
    RECONNECT_SYNC_BUSY_MESSAGE = (
        "同步任务正在结束当前操作，暂时无法重新上线，请稍后再试。"
    )
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
        credential_store=None,
        client_preferences=None,
        business_metrics_enabled=True,
        offline_business_mode=False,
    ):
        super().__init__(parent)
        application = QApplication.instance()
        if application is not None:
            install_disabled_cursor_filter(application)
        self.database = database
        self.account = account
        self.offline_business_mode = bool(offline_business_mode)
        self.session_manager = (
            None if self.offline_business_mode else session_manager
        )
        self.sync_coordinator = (
            None if self.offline_business_mode else sync_coordinator
        )
        self.update_coordinator = (
            None if self.offline_business_mode else update_coordinator
        )
        self.credential_store = credential_store
        self._business_metrics_requested = bool(business_metrics_enabled)
        self.business_metrics_enabled = (
            self._business_metrics_requested
            and not self.offline_business_mode
            and account.statistics_enabled
        )
        self.client_preferences = client_preferences or ClientPreferences(
            self.database.path.parent
        )
        self.captcha_learning_client_available = (
            self._captcha_learning_client_is_available()
        )
        self.captcha_learning_service = (
            CaptchaLearningService(
                self.session_manager,
                self,
                database=self.database,
                server_account_id=account.server_account_id,
            )
            if self.captcha_learning_client_available
            else None
        )
        self.available_update = None
        self.downloaded_installer_path = None
        self.update_dialog = None
        self._update_busy = False
        self._update_prompt_blocked = False
        self._prepared_to_close = False
        self._hard_exit_timer = None
        self._nav_buttons = {}
        self._pages = {}
        self._reauth_scheduled = False
        self.announcements = []
        self.announcement_index = 0
        self.announcement_dialog = None
        self.announcement_list_dialog = None
        self.contact_history_dialog = None
        self.contact_conversation_dialogs = {}
        self._announcement_message_unread_count = None
        self._last_sync_state = None
        self._announcement_nav_badge = None
        self._announcement_nav_badge_spacer = None
        self._announcement_poll_task = None
        self._announcement_action_tasks = []
        self._announcement_poll_timer = None
        self._announcement_rotation_timer = None
        self.announcement_admin_page = None
        self.announcement_service_available = self._announcement_service_is_available()
        self.machine_learning_page = None
        self.machine_learning_available = (
            self._captcha_learning_admin_is_available()
        )

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

        self.stack = _CurrentPageStack()
        self.stack.setMinimumWidth(self.PAGE_CANVAS_SIZE.width())
        self.stack.setMinimumHeight(self.PAGE_CANVAS_SIZE.height())
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
        self.statistics_page = None
        self.dashboard_page = None
        if not self.offline_business_mode:
            self.statistics_page = StatisticsPage(database, account, warning)
            self.statistics_page.set_sidebar_navigation(True)
            self.dashboard_page = DashboardPage(
                database,
                account,
                client_preferences=self.client_preferences,
                account_key=account.username,
            )
        self.workflow_timing = (
            WorkflowTimingService(database, account.id)
            if self.business_metrics_enabled
            else None
        )
        self.workflow_page = WorkflowPage(
            self._record_workflow_summary,
            timing_service=self.workflow_timing,
            client_preferences=self.client_preferences,
            account_key=account.username,
            untracked_mode=not self.business_metrics_enabled,
            untracked_label=(
                "游客模式"
                if self.offline_business_mode
                else "测试账号" if account.is_test else "当前模式"
            ),
            captcha_reporter=(
                self.captcha_learning_service.record_attempt
                if self.captcha_learning_service is not None
                else None
            ),
            captcha_model_manager=(
                self.captcha_learning_service.model_manager
                if self.captcha_learning_service is not None
                else None
            ),
            captcha_collection_enabled=(
                self.captcha_learning_service.reporting_enabled
                if self.captcha_learning_service is not None
                else None
            ),
            captcha_sample_collection_enabled=(
                self.captcha_learning_service.sample_collection_enabled
                if self.captcha_learning_service is not None
                else None
            ),
        )
        self.personal_center_page = None
        if not self.offline_business_mode:
            self.personal_center_page = PersonalCenterPage(
                account,
                updates_enabled=self.update_coordinator is not None,
                updates_disabled_message=(
                    "当前系统或处理器架构不支持自动安装更新，业务数据会独立保留。"
                    if not supports_self_update()
                    else ""
                ),
            )
            self.personal_center_page.change_password_requested.connect(
                self._change_password
            )
            self.personal_center_page.logout_requested.connect(self._request_logout)
            self.personal_center_page.check_update_requested.connect(
                self._manual_update_check
            )
            self.personal_center_page.update_requested.connect(
                self._download_available_update
            )
            self.personal_center_page.cancel_update_requested.connect(
                self._cancel_update_download
            )
            self.personal_center_page.install_update_requested.connect(
                self._install_downloaded_update
            )

        if not self.offline_business_mode:
            self._add_page("home", self.dashboard_page)
            self._add_page("statistics", self.statistics_page)
        self._add_page("workflow", self.workflow_page)
        if account.is_admin and not self.offline_business_mode:
            if self.announcement_service_available:
                self.announcement_admin_page = AnnouncementAdminPage(
                    self.session_manager,
                )
                self.announcement_admin_page.announcements_changed.connect(
                    self.refresh_announcements
                )
                self.announcement_admin_page.unread_messages_changed.connect(
                    self._set_announcement_message_indicator
                )
                self._add_page(
                    "announcements_admin",
                    self.announcement_admin_page,
                )
            if self.machine_learning_available:
                self.machine_learning_page = MachineLearningPage(
                    self.session_manager,
                    self.captcha_learning_service,
                )
                self._add_page("machine_learning", self.machine_learning_page)
            if self.session_manager is not None:
                self.account_page = OnlineAccountPage(
                    database,
                    account,
                    self.session_manager,
                )
            else:
                self.account_page = AccountPage(database, account)
            self.account_page.account_name_changed.connect(self._account_name_changed)
            self.account_page.account_permission_changed.connect(
                self._account_data_changed
            )
            self.account_page.account_deleted.connect(self._account_data_changed)
            self.account_page.station_data_changed.connect(self._account_data_changed)
            self._add_page("accounts", self.account_page)
        else:
            self.account_page = None
        if not self.offline_business_mode:
            self._add_page("personal", self.personal_center_page)

        if self.sync_coordinator is not None:
            self.sync_coordinator.status_changed.connect(self._update_sync_status)
            self.sync_coordinator.data_changed.connect(self._online_data_changed)
            messages_changed = getattr(
                self.sync_coordinator,
                "messages_changed",
                None,
            )
            if messages_changed is not None:
                messages_changed.connect(self.refresh_announcements)
            self._update_sync_status(self.sync_coordinator.engine.status())
        if self.captcha_learning_service is not None:
            self.captcha_learning_service.policy_changed.connect(
                self._update_captcha_sync_policy
            )
            self.captcha_learning_service.pending_count_changed.connect(
                self._update_captcha_pending_count
            )
            self._update_captcha_sync_policy(
                self.captcha_learning_service.policy
            )
            self._update_captcha_pending_count(
                self.captcha_learning_service.pending_upload_count()
            )
        if self.update_coordinator is not None:
            self.update_coordinator.update_available.connect(self._update_available)
            self.update_coordinator.state_changed.connect(self._update_download_state)
            self.update_coordinator.download_progress.connect(
                self._update_download_progress
            )
            speed_signal = getattr(
                self.update_coordinator,
                "download_speed",
                None,
            )
            if speed_signal is not None:
                speed_signal.connect(self._update_download_speed)
            self.update_coordinator.download_completed.connect(self._update_downloaded)

        self.show_page(
            "workflow"
            if self.offline_business_mode
            else ("home" if account.is_admin else "workflow")
        )
        if self.announcement_service_available:
            self._start_announcement_polling()
        if self.captcha_learning_service is not None:
            self.captcha_learning_service.start()

    def _announcement_service_is_available(self):
        api = getattr(self.session_manager, "api", None)
        required = [
            "announcements",
            "mark_announcement_read",
            "download_announcement_attachment",
            "send_admin_message",
        ]
        if self.account.is_admin and not self.offline_business_mode:
            required.extend(
                [
                    "admin_announcements",
                    "admin_create_announcement",
                    "admin_update_announcement",
                    "admin_delete_announcement",
                    "admin_add_announcement_attachment",
                    "admin_delete_announcement_attachment",
                    "admin_messages",
                    "admin_mark_message_read",
                    "admin_accounts",
                ]
            )
        return api is not None and all(
            callable(getattr(api, name, None)) for name in required
        )

    def _captcha_learning_client_is_available(self):
        api = getattr(self.session_manager, "api", None)
        required = [
            "captcha_policy",
            "submit_captcha_attempt",
            "current_captcha_model",
        ]
        return (
            not self.offline_business_mode
            and api is not None
            and all(callable(getattr(api, name, None)) for name in required)
        )

    def _captcha_learning_admin_is_available(self):
        if not self.account.is_admin or not self.captcha_learning_client_available:
            return False
        api = getattr(self.session_manager, "api", None)
        required = [
            "admin_captcha_learning_overview",
            "admin_update_captcha_policy",
            "admin_export_captcha_dataset",
            "admin_import_captcha_dataset",
            "admin_captcha_samples",
            "admin_delete_captcha_samples",
            "admin_create_captcha_model",
            "admin_activate_captcha_model",
            "admin_use_builtin_captcha_model",
            "admin_delete_captcha_model",
        ]
        return api is not None and all(
            callable(getattr(api, name, None)) for name in required
        )

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
        self.sidebar_brand_badge = QLabel("查")
        self.sidebar_brand_badge.setObjectName("BrandBadge")
        self.sidebar_brand_badge.setAlignment(Qt.AlignCenter)
        self.sidebar_brand_badge.setFixedSize(44, 44)
        brand_row.addWidget(self.sidebar_brand_badge)

        brand_text = QVBoxLayout()
        brand_text.setContentsMargins(0, 0, 0, 0)
        brand_text.setSpacing(1)
        brand = QLabel(APP_NAME.replace("信息智能查询平台", "信息\n智能查询平台"))
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

        primary_nav_items = (
            [("workflow", "一键业务处理", "nav-workflow.svg")]
            if self.offline_business_mode
            else [
                ("home", "数据仪表盘", "nav-dashboard.svg"),
                ("workflow", "一键业务处理", "nav-workflow.svg"),
            ]
        )
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
        self.data_nav_toggle.setVisible(not self.offline_business_mode)
        layout.addWidget(self.data_nav_toggle)

        self.data_nav_container = QWidget()
        self.data_nav_container.setObjectName("DataNavContainer")
        data_nav_layout = QVBoxLayout(self.data_nav_container)
        data_nav_layout.setContentsMargins(0, 0, 0, 2)
        data_nav_layout.setSpacing(3)
        data_nav_items = (
            []
            if self.offline_business_mode
            else [
                ("data_station", "全站分布"),
                ("data_timing", "用时效率"),
                ("data_completion", "完成类型"),
                ("data_violation", "违规原因"),
            ]
        )
        for key, text in data_nav_items:
            data_nav_layout.addWidget(
                self._create_nav_button(key, text, object_name="NavSubButton")
            )
        self.data_nav_container.hide()
        layout.addWidget(self.data_nav_container)

        trailing_nav_items = []
        if self.account.is_admin and not self.offline_business_mode:
            if self.announcement_service_available:
                trailing_nav_items.append(
                    (
                        "announcements_admin",
                        "公告发布",
                        "nav-announcement.svg",
                    )
                )
            if self.machine_learning_available:
                trailing_nav_items.append(
                    ("machine_learning", "机器学习", "nav-ml.svg")
                )
            trailing_nav_items.append(("accounts", "账号管理", "nav-accounts.svg"))
        if not self.offline_business_mode:
            trailing_nav_items.append(("personal", "系统设置", "nav-user.svg"))
        for key, text, icon_name in trailing_nav_items:
            if key == "announcements_admin":
                self._announcement_nav_badge_spacer = QWidget(sidebar)
                self._announcement_nav_badge_spacer.setFixedHeight(20)
                self._announcement_nav_badge_spacer.hide()
                layout.addWidget(self._announcement_nav_badge_spacer)
            button = self._create_nav_button(key, text)
            button.setIcon(QIcon(_control_asset_path(icon_name)))
            button.setIconSize(QSize(19, 19))
            layout.addWidget(button)
        if self.offline_business_mode:
            self.guest_logout_button = QPushButton("退出游客模式")
            self.guest_logout_button.setObjectName("DangerButton")
            self.guest_logout_button.setMinimumHeight(42)
            self.guest_logout_button.clicked.connect(self._request_logout)
            layout.addWidget(self.guest_logout_button)

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
            "离线游客模式"
            if self.offline_business_mode
            else (
                "等待连接"
                if self.sync_coordinator is not None
                else "本地模式"
            )
        )
        self.sync_state_title.setObjectName("SyncStateTitle")
        sync_header.addWidget(self.sync_state_title)
        sync_header.addStretch()
        sync_layout.addLayout(sync_header)

        self.sync_detail_label = QLabel(
            "仅处理业务，不记录统计、计时或同步数据"
            if self.offline_business_mode
            else (
                "数据仅保存在本机"
                if self.sync_coordinator is None
                else "正在读取同步状态"
            )
        )
        self.sync_detail_label.setObjectName("SyncDetail")
        self.sync_detail_label.setWordWrap(True)
        sync_layout.addWidget(self.sync_detail_label)

        self.sync_error_label = QLabel("")
        self.sync_error_label.setObjectName("SyncError")
        self.sync_error_label.setWordWrap(True)
        self.sync_error_label.hide()
        sync_layout.addWidget(self.sync_error_label)
        self.sync_retry_button = QPushButton("未上传：0")
        self.sync_retry_button.setObjectName("SyncActionButton")
        self.sync_retry_button.setProperty("hasPending", False)
        self.sync_retry_button.setProperty("split", False)
        self.sync_retry_button.setSizePolicy(
            QSizePolicy.Ignored,
            QSizePolicy.Fixed,
        )
        self.sync_retry_button.setVisible(self.sync_coordinator is not None)
        self.sync_retry_button.clicked.connect(self._retry_sync)
        self.captcha_sync_retry_button = QPushButton("验证码待同步：0")
        self.captcha_sync_retry_button.setObjectName("SyncActionButton")
        self.captcha_sync_retry_button.setProperty("hasPending", False)
        self.captcha_sync_retry_button.setProperty("split", False)
        self.captcha_sync_retry_button.setSizePolicy(
            QSizePolicy.Ignored,
            QSizePolicy.Fixed,
        )
        self.captcha_sync_retry_button.hide()
        self.captcha_sync_retry_button.clicked.connect(
            self._retry_captcha_sync
        )
        sync_actions = QHBoxLayout()
        sync_actions.setContentsMargins(0, 0, 0, 0)
        sync_actions.setSpacing(5)
        sync_actions.addWidget(self.sync_retry_button, 1)
        sync_actions.addWidget(self.captcha_sync_retry_button, 1)
        sync_layout.addLayout(sync_actions)
        # Compatibility alias retained for older UI automation.
        self.sync_pending_badge = self.sync_retry_button
        # Compatibility alias for older UI automation.
        self.sync_status_label = self.sync_detail_label
        layout.addWidget(sync_card)

        profile = _ClickableFrame()
        profile.setObjectName("SidebarProfile")
        profile.setFocusPolicy(Qt.StrongFocus)
        profile.setProperty("reconnectAvailable", False)
        profile.clicked.connect(self._reconnect_online)
        self.sidebar_profile = profile
        profile_layout = QHBoxLayout(profile)
        profile_layout.setContentsMargins(11, 10, 11, 10)
        profile_layout.setSpacing(10)
        self.sidebar_avatar = QLabel(self._sidebar_avatar_text(self.account.name_label))
        self.sidebar_avatar.setObjectName("SidebarAvatar")
        self.sidebar_avatar.setAlignment(Qt.AlignCenter)
        self.sidebar_avatar.setFixedSize(36, 36)
        self.sidebar_avatar.setAttribute(Qt.WA_TransparentForMouseEvents)
        profile_layout.addWidget(self.sidebar_avatar)

        identity_layout = QVBoxLayout()
        identity_layout.setContentsMargins(0, 0, 0, 0)
        identity_layout.setSpacing(1)
        self.sidebar_user = QLabel(self.account.name_label)
        self.sidebar_user.setObjectName("SidebarUser")
        self.sidebar_user.setToolTip(self.account.name_label)
        self.sidebar_user.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.sidebar_role = QLabel(
            "游客  ·  不记录数据"
            if self.offline_business_mode
            else f"{self.account.role_label}  ·  {self.account.username}"
        )
        self.sidebar_role.setObjectName("SidebarRole")
        self.sidebar_role.setAttribute(Qt.WA_TransparentForMouseEvents)
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
        self.data_nav_toggle.setText("数据中心    ▾" if expanded else "数据中心    ▸")

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
        layout.setSpacing(8)
        # 各页面已经在内容区展示标题，窗口控制栏用于公告轮播和窗口按钮。
        self.page_title = QLabel(bar)
        self.page_title.hide()
        self.offline_mode_badge = QLabel(
            "离线游客模式 · 仅处理业务，不记录统计、计时或同步数据"
        )
        self.offline_mode_badge.setObjectName("OfflineBusinessBadge")
        self.offline_mode_badge.setVisible(self.offline_business_mode)
        layout.addWidget(self.offline_mode_badge)
        self.announcement_horn_button = AnnouncementHornButton()
        self.announcement_horn_button.setObjectName("AnnouncementHornButton")
        self.announcement_horn_button.setIcon(
            QIcon(_control_asset_path("announcement.svg"))
        )
        self.announcement_horn_button.setIconSize(QSize(20, 20))
        self.announcement_horn_button.setFixedSize(38, 38)
        self.announcement_horn_button.set_unread_overlay_parent(
            self.centralWidget()
        )
        self.announcement_horn_button.setToolTip("暂无公告")
        self.announcement_horn_button.setEnabled(False)
        self.announcement_horn_button.setVisible(
            self.announcement_service_available
        )
        self.announcement_horn_button.clicked.connect(self._show_announcement_list)
        layout.addWidget(self.announcement_horn_button)

        self.announcement_ticker_button = AnnouncementTickerButton(
            "暂无公告",
            bar,
        )
        self.announcement_ticker_button.setObjectName("AnnouncementTickerButton")
        self.announcement_ticker_button.setSizePolicy(
            QSizePolicy.Preferred,
            QSizePolicy.Fixed,
        )
        self.announcement_ticker_button.setMinimumWidth(260)
        self.announcement_ticker_button.setMinimumHeight(38)
        self.announcement_ticker_button.setMaximumWidth(480)
        self.announcement_ticker_button.setEnabled(False)
        self.announcement_ticker_button.setVisible(
            self.announcement_service_available
        )
        self.announcement_ticker_button.clicked.connect(self._show_current_announcement)
        layout.addWidget(self.announcement_ticker_button, 1)
        layout.addStretch()
        self.window_controls = WindowControls(self, bar)
        layout.addWidget(self.window_controls)
        self.register_window_drag_region(bar)
        return bar

    def _start_announcement_polling(self):
        self._announcement_poll_timer = QTimer(self)
        self._announcement_poll_timer.setInterval(30_000)
        self._announcement_poll_timer.timeout.connect(self.refresh_announcements)
        self._announcement_poll_timer.start()

        self._announcement_rotation_timer = QTimer(self)
        self._announcement_rotation_timer.setInterval(7_000)
        self._announcement_rotation_timer.timeout.connect(self._rotate_announcement)
        self._announcement_rotation_timer.start()
        QTimer.singleShot(0, self.refresh_announcements)

    def refresh_announcements(self):
        if (
            not self.announcement_service_available
            or self._announcement_poll_task is not None
        ):
            return

        def load():
            token = self.session_manager.access_token()
            result = {
                "announcements": self.session_manager.api.announcements(
                    token,
                    limit=100,
                ),
                "unread_messages": 0,
            }
            if self.account.is_admin:
                inbox = self.session_manager.api.admin_messages(
                    token,
                    unread_only=True,
                    limit=1,
                )
                result["unread_messages"] = int((inbox or {}).get("unread_count") or 0)
            else:
                unread_loader = getattr(
                    self.session_manager.api,
                    "contact_conversation_unread_count",
                    None,
                )
                if callable(unread_loader):
                    try:
                        result["unread_messages"] = int(unread_loader(token))
                    except ApiResponseError as exc:
                        if exc.status_code != 404:
                            raise
                        result["unread_messages"] = 0
                else:
                    result["unread_messages"] = 0
            return result

        self._announcement_poll_task = start_api_task(
            load,
            self._announcements_loaded,
        )

    def _announcements_loaded(self, result, error):
        self._announcement_poll_task = None
        if error is not None or not isinstance(result, dict):
            return
        current_id = None
        if self.announcements:
            current_id = str(
                self.announcements[self.announcement_index].get("id") or ""
            )
        self.announcements = list(result.get("announcements") or [])
        self.announcement_index = 0
        if current_id:
            for index, announcement in enumerate(self.announcements):
                if str(announcement.get("id") or "") == current_id:
                    self.announcement_index = index
                    break
        self._set_announcement_message_indicator(
            int(result.get("unread_messages") or 0)
        )
        self._update_announcement_ticker()
        announcement_list = self.announcement_list_dialog
        if announcement_list is not None and announcement_list.isVisible():
            announcement_list.set_announcements(self.announcements)
        startup_announcement = next(
            (
                announcement
                for announcement in self.announcements
                if announcement.get("startup_pending")
            ),
            None,
        )
        if startup_announcement is not None:
            QTimer.singleShot(
                0,
                lambda item=startup_announcement: self._open_announcement(
                    item, startup_shown=True
                ),
            )

    def _rotate_announcement(self):
        if len(self.announcements) < 2:
            return
        self.announcement_index = (self.announcement_index + 1) % len(
            self.announcements
        )
        self._update_announcement_ticker()

    def _update_announcement_ticker(self):
        has_announcements = bool(self.announcements)
        controls_available = not self._update_busy and not self._update_prompt_blocked
        self.announcement_horn_button.setEnabled(
            controls_available and (has_announcements or not self.account.is_admin)
        )
        self.announcement_ticker_button.setEnabled(
            controls_available and has_announcements
        )
        message_unread = (
            0
            if self.account.is_admin
            else max(0, int(self._announcement_message_unread_count or 0))
        )
        if not has_announcements:
            self.announcement_horn_button.setProperty(
                "hasUnread",
                bool(message_unread),
            )
            self.announcement_horn_button.setToolTip(
                (
                    f"有 {message_unread} 条管理员未读回复，点击查看历史会话"
                    if message_unread
                    else "查看历史会话"
                )
                if not self.account.is_admin
                else "暂无公告"
            )
            self.announcement_ticker_button.setText("暂无公告")
            self.announcement_ticker_button.setToolTip("")
            self.announcement_ticker_button.set_announcement(None)
        else:
            self.announcement_index %= len(self.announcements)
            announcement = self.announcements[self.announcement_index]
            ticker_text = str(
                announcement.get("ticker_text")
                or announcement.get("title")
                or "查看公告"
            )
            unread_announcements = bool(
                not self.account.is_admin
                and any(
                    not announcement.get("read_at")
                    for announcement in self.announcements
                )
            )
            unread = bool(unread_announcements or message_unread)
            self.announcement_horn_button.setProperty("hasUnread", unread)
            self.announcement_horn_button.setToolTip(
                (
                    f"有 {message_unread} 条管理员未读回复"
                    + ("和未读公告" if unread_announcements else "")
                    + "，点击查看"
                )
                if message_unread
                else (
                    "有未读公告，点击查看全部公告"
                    if unread_announcements
                    else "点击查看全部公告"
                )
            )
            self.announcement_ticker_button.setText(ticker_text)
            self.announcement_ticker_button.setToolTip("")
            self.announcement_ticker_button.set_announcement(
                announcement,
                self.announcement_index,
                len(self.announcements),
                show_unread=not self.account.is_admin,
            )
        self.announcement_horn_button.style().unpolish(self.announcement_horn_button)
        self.announcement_horn_button.style().polish(self.announcement_horn_button)

    def _show_current_announcement(self, *_args):
        if not self.announcements:
            return
        self.announcement_index %= len(self.announcements)
        self._open_announcement(
            self.announcements[self.announcement_index],
            startup_shown=False,
        )

    def _show_announcement_list(self, *_args):
        if not self.announcements and self.account.is_admin:
            return
        current = self.announcement_list_dialog
        if current is not None and current.isVisible():
            current.raise_()
            current.activateWindow()
            return
        dialog = AnnouncementListDialog(
            self.announcements,
            self,
            show_unread=not self.account.is_admin,
            message_unread_count=(
                0
                if self.account.is_admin
                else self._announcement_message_unread_count or 0
            ),
        )

        def clear_dialog(*_args):
            if self.announcement_list_dialog is dialog:
                self.announcement_list_dialog = None

        dialog.announcement_open_requested.connect(
            lambda announcement: self._open_announcement(
                announcement,
                startup_shown=False,
            )
        )
        dialog.announcement_read_requested.connect(self._confirm_announcement_read)
        dialog.conversation_history_requested.connect(self._show_contact_history)
        dialog.finished.connect(clear_dialog)
        self.announcement_list_dialog = dialog
        dialog.open()

    def _show_contact_history(self):
        if self.account.is_admin or self.session_manager is None:
            return
        current = self.contact_history_dialog
        if current is not None and current.isVisible():
            current.raise_()
            current.activateWindow()
            return
        dialog = ContactHistoryDialog(self.session_manager, parent=None)

        def clear_dialog(*_args):
            if self.contact_history_dialog is dialog:
                self.contact_history_dialog = None

        dialog.finished.connect(clear_dialog)
        dialog.unread_count_changed.connect(self._set_announcement_message_indicator)
        dialog.messages_changed.connect(self.refresh_announcements)
        self.contact_history_dialog = dialog
        self._set_announcement_message_indicator(dialog.total_unread_count)
        dialog.show()

    def _show_contact_conversation(self, announcement):
        if self.account.is_admin or self.session_manager is None:
            return
        announcement_id = str((announcement or {}).get("id") or "")
        key = announcement_id or "new"
        current = self.contact_conversation_dialogs.get(key)
        if current is not None and current.isVisible():
            current.raise_()
            current.activateWindow()
            current.message_edit.setFocus()
            return
        dialog = ContactConversationDialog(
            announcement,
            self.session_manager,
            parent=None,
        )

        def clear_dialog(*_args):
            if self.contact_conversation_dialogs.get(key) is dialog:
                self.contact_conversation_dialogs.pop(key, None)

        dialog.finished.connect(clear_dialog)
        dialog.unread_count_changed.connect(self._set_announcement_message_indicator)
        dialog.messages_changed.connect(self.refresh_announcements)
        self.contact_conversation_dialogs[key] = dialog
        self._set_announcement_message_indicator(dialog.total_unread_count)
        dialog.show()

    def _open_announcement(self, announcement, *, startup_shown):
        current = self.announcement_dialog
        if current is not None and current.isVisible():
            current.raise_()
            current.activateWindow()
            return
        dialog = AnnouncementDetailDialog(
            announcement,
            self.session_manager,
            self.account,
            self,
        )

        def clear_dialog(*_args):
            if self.announcement_dialog is dialog:
                self.announcement_dialog = None

        dialog.finished.connect(clear_dialog)
        dialog.read_confirmed.connect(
            lambda current=announcement: self._confirm_announcement_read(current)
        )
        dialog.contact_requested.connect(self._show_contact_conversation)
        self.announcement_dialog = dialog
        if startup_shown:
            announcement["startup_pending"] = False
        self._mark_announcement_read(
            announcement,
            startup_shown=bool(startup_shown),
            confirmed=False,
        )
        self._update_announcement_ticker()
        dialog.open()

    def _confirm_announcement_read(self, announcement):
        announcement["read_at"] = announcement.get("read_at") or True
        self._update_announcement_ticker()
        if self.announcement_list_dialog is not None:
            self.announcement_list_dialog.set_announcements(self.announcements)
        self._mark_announcement_read(
            announcement,
            startup_shown=False,
            confirmed=True,
        )

    def _mark_announcement_read(self, announcement, startup_shown, confirmed=True):
        announcement_id = str(announcement.get("id") or "")
        if not announcement_id:
            return

        task = None

        def completed(_result, _error):
            if task in self._announcement_action_tasks:
                self._announcement_action_tasks.remove(task)

        def mark_read():
            try:
                return self.session_manager.api.mark_announcement_read(
                    self.session_manager.access_token(),
                    announcement_id,
                    startup_shown=bool(startup_shown),
                    confirmed=bool(confirmed),
                )
            except TypeError:
                return self.session_manager.api.mark_announcement_read(
                    self.session_manager.access_token(),
                    announcement_id,
                    startup_shown=bool(startup_shown),
                )

        task = start_api_task(
            mark_read,
            completed,
        )
        self._announcement_action_tasks.append(task)

    def _set_announcement_message_indicator(self, unread_count):
        unread_count = max(0, int(unread_count or 0))
        previous = self._announcement_message_unread_count
        self._announcement_message_unread_count = unread_count
        if (
            previous is not None
            and unread_count > previous
            and QApplication.instance() is not None
        ):
            QApplication.alert(self, 0)
        if not self.account.is_admin:
            self.announcement_horn_button.set_message_unread_count(unread_count)
            if self.announcement_list_dialog is not None:
                self.announcement_list_dialog.set_message_unread_count(
                    unread_count
                )
            self._update_announcement_ticker()
            return
        if self.announcement_admin_page is not None:
            self.announcement_admin_page.set_unread_message_count(unread_count)
        button = self._nav_buttons.get("announcements_admin")
        if button is None:
            return
        button.setProperty("hasMessage", bool(unread_count))
        button.setText("公告发布")
        button.setToolTip(f"收到 {unread_count} 条未读用户消息" if unread_count else "")
        if self._announcement_nav_badge_spacer is not None:
            self._announcement_nav_badge_spacer.setVisible(bool(unread_count))
        if self._announcement_nav_badge is None:
            self._announcement_nav_badge = UnreadMessageBubble(
                button,
                self.centralWidget(),
            )
        self._announcement_nav_badge.set_unread_count(unread_count)
        button.style().unpolish(button)
        button.style().polish(button)

    def _update_available(self, update):
        self.available_update = update
        self.downloaded_installer_path = None
        self.personal_center_page.set_update_available(update)
        self._set_settings_update_indicator(True)
        ignored = self.client_preferences.ignored_update_version
        if update.mandatory or ignored != update.version:
            QTimer.singleShot(
                0,
                lambda current=update: self._show_update_dialog(current),
            )
        else:
            self.personal_center_page.update_status_label.setText(
                f"v{update.version} 已设为不再提示；仍可在这里手动更新。"
            )

    def _manual_update_check(self):
        if self.update_coordinator is not None and not self._update_busy:
            self.update_coordinator.check(manual=True)

    def _show_update_dialog(self, update):
        if self.available_update is not update:
            return
        current = self.update_dialog
        if current is not None and current.isVisible():
            current.raise_()
            current.activateWindow()
            return
        dialog = UpdatePromptDialog(update, self)
        dialog.update_requested.connect(self._download_available_update)
        dialog.background_update_requested.connect(
            lambda: self._download_available_update(background=True)
        )
        dialog.cancel_requested.connect(self._cancel_update_download)
        dialog.ignore_requested.connect(
            lambda version=update.version: self._ignore_update(version)
        )
        dialog.restart_requested.connect(self._install_downloaded_update)

        def clear_dialog(*_args):
            if self.update_dialog is dialog:
                self.update_dialog = None
                self._set_update_prompt_blocked(False)

        dialog.finished.connect(clear_dialog)
        self.update_dialog = dialog
        # A Qt-modal dialog also blocks the custom title-bar controls. Keep
        # this prompt modeless and emulate modality only for the application
        # content so minimize, maximize, and close always remain available.
        self._set_update_prompt_blocked(True)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _ignore_update(self, version):
        self.client_preferences.ignore_update(version)
        self.personal_center_page.update_status_label.setText(
            f"已取消 v{version} 的自动提示；可随时在此手动更新。"
        )

    def _download_available_update(self, *, background=False):
        update = self.available_update
        if update is None or self.update_coordinator is None:
            return
        dialog = self.update_dialog
        if not self.update_coordinator.download(update):
            if dialog is not None:
                dialog.set_error("更新服务正忙，请稍后重试。")
            return
        if dialog is not None and dialog.update is update:
            dialog.begin_download()
        self._set_update_prompt_blocked(False)
        self.client_preferences.clear_ignored_update()
        self._set_update_busy(True)
        if background and dialog is not None and not dialog.mandatory:
            dialog.allow_close()
            dialog.reject()

    def _cancel_update_download(self):
        if self.update_coordinator is None:
            return
        self.update_coordinator.cancel_download()

    def _update_download_state(self, state, message):
        self.personal_center_page.set_update_state(state, message)
        if state == "up_to_date":
            self.available_update = None
            self.downloaded_installer_path = None
            self._set_settings_update_indicator(False)
        elif state == "check_error":
            QMessageBox.warning(self, "检查更新失败", message)
        elif state == "downloading":
            self._set_update_busy(True)
        elif state == "cancelling":
            self._set_update_busy(True)
        elif state == "download_cancelled":
            self._set_update_busy(False)
            self._set_update_prompt_blocked(False)
            dialog = self.update_dialog
            if dialog is not None:
                dialog.set_cancelled()
        elif state == "download_error":
            self._set_update_busy(False)
            self._set_update_prompt_blocked(False)
            dialog = self.update_dialog
            if dialog is not None:
                dialog.set_error(message)
            else:
                QMessageBox.warning(self, "更新下载失败", message)

    def _update_download_progress(self, received, total):
        self.personal_center_page.set_update_progress(received, total)
        dialog = self.update_dialog
        if dialog is not None:
            dialog.set_progress(received, total)

    def _update_download_speed(self, bytes_per_second):
        self.personal_center_page.set_update_speed(bytes_per_second)
        dialog = self.update_dialog
        if dialog is not None:
            dialog.set_speed(bytes_per_second)

    def _update_downloaded(self, installer_path):
        self.downloaded_installer_path = installer_path
        self._set_update_busy(False)
        self._set_update_prompt_blocked(False)
        self.personal_center_page.set_update_state(
            "downloaded",
            "更新包已下载并校验完成，需要重启程序并运行安装程序。",
        )
        dialog = self.update_dialog
        if dialog is not None:
            dialog.set_downloaded()
            return
        running = bool(getattr(self.workflow_page, "pipeline_running", False))
        reply = QMessageBox.question(
            self,
            "更新下载完成",
            "更新包已下载并通过 SHA-256 校验。\n\n"
            + (
                "安装需要保存已完成数据并停止当前业务。是否现在安装？"
                if running
                else "安装会关闭程序。是否现在安装？"
            ),
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            self._install_downloaded_update(confirmed=True)

    def _install_downloaded_update(self, *, confirmed=False):
        installer_path = self.downloaded_installer_path
        if installer_path is None:
            return
        running = bool(getattr(self.workflow_page, "pipeline_running", False))
        message = (
            "安装更新必须停止当前业务处理并关闭程序。\n\n"
            "确定现在保存已完成数据、停止业务并安装吗？"
            if running
            else "安装更新将关闭当前程序。\n\n确定现在安装吗？"
        )
        if not confirmed:
            reply = QMessageBox.question(
                self,
                "安装更新前确认",
                message,
                QMessageBox.Yes | QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return
        if not self._prepare_close():
            return
        try:
            installer_program, installer_arguments = update_install_command(
                installer_path
            )
        except RuntimeError as exc:
            self._prepared_to_close = False
            QMessageBox.critical(self, "无法启动安装包", str(exc))
            return
        installer_process = QProcess()
        installer_process.setProgram(installer_program)
        installer_process.setArguments(installer_arguments)
        process_environment = QProcessEnvironment()
        for name, value in update_install_environment().items():
            process_environment.insert(name, value)
        installer_process.setProcessEnvironment(process_environment)
        launched = installer_process.startDetached()
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
        if self.update_dialog is not None:
            self.update_dialog.allow_close()
            self.update_dialog.accept()
        self.close()

    def _set_settings_update_indicator(self, available):
        button = self._nav_buttons.get("personal")
        if button is None:
            return
        button.setProperty("hasUpdate", bool(available))
        button.setText("系统设置    ●" if available else "系统设置")
        button.setToolTip("新版本发布!" if available else "")
        button.style().unpolish(button)
        button.style().polish(button)

    def _set_update_prompt_blocked(self, blocked):
        blocked = bool(blocked)
        self._update_prompt_blocked = blocked
        # Reproduce the useful part of a modal prompt without disabling the
        # top bar. Disabling the whole window would make its custom system
        # buttons unable to minimize, maximize, or enter closeEvent().
        self.sidebar.setEnabled(not blocked)
        self.page_scroll_area.setEnabled(not blocked)
        self._update_announcement_ticker()
        self.window_controls.setEnabled(True)
        for button in (
            self.window_controls.minimize_button,
            self.window_controls.maximize_button,
            self.window_controls.close_button,
        ):
            button.setEnabled(True)

    def _set_update_busy(self, busy):
        busy = bool(busy)
        if self._update_busy == busy:
            return
        self._update_busy = busy
        # Downloading is a background operation. Business pages intentionally
        # remain interactive; only another update check is blocked.
        self.personal_center_page.check_update_btn.setEnabled(not busy)
        self._update_announcement_ticker()
        self.window_controls.setEnabled(True)
        if self.sync_coordinator is not None:
            self._update_sync_status(self.sync_coordinator.engine.status())

    def _retry_sync(self):
        if self.sync_coordinator is None or self._update_busy:
            return
        state = getattr(self.session_manager, "state", None)
        if state is not None and state.mode == "reauth_required":
            self._reconnect_online()
            return
        self.sync_coordinator.retry_now()

    def _retry_captcha_sync(self):
        if self.captcha_learning_service is None:
            return
        self.captcha_learning_service.retry_pending()
        self._update_captcha_pending_count(
            self.captcha_learning_service.pending_upload_count()
        )

    def _update_captcha_sync_policy(self, policy):
        if not hasattr(self, "captcha_sync_retry_button"):
            return
        policy = policy or {}
        upload_mode = str(policy.get("upload_mode") or "")
        if not upload_mode:
            upload_mode = (
                "samples_and_metrics"
                if policy.get("upload_enabled")
                else "off"
            )
        split = (
            upload_mode == "samples_and_metrics"
            and self.captcha_learning_service is not None
        )
        self.captcha_sync_retry_button.setVisible(split)
        for button in (
            self.sync_retry_button,
            self.captcha_sync_retry_button,
        ):
            button.setProperty("split", split)
            button.style().unpolish(button)
            button.style().polish(button)

    def _update_captcha_pending_count(self, count):
        if not hasattr(self, "captcha_sync_retry_button"):
            return
        count = max(0, int(count or 0))
        self.captcha_sync_retry_button.setText(f"验证码待同步：{count}")
        self.captcha_sync_retry_button.setProperty("hasPending", bool(count))
        self.captcha_sync_retry_button.setToolTip(
            "点击重新上传失败的验证码样本"
            if count
            else "没有待上传的验证码样本"
        )
        self.captcha_sync_retry_button.style().unpolish(
            self.captcha_sync_retry_button
        )
        self.captcha_sync_retry_button.style().polish(
            self.captcha_sync_retry_button
        )

    def _update_sync_status(self, status):
        if not hasattr(self, "sync_status_card"):
            return
        self._last_sync_state = status.state
        if status.state == "reauth_required":
            invalidate = getattr(
                self.session_manager,
                "invalidate_credentials",
                None,
            )
            if callable(invalidate):
                invalidate()
        labels = {
            "online": "在线",
            "syncing": "同步中",
            "offline": "离线",
            "error": "同步错误",
            "reauth_required": "离线，需重新上线",
        }
        state_label = labels.get(status.state, status.state)
        self.sync_state_title.setText(state_label)
        for widget in (self.sync_status_card, self.sync_state_dot):
            widget.setProperty("state", status.state)
            widget.style().unpolish(widget)
            widget.style().polish(widget)

        pending_text = f"未上传：{status.pending_count}"
        if status.quarantined_count:
            pending_text += f"（隔离：{status.quarantined_count}）"
        if status.state == "syncing":
            pending_text = f"同步中…  {pending_text}"
        self.sync_pending_badge.setText(pending_text)
        self.sync_pending_badge.setProperty(
            "hasPending",
            bool(status.pending_count or status.quarantined_count),
        )
        self.sync_pending_badge.style().unpolish(self.sync_pending_badge)
        self.sync_pending_badge.style().polish(self.sync_pending_badge)

        details = []
        if (
            status.state in {"offline", "reauth_required"}
            and self.session_manager is not None
            and self.session_manager.state is not None
        ):
            expires = self.session_manager.state.offline_expires_at
            details.append(f"离线授权至 {str(expires).replace('T', ' ')[:19]}")
        if status.state == "reauth_required":
            details.append("当前任务继续，数据保存在本机")
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
            self.sync_retry_button.setEnabled(
                status.state != "syncing" and not self._update_busy
            )
            quarantined = self.database.get_sync_quarantined_items(limit=5)
            self.sync_retry_button.setToolTip(
                "\n".join(
                    f"{row['kind']}: {row['last_error_message']}" for row in quarantined
                )
            )
        session_state = getattr(self.session_manager, "state", None)
        reconnect_unavailable_reason = self._reconnect_unavailable_reason(
            status.state
        )
        reconnect_available = bool(
            session_state is not None
            and not reconnect_unavailable_reason
        )
        self.sidebar_profile.setProperty(
            "reconnectAvailable",
            reconnect_available,
        )
        self.sidebar_profile.setCursor(
            Qt.PointingHandCursor if reconnect_available else Qt.ArrowCursor
        )
        self.sidebar_profile.setToolTip(
            "点击输入密码并重新上线"
            if reconnect_available
            else reconnect_unavailable_reason
        )
        self.sidebar_profile.style().unpolish(self.sidebar_profile)
        self.sidebar_profile.style().polish(self.sidebar_profile)
        self.sidebar_role.setText(
            f"{self.account.role_label}  ·  点击重新上线"
            if reconnect_available
            else f"{self.account.role_label}  ·  {self.account.username}"
        )

    def _require_reauthentication(self):
        self._reconnect_online()

    def _reconnect_unavailable_reason(self, sync_state=None):
        if self.session_manager is None:
            if self.offline_business_mode:
                return (
                    "当前为离线游客模式，没有可恢复的在线登录会话。"
                    "请先退出游客模式，再使用账号登录。"
                )
            return (
                "当前未连接在线登录服务，无法从账号区域重新上线。"
                "请返回登录页后重新登录。"
            )
        state = self.session_manager.state
        if state is None:
            return "当前没有可恢复的登录会话，请返回登录页后重新登录。"
        if state.mode == "offline_untracked":
            return (
                "当前为手动离线业务模式，不能在此恢复在线。"
                "请先退出游客模式，再使用账号登录。"
            )
        effective_sync_state = sync_state or self._last_sync_state
        if effective_sync_state == "reauth_required":
            return ""
        if state.is_online:
            return "当前账号的登录会话仍然有效，无需重新上线。"
        if effective_sync_state == "syncing":
            return self.RECONNECT_SYNC_BUSY_MESSAGE
        return ""

    def _show_reconnect_unavailable(self, reason):
        QMessageBox.information(
            self,
            "暂时无法重新上线",
            reason,
        )

    def _reconnect_online(self):
        unavailable_reason = self._reconnect_unavailable_reason(
            self._last_sync_state
        )
        if unavailable_reason:
            self._show_reconnect_unavailable(unavailable_reason)
            return
        state = self.session_manager.state
        coordinator_was_active = bool(
            self.sync_coordinator is not None
            and not bool(
                getattr(
                    self.sync_coordinator,
                    "is_stopped",
                    getattr(self.sync_coordinator, "_stopped", False),
                )
            )
        )
        if self.sync_coordinator is not None:
            pause = getattr(
                self.sync_coordinator,
                "pause_for_reauthentication",
                None,
            )
            try:
                if callable(pause):
                    paused = pause()
                else:
                    if getattr(self.sync_coordinator, "_running", False):
                        self._show_reconnect_unavailable(
                            self.RECONNECT_SYNC_BUSY_MESSAGE
                        )
                        return
                    stop = getattr(self.sync_coordinator, "stop", None)
                    if not callable(stop):
                        self._show_reconnect_unavailable(
                            "同步组件当前无法暂停，请稍后再试；若持续出现，"
                            "请返回登录页后重新登录。"
                        )
                        return
                    stop()
                    paused = True
            except Exception as exc:  # noqa: BLE001 - Qt slots must show failures
                self._show_reconnect_unavailable(
                    f"同步组件暂时无法暂停：{exc}"
                )
                return
            if not paused:
                self._show_reconnect_unavailable(
                    self.RECONNECT_SYNC_BUSY_MESSAGE
                )
                return
        dialog = ReconnectDialog(self.session_manager, self.account, self)
        if dialog.exec_() != dialog.Accepted or dialog.account is None:
            if coordinator_was_active and state.mode != "reauth_required":
                self.sync_coordinator.start()
            return

        account = dialog.account
        password = dialog.password
        if account.must_change_password:
            password_dialog = PasswordDialog(
                self.database,
                account.id,
                forced=True,
                parent=self,
                session_manager=self.session_manager,
                initial_current_password=password,
            )
            if password_dialog.exec_() != password_dialog.Accepted:
                self.session_manager.invalidate_credentials()
                if self.sync_coordinator is not None:
                    self.sync_coordinator.stop()
                    self._update_sync_status(
                        self.sync_coordinator.engine.status(
                            "reauth_required",
                            "必须先修改初始密码才能重新上线",
                        )
                    )
                return
            account = password_dialog.account or replace(
                account,
                must_change_password=False,
            )
            password = password_dialog.new_password or password

        self._apply_current_account(account)
        if self.credential_store is not None:
            try:
                remembered = self.credential_store.load()
                if (
                    remembered is not None
                    and remembered.username.casefold() == account.username.casefold()
                ):
                    self.credential_store.save(
                        account.username,
                        password,
                        auto_login=remembered.auto_login,
                    )
            except (OSError, RuntimeError, ValueError):
                pass
        if self.sync_coordinator is not None:
            self.sync_coordinator.resume_after_reauthentication()
        if self.captcha_learning_service is not None:
            self.captcha_learning_service.refresh_policy()
            self.captcha_learning_service.retry_pending()
        if self.announcement_service_available:
            self.refresh_announcements()
        QMessageBox.information(
            self,
            "已重新上线",
            "账号已恢复在线，未上传数据正在后台同步。",
        )

    def _apply_current_account(self, account):
        self.account = account
        self.business_metrics_enabled = (
            self._business_metrics_requested and account.statistics_enabled
        )
        if account.is_test:
            if self.workflow_timing is not None and self.workflow_timing.is_active:
                self.workflow_page._timing_finish_run(
                    "stopped",
                    "账号已切换为测试账号",
                )
            self.workflow_timing = None
            self.workflow_page._timing_service = None
            self.workflow_page._untracked_mode = True
            self.workflow_page._untracked_label = "测试账号"
        elif self._business_metrics_requested and self.workflow_timing is None:
            self.workflow_timing = WorkflowTimingService(
                self.database,
                account.id,
            )
            self.workflow_page._timing_service = self.workflow_timing
            self.workflow_page._untracked_mode = False
            self.workflow_page._untracked_label = "当前模式"
        for page in (self.dashboard_page, self.statistics_page):
            if page is not None:
                page.account = account
        if self.personal_center_page is not None:
            self.personal_center_page.account = account
            self.personal_center_page.identity_value.setText(account.role_label)
            self.personal_center_page.name_value.setText(account.name_label)
            self.personal_center_page.username_value.setText(account.username)
        if self.account_page is not None:
            self.account_page.current_account = account
        self.sidebar_user.setText(account.name_label)
        self.sidebar_user.setToolTip(account.name_label)
        self.sidebar_role.setText(f"{account.role_label}  ·  {account.username}")
        self.sidebar_avatar.setText(self._sidebar_avatar_text(account.name_label))
        self.setWindowTitle(f"{APP_NAME} - {account.name_label}")

    def _online_data_changed(self):
        refreshed_account = self.database.get_account(self.account.id)
        if refreshed_account is not None:
            self._apply_current_account(refreshed_account)
        self.dashboard_page.refresh()
        self.statistics_page.refresh()

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
        self.sidebar_avatar.setText(self._sidebar_avatar_text(self.account.name_label))
        self.setWindowTitle(f"{APP_NAME} - {self.account.name_label}")

    def _account_data_changed(self, *_args):
        self.dashboard_page.refresh()
        self.statistics_page.refresh()

    def show_page(self, key):
        data_view = self.DATA_CENTER_VIEWS.get(key)
        page_key = "statistics" if data_view else key
        if page_key not in self._pages:
            return
        if data_view:
            run_ui_with_loading(
                self,
                "正在加载统计数据…",
                lambda: self.statistics_page.set_navigation_view(data_view),
            )
            if not self.data_nav_toggle.isChecked():
                self.data_nav_toggle.setChecked(True)
                self._toggle_data_navigation(True)
        self.stack.setCurrentWidget(self._pages[page_key])
        # Settings is a compact page. Let it use its size hint so its primary
        # actions stay visible in a normal small window; data-heavy pages keep
        # the established canvas height and scrolling behavior.
        compact_page = page_key == "personal"
        self.stack.setMinimumWidth(
            0 if compact_page else self.PAGE_CANVAS_SIZE.width()
        )
        self.stack.setMinimumHeight(
            0 if compact_page else self.PAGE_CANVAS_SIZE.height()
        )
        for page_key, button in self._nav_buttons.items():
            button.setChecked(page_key == key)
        self._set_data_navigation_active(bool(data_view))
        if key == "home":
            run_ui_with_loading(
                self,
                "正在加载数据…",
                self.dashboard_page.refresh,
            )
        elif key == "accounts" and self.account_page:
            self.account_page.refresh()
        elif key == "announcements_admin" and self.announcement_admin_page:
            self.announcement_admin_page.refresh()
        elif key == "machine_learning" and self.machine_learning_page:
            self.machine_learning_page.refresh()

    def _record_activity(self, metric_key, amount=1, source=""):
        if not self.business_metrics_enabled:
            return
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
            QMessageBox.warning(
                self, "统计记录失败", f"业务结果已产生，但统计写入失败：\n{exc}"
            )

    def _record_workflow_summary(self, counts, source="", details=None, task_id=None):
        if not self.business_metrics_enabled:
            return True
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
            QMessageBox.warning(
                self, "统计记录失败", f"完整流程已完成，但统计写入失败：\n{exc}"
            )
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
            if self.credential_store is not None and dialog.new_password:
                try:
                    self.credential_store.update_password(
                        self.account.username,
                        dialog.new_password,
                    )
                except (OSError, RuntimeError, ValueError):
                    pass

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
        if self.machine_learning_page is not None:
            self.machine_learning_page.shutdown()
        if self.captcha_learning_service is not None:
            self.captcha_learning_service.stop()
        self._prepared_to_close = True
        return True

    def _request_logout(self):
        reply = QMessageBox.question(
            self,
            "退出游客模式" if self.offline_business_mode else "退出登录",
            (
                "确定退出游客模式吗？正在执行的任务将被停止。"
                if self.offline_business_mode
                else "确定退出当前账号吗？正在执行的任务将被停止。"
            ),
            QMessageBox.Yes | QMessageBox.No,
        )
        if reply != QMessageBox.Yes or not self._prepare_close():
            return
        self.logout_requested.emit()

    def closeEvent(self, event):
        if self._update_busy:
            reply = QMessageBox.question(
                self,
                "停止更新并退出",
                "更新包仍在下载。是否停止下载并退出程序？",
                QMessageBox.Yes | QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                event.ignore()
                return
            self._cancel_update_download()
            if self.update_dialog is not None:
                self.update_dialog.allow_close()
        if (
            self.update_dialog is not None
            and self.update_dialog.mandatory
            and self.update_dialog.isVisible()
            and not self._prepared_to_close
        ):
            event.ignore()
            self.update_dialog.raise_()
            self.update_dialog.activateWindow()
            return
        if not self._prepare_close():
            event.ignore()
            return
        if self._announcement_poll_timer is not None:
            self._announcement_poll_timer.stop()
        if self._announcement_rotation_timer is not None:
            self._announcement_rotation_timer.stop()
        if self.contact_history_dialog is not None:
            self.contact_history_dialog.close()
            self.contact_history_dialog = None
        for dialog in list(self.contact_conversation_dialogs.values()):
            dialog.close()
        self.contact_conversation_dialogs.clear()
        if self.announcement_admin_page is not None:
            self.announcement_admin_page.close_chat_windows()
        if self.captcha_learning_service is not None:
            self.captcha_learning_service.stop()
        event.accept()
        self.window_closed.emit()

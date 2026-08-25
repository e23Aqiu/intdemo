from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Account:
    id: int
    username: str
    display_name: str
    role: str
    is_active: bool
    created_at: str
    last_login: Optional[str]
    must_change_password: bool = False
    server_account_id: Optional[str] = None
    stats_scope: str = "own"
    is_archived: bool = False
    entitlement_revision: int = 0
    # Keep role="user" for compatibility with the v1.1.0 client.
    is_test: bool = False
    # v1.2 hierarchy fields are additive so older local databases and offline
    # accounts can still be materialized with their legacy defaults.
    account_type: str = ""
    data_scope: str = ""
    road_id: Optional[str] = None
    road_name: Optional[str] = None

    @property
    def resolved_account_type(self) -> str:
        if self.role == "admin":
            return "admin"
        if self.account_type in {"admin", "road_admin", "station"}:
            return self.account_type
        return "station"

    @property
    def effective_data_scope(self) -> str:
        if self.resolved_account_type == "admin":
            return "all"
        if self.data_scope in {"own", "road", "all"}:
            return self.data_scope
        return "all" if self.stats_scope == "all" else "own"

    @property
    def is_admin(self) -> bool:
        return self.resolved_account_type == "admin"

    @property
    def is_road_admin(self) -> bool:
        return self.resolved_account_type == "road_admin"

    @property
    def is_station(self) -> bool:
        return self.resolved_account_type == "station"

    @property
    def is_account_manager(self) -> bool:
        return self.resolved_account_type in {"admin", "road_admin"}

    @property
    def role_label(self) -> str:
        if self.is_test:
            return "测试账号"
        if self.is_admin:
            return "管理员"
        return "路段管理员" if self.is_road_admin else "中心站账号"

    @property
    def name_label(self) -> str:
        return self.display_name or self.username

    @property
    def can_view_all_stats(self) -> bool:
        return self.is_admin or self.effective_data_scope == "all"

    @property
    def can_view_shared_stats(self) -> bool:
        return self.is_admin or self.effective_data_scope in {"road", "all"}

    @property
    def statistics_enabled(self) -> bool:
        return not self.is_test and not self.is_road_admin

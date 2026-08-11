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

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def role_label(self) -> str:
        return "管理员" if self.is_admin else "用户"

    @property
    def name_label(self) -> str:
        return self.display_name or self.username

    @property
    def can_view_all_stats(self) -> bool:
        return self.is_admin or self.stats_scope == "all"

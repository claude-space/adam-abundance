"""Role-based access control (PRD §9, §9.1).

Google (or dev) identity authenticates; this maps it to a role that gates who
may approve/dispatch which brand. Resource access still uses service accounts —
roles govern *human approvals*, not tool credentials.

Roles (mirrors the existing Seona role model):
  * ``global_admin``    — everything, incl. managing users
  * ``portfolio_admin`` — approve/dispatch any brand; no user management
  * ``brand_user``      — approve/dispatch only their assigned brands
  * ``viewer``          — read-only; cannot approve or dispatch
"""

from __future__ import annotations

from enum import Enum


class Role(str, Enum):
    GLOBAL_ADMIN = "global_admin"
    PORTFOLIO_ADMIN = "portfolio_admin"
    BRAND_USER = "brand_user"
    VIEWER = "viewer"


ROLE_ORDER = [Role.GLOBAL_ADMIN, Role.PORTFOLIO_ADMIN, Role.BRAND_USER, Role.VIEWER]
ROLE_LABELS = {
    Role.GLOBAL_ADMIN.value: "Global admin",
    Role.PORTFOLIO_ADMIN.value: "Portfolio admin",
    Role.BRAND_USER.value: "Brand user",
    Role.VIEWER.value: "Viewer",
}


def is_valid_role(role: str) -> bool:
    return role in {r.value for r in Role}


def can_manage_users(role: str) -> bool:
    return role == Role.GLOBAL_ADMIN.value


def can_approve(role: str, brands: list[str] | None, target_brand: str) -> bool:
    """Whether ``role`` (with brand scope ``brands``) may approve/dispatch work
    for ``target_brand``. Portfolio-scoped items (``portfolio``) require an admin."""
    if role in (Role.GLOBAL_ADMIN.value, Role.PORTFOLIO_ADMIN.value):
        return True
    if role == Role.BRAND_USER.value:
        return target_brand in (brands or [])
    return False  # viewer


def can_dispatch(role: str, brands: list[str] | None, target_brand: str) -> bool:
    return can_approve(role, brands, target_brand)

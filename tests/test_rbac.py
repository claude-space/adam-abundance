"""RBAC permission logic (PRD §9.1). Dependency-free."""

from switchboard.rbac import Role, can_approve, can_manage_users, is_valid_role


def test_admins_can_approve_any_brand():
    for role in (Role.GLOBAL_ADMIN.value, Role.PORTFOLIO_ADMIN.value):
        assert can_approve(role, None, "hotcars") is True
        assert can_approve(role, None, "portfolio") is True


def test_brand_user_scoped_to_assigned_brands():
    assert can_approve(Role.BRAND_USER.value, ["hotcars"], "hotcars") is True
    assert can_approve(Role.BRAND_USER.value, ["hotcars"], "carbuzz") is False
    assert can_approve(Role.BRAND_USER.value, [], "hotcars") is False
    # portfolio scope is admin-only:
    assert can_approve(Role.BRAND_USER.value, ["hotcars"], "portfolio") is False


def test_viewer_cannot_approve():
    assert can_approve(Role.VIEWER.value, ["hotcars"], "hotcars") is False


def test_manage_users_is_global_admin_only():
    assert can_manage_users(Role.GLOBAL_ADMIN.value) is True
    for role in (Role.PORTFOLIO_ADMIN.value, Role.BRAND_USER.value, Role.VIEWER.value):
        assert can_manage_users(role) is False


def test_role_validation():
    assert is_valid_role("global_admin") and not is_valid_role("superuser")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")

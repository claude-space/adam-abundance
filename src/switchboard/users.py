"""User/role persistence for RBAC (PRD §9.1).

Users are auto-provisioned on first login: the very first user (or anyone in the
``AUTH_ADMINS`` config) becomes ``global_admin`` so the system is bootstrappable;
everyone else gets the configured default role. A ``global_admin`` can change
roles from the /users view.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import get_settings
from .db.models import AppUser
from .logging_ import get_logger
from .rbac import Role, is_valid_role

log = get_logger("users")


class UserRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.settings = get_settings()

    async def get(self, email: str) -> AppUser | None:
        return await self.session.get(AppUser, email.lower())

    async def list(self) -> list[AppUser]:
        rows = await self.session.execute(select(AppUser).order_by(AppUser.created_at))
        return list(rows.scalars().all())

    async def _count(self) -> int:
        return int((await self.session.execute(select(func.count()).select_from(AppUser))).scalar_one())

    async def provision(self, email: str, name: str | None = None) -> AppUser:
        """Get-or-create the user, assigning a bootstrap/default role on creation."""
        email = email.lower()
        existing = await self.get(email)
        if existing:
            return existing
        first_user = await self._count() == 0
        admins = {a.lower() for a in self.settings.auth.admins}
        role = Role.GLOBAL_ADMIN.value if (first_user or email in admins) else self.settings.auth.default_role
        if not is_valid_role(role):
            role = Role.VIEWER.value
        user = AppUser(email=email, name=name, role=role, brands=None)
        self.session.add(user)
        await self.session.flush()
        log.info("Provisioned user %s as %s%s", email, role, " (bootstrap admin)" if first_user else "")
        return user

    async def set_role(self, email: str, role: str, brands: list[str] | None = None) -> AppUser:
        if not is_valid_role(role):
            raise ValueError(f"invalid role '{role}'")
        user = await self.get(email)
        if user is None:
            raise ValueError(f"unknown user '{email}'")
        user.role = role
        user.brands = brands if role == Role.BRAND_USER.value else None
        user.updated_at = datetime.now(timezone.utc)
        await self.session.flush()
        log.info("Set role for %s -> %s brands=%s", email, role, user.brands)
        return user

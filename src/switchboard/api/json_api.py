"""JSON API (``/api/*``) for the React/TanStack frontend (story-unraveler-tool).

Same-origin by design: the SPA is served from — or reverse-proxied alongside —
this app, so these endpoints reuse the existing Google-SSO **session cookie**
(no CORS, no tokens). Each returns the SAME real data the server-rendered pages
compute, via shared gatherers in :mod:`routes`, so the HTML and JSON surfaces
never drift. ``require_user`` returns HTTP 401 JSON when unauthenticated.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from ..context import RunContext
from . import routes
from .auth import require_user

router = APIRouter(prefix="/api", tags=["api"])


@router.get("/me")
async def me(request: Request) -> dict[str, Any]:
    """The signed-in user — lets the SPA render identity + gate admin controls."""
    u = require_user(request)
    return {"email": u.get("email"), "name": u.get("name"), "role": u.get("role"),
            "brands": u.get("brands") or []}


@router.get("/writers")
async def writers(request: Request, brand: str | None = None) -> dict[str, Any]:
    """Writer-emulation data (§16.3): per-brand top-writer leaderboard + versioned
    style profiles — the real backing for the SPA's Writer Emulation screen."""
    u = require_user(request)
    async with RunContext.open() as ctx:
        data = await routes.gather_writer_emulation(ctx, brand)
    data["may_edit"] = u.get("role") in ("global_admin", "portfolio_admin")
    return data

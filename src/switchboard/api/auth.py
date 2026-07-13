"""Authentication & identity (PRD §9.1): Google OAuth / OIDC.

Login requests only ``openid email profile`` — no Gmail/Sheets/BigQuery scopes.
Access is restricted to the Valnet Workspace domain(s) (``hd`` verified
**server-side**) plus an explicit allowlist. The Google identity is used only for
authentication + attribution (it populates ``approved_by``); resource access
always uses service-account credentials from the credentials layer, never the
user's token.

A ``/auth/dev-login`` path exists **only** when ``SWITCHBOARD_ENV=local`` so the
approval surface is usable before OAuth is wired; it still enforces the domain +
allowlist check.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.status import HTTP_302_FOUND

from ..config import get_settings
from ..logging_ import get_logger

log = get_logger("api.auth")
router = APIRouter(prefix="/auth", tags=["auth"])

_oauth = None


def _get_oauth():
    """Lazily build the authlib OAuth client from credentials (or None)."""
    global _oauth
    if _oauth is not None:
        return _oauth
    settings = get_settings()
    client_id, client_secret, _redirect = settings.creds.google_oauth_client()
    if not (client_id and client_secret):
        return None
    try:
        from authlib.integrations.starlette_client import OAuth  # type: ignore
    except ImportError:  # pragma: no cover
        log.warning("authlib not installed; Google OAuth unavailable")
        return None
    oauth = OAuth()
    oauth.register(
        name="google",
        client_id=client_id,
        client_secret=client_secret,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )
    _oauth = oauth
    return _oauth


def current_user(request: Request) -> dict[str, Any] | None:
    return request.session.get("user")


async def _establish_session(request: Request, email: str, name: str, hd: str | None, dev: bool = False) -> None:
    """Provision the user (assigning a role) and store identity + role in session."""
    from ..context import RunContext
    from ..users import UserRepo

    role, brands = "viewer", None
    try:
        async with RunContext.open() as ctx:
            u = await UserRepo(ctx.session).provision(email, name)
            role, brands = u.role, u.brands
    except Exception as exc:  # noqa: BLE001 — DB down shouldn't block login entirely
        log.warning("Could not provision %s (%s); defaulting to viewer", email, exc)
    request.session["user"] = {"email": email, "name": name, "hd": hd, "dev": dev,
                               "role": role, "brands": brands or []}
    log.info("Session established: %s role=%s", email, role)


def require_user(request: Request) -> dict[str, Any]:
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Login required")
    return user


@router.get("/login")
async def login(request: Request):
    oauth = _get_oauth()
    settings = get_settings()
    if oauth is None:
        # OAuth not configured — offer dev login in local env.
        if settings.env == "local":
            return RedirectResponse("/auth/dev-login", status_code=HTTP_302_FOUND)
        raise HTTPException(status_code=503, detail="Google OAuth is not configured")
    redirect_uri = settings.auth.redirect_uri
    return await oauth.google.authorize_redirect(request, redirect_uri)


@router.get("/callback")
async def callback(request: Request):
    oauth = _get_oauth()
    if oauth is None:
        raise HTTPException(status_code=503, detail="Google OAuth is not configured")
    settings = get_settings()
    token = await oauth.google.authorize_access_token(request)
    info = token.get("userinfo") or {}
    email = (info.get("email") or "").lower()
    hd = info.get("hd")
    if not info.get("email_verified", True) or not settings.auth.is_allowed(email, hd):
        log.warning("Denied login for %s (hd=%s)", email, hd)
        raise HTTPException(status_code=403, detail="Not authorized for Switchboard")
    await _establish_session(request, email, info.get("name", email), hd)
    return RedirectResponse("/", status_code=HTTP_302_FOUND)


@router.get("/dev-login", response_class=HTMLResponse)
async def dev_login_form(request: Request):
    settings = get_settings()
    if settings.env != "local":
        raise HTTPException(status_code=404, detail="Not found")
    allow = settings.auth.allowlist[0] if settings.auth.allowlist else f"you@{settings.auth.allowed_domains[0]}"
    return f"""
    <h2>Switchboard dev login (local only)</h2>
    <p>OAuth isn't configured. Enter a Workspace email on the allowlist/domain.</p>
    <form method="post" action="/auth/dev-login">
      <input name="email" value="{allow}" style="width:320px" />
      <button type="submit">Sign in</button>
    </form>
    """


@router.post("/dev-login")
async def dev_login(request: Request):
    settings = get_settings()
    if settings.env != "local":
        raise HTTPException(status_code=404, detail="Not found")
    form = await request.form()
    email = str(form.get("email", "")).lower().strip()
    if not settings.auth.is_allowed(email, settings.auth.allowed_domains[0] if settings.auth.allowed_domains else None):
        raise HTTPException(status_code=403, detail="Email not on allowlist/domain")
    await _establish_session(request, email, email, email.split("@")[-1], dev=True)
    return RedirectResponse("/", status_code=HTTP_302_FOUND)


@router.get("/logout")
async def logout(request: Request):
    request.session.pop("user", None)
    return RedirectResponse("/", status_code=HTTP_302_FOUND)

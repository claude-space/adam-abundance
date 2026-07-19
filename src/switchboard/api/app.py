"""FastAPI application factory: session middleware, auth, the approval surface,
and observability. Import path for uvicorn: ``switchboard.api.app:app``."""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import Response

from ..config import get_settings
from ..logging_ import get_logger, setup_logging
from . import auth, json_api, routes

log = get_logger("api")


def _link_re(base_path: str) -> re.Pattern[str]:
    """Match root-absolute href/src/action values that need the mount prefix.
    Skips protocol-relative ``//`` and values already carrying ``base_path`` so
    the rewrite is idempotent."""
    seg = re.escape(base_path.lstrip("/"))
    return re.compile(rf'''(href|src|action)=(["'])/(?!/)(?!{seg}(?:/|["']))''')


def create_app() -> FastAPI:
    setup_logging()
    settings = get_settings()
    # root_path lets Starlette know the external mount prefix (Caddy /agents/<slug>/).
    app = FastAPI(title="Switchboard", version="0.1.0", root_path=settings.base_path or "")

    # When mounted under a Caddy subpath (APP_BASE_PATH), rewrite server-issued
    # redirect Location headers AND root-absolute links in rendered HTML so both
    # land under the prefix. No-op when base_path is empty (local / root mount),
    # which keeps the templates free of base-path knowledge.
    base_path = settings.base_path
    if base_path:
        link_re = _link_re(base_path)

        @app.middleware("http")
        async def _basepath(request, call_next):
            response = await call_next(request)
            # 1) redirect Location headers (login, post-action 302s)
            loc = response.headers.get("location")
            if loc and loc.startswith("/") and not loc.startswith("//") \
                    and not loc.startswith(base_path + "/") and loc != base_path:
                response.headers["location"] = base_path + loc
            # 2) root-absolute links in HTML bodies
            if response.headers.get("content-type", "").startswith("text/html"):
                body = b""
                async for chunk in response.body_iterator:
                    body += chunk if isinstance(chunk, bytes) else str(chunk).encode()
                text = link_re.sub(rf'\1=\2{base_path}/', body.decode("utf-8"))
                new_body = text.encode("utf-8")
                raw = [(k, v) for (k, v) in response.raw_headers
                       if k.lower() != b"content-length"]
                rewritten = Response(content=new_body, status_code=response.status_code)
                rewritten.raw_headers = raw + [(b"content-length", str(len(new_body)).encode())]
                return rewritten
            return response

    app.add_middleware(SessionMiddleware, secret_key=settings.creds.session_secret(), https_only=False)
    app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
    app.include_router(auth.router)
    app.include_router(routes.router)
    app.include_router(json_api.router)

    @app.get("/healthz")
    async def healthz() -> JSONResponse:  # no auth
        return JSONResponse({"status": "ok", "env": settings.env,
                             "kill_switch": settings.kill_switch})

    log.info("Switchboard API ready (env=%s)", settings.env)
    return app


app = create_app()

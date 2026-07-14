"""HC Viral Hits client. Reads use the machine-facing API-key surface
(``/api/cms/*``, header ``X-API-Key``); the ideate/poll/emaki triggers are
session-authed and are exercised only by the (Phase-4) action adapter."""

from __future__ import annotations

from typing import Any

from ...logging_ import get_logger
from ..base import AdapterUnavailable

log = get_logger("client.hcviral")


class HCViralClient:
    def __init__(self, base_url: str, api_key: str | None) -> None:
        self._base = base_url.rstrip("/")
        self._api_key = api_key

    def _headers(self) -> dict[str, str]:
        if not self._api_key:
            raise AdapterUnavailable("HC_VIRAL_HITS_API_KEY not configured")
        return {"X-API-Key": self._api_key, "Accept": "application/json"}

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        try:
            import httpx  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise AdapterUnavailable("httpx not installed") from exc
        # ShellAgent 308-redirects API paths to add a trailing slash; follow it
        # (same-origin, so the X-API-Key header is preserved).
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            resp = await client.get(f"{self._base}{path}", params=params, headers=self._headers())
            resp.raise_for_status()
            return resp.json()

    async def list_drafts(self, brand: str, status: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"brand": brand}
        if status:
            params["status"] = status
        data = await self._get("/api/cms/drafts", params)
        return data if isinstance(data, list) else data.get("drafts", [])

    async def list_topics(self, brand: str) -> list[dict[str, Any]]:
        """Every topic HC-Viral is tracking for a brand (any status), used for the
        cross-monitor corroboration check. Targets the machine topics surface
        (`/api/cms/topics`); callers should fall back to ``list_drafts`` when it
        isn't exposed yet (this raises on 404)."""
        data = await self._get("/api/cms/topics", {"brand": brand})
        return data if isinstance(data, list) else data.get("topics", data.get("drafts", []))

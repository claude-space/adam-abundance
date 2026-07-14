"""Firecrawl client — web search + page extraction (markdown) for trend
sourcing and dossier building (docs/trend-pipeline.md)."""

from __future__ import annotations

from typing import Any

from ...logging_ import get_logger
from ..base import AdapterUnavailable

log = get_logger("client.firecrawl")

_BASE = "https://api.firecrawl.dev"


class FirecrawlClient:
    def __init__(self, api_key: str | None) -> None:
        if not api_key:
            raise AdapterUnavailable("FIRECRAWL_API_KEY not configured")
        self._api_key = api_key

    async def _post(self, path: str, payload: dict[str, Any], *, timeout: float = 60.0) -> Any:
        try:
            import httpx  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise AdapterUnavailable("httpx not installed") from exc
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{_BASE}{path}", json=payload,
                headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
            )
            resp.raise_for_status()
            return resp.json()

    async def search(self, query: str, *, limit: int = 8) -> list[dict[str, Any]]:
        """Web search: [{title, url, source, snippet}]."""
        data = await self._post("/v1/search", {"query": query, "limit": limit})
        out: list[dict[str, Any]] = []
        for r in (data.get("data") or []) if isinstance(data, dict) else []:
            url = r.get("url", "")
            out.append({
                "title": r.get("title", ""), "url": url,
                "source": url.split("//", 1)[-1].split("/", 1)[0].removeprefix("www."),
                "snippet": (r.get("description") or r.get("markdown") or "")[:400],
            })
        return out

    async def scrape(self, url: str) -> dict[str, Any]:
        """Extract one page as markdown: {url, title, markdown}."""
        data = await self._post("/v1/scrape", {"url": url, "formats": ["markdown"]}, timeout=90.0)
        body = data.get("data") if isinstance(data, dict) else None
        body = body if isinstance(body, dict) else {}
        meta = body.get("metadata") if isinstance(body.get("metadata"), dict) else {}
        return {
            "url": url,
            "title": meta.get("title", ""),
            "markdown": (body.get("markdown") or "")[:12000],
        }

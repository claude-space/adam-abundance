"""NewsAPI.org client — headline sourcing for the trend scout
(docs/trend-pipeline.md). Free-tier friendly: one 'everything' query per call."""

from __future__ import annotations

from typing import Any

from ...logging_ import get_logger
from ..base import AdapterUnavailable

log = get_logger("client.newsapi")

_BASE = "https://newsapi.org"


class NewsApiClient:
    def __init__(self, api_key: str | None) -> None:
        if not api_key:
            raise AdapterUnavailable("NEWSAPI_API_KEY not configured")
        self._api_key = api_key

    async def everything(self, query: str, *, page_size: int = 25) -> list[dict[str, Any]]:
        """Recent articles matching the query: [{title, url, source, published_at, snippet}]."""
        try:
            import httpx  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise AdapterUnavailable("httpx not installed") from exc
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(
                f"{_BASE}/v2/everything",
                params={"q": query, "sortBy": "publishedAt", "pageSize": page_size, "language": "en"},
                headers={"X-Api-Key": self._api_key},
            )
            resp.raise_for_status()
            data = resp.json()
        out: list[dict[str, Any]] = []
        for a in data.get("articles", []) or []:
            src = a.get("source") or {}
            out.append({
                "title": a.get("title", ""),
                "url": a.get("url", ""),
                "source": (src.get("name") if isinstance(src, dict) else "") or "",
                "published_at": a.get("publishedAt") or "",
                "snippet": (a.get("description") or "")[:400],
            })
        return out

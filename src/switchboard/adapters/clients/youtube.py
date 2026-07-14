"""YouTube Data API v3 client — recent automotive videos as trend signals
(docs/trend-pipeline.md). Search costs 100 quota units/call; the scout calls it
once per scan. Soft-fails (AdapterUnavailable) when the key is missing."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from ...logging_ import get_logger
from ..base import AdapterUnavailable

log = get_logger("client.youtube")

_BASE = "https://www.googleapis.com/youtube/v3"


class YouTubeClient:
    def __init__(self, api_key: str | None) -> None:
        if not api_key:
            raise AdapterUnavailable("YOUTUBE_API_KEY not configured")
        self._key = api_key

    async def search_recent(self, query: str, *, days: int = 2, max_results: int = 10) -> list[dict[str, Any]]:
        """Recent videos matching the query: [{title, url, source, published_at, snippet}]."""
        try:
            import httpx  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise AdapterUnavailable("httpx not installed") from exc
        after = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(f"{_BASE}/search", params={
                "part": "snippet", "q": query, "type": "video", "order": "date",
                "maxResults": max_results, "relevanceLanguage": "en",
                "publishedAfter": after, "key": self._key,
            })
            resp.raise_for_status()
            data = resp.json()
        out: list[dict[str, Any]] = []
        for it in data.get("items", []) or []:
            sn = it.get("snippet") or {}
            vid = (it.get("id") or {}).get("videoId")
            if not vid:
                continue
            out.append({
                "title": sn.get("title", ""),
                "url": f"https://www.youtube.com/watch?v={vid}",
                "source": sn.get("channelTitle", "YouTube"),
                "published_at": sn.get("publishedAt", ""),
                "snippet": (sn.get("description") or "")[:400],
            })
        return out

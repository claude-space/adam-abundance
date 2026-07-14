"""X (Twitter) API v2 recent-search client — recent posts as trend signals
(docs/trend-pipeline.md). Uses the app bearer token. Recent search requires a
paid tier; if the token lacks access the call soft-fails and the scan degrades."""

from __future__ import annotations

from typing import Any

from ...logging_ import get_logger
from ..base import AdapterUnavailable

log = get_logger("client.x")

_BASE = "https://api.x.com/2"


class XClient:
    def __init__(self, bearer_token: str | None) -> None:
        if not bearer_token:
            raise AdapterUnavailable("X_BEARER_TOKEN not configured")
        self._bearer = bearer_token

    async def search_recent(self, query: str, *, max_results: int = 10) -> list[dict[str, Any]]:
        """Recent posts matching the query: [{title, url, source, published_at, snippet}]."""
        try:
            import httpx  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise AdapterUnavailable("httpx not installed") from exc
        q = f"{query} -is:retweet -is:reply lang:en"
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(
                f"{_BASE}/tweets/search/recent",
                headers={"Authorization": f"Bearer {self._bearer}"},
                params={"query": q, "max_results": max(10, min(max_results, 100)),
                        "tweet.fields": "created_at,public_metrics"},
            )
            resp.raise_for_status()
            data = resp.json()
        out: list[dict[str, Any]] = []
        for t in data.get("data", []) or []:
            text = (t.get("text") or "").replace("\n", " ")
            out.append({
                "title": text[:120],
                "url": f"https://x.com/i/web/status/{t.get('id')}",
                "source": "X",
                "published_at": t.get("created_at", ""),
                "snippet": text[:400],
            })
        return out

"""Tavily search client (trend sourcing — docs/trend-pipeline.md).

News-mode search for competitor/vertical trend signals plus advanced search for
dossier building. Thin and tolerant: any response-shape drift degrades to fewer
fields, never a crash upstream (the adapter layer soft-fails on exceptions).
"""

from __future__ import annotations

from typing import Any

from ...logging_ import get_logger
from ..base import AdapterUnavailable

log = get_logger("client.tavily")

_BASE = "https://api.tavily.com"


class TavilyClient:
    def __init__(self, api_key: str | None) -> None:
        if not api_key:
            raise AdapterUnavailable("TAVILY_API_KEY not configured")
        self._api_key = api_key

    async def _post(self, path: str, payload: dict[str, Any]) -> Any:
        try:
            import httpx  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise AdapterUnavailable("httpx not installed") from exc
        headers = {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(f"{_BASE}{path}", json=payload, headers=headers)
            resp.raise_for_status()
            return resp.json()

    async def search_news(self, query: str, *, days: int = 2, max_results: int = 10) -> list[dict[str, Any]]:
        """Recent news results: [{title, url, source, published_at, snippet, relevance}]."""
        data = await self._post("/search", {
            "query": query, "topic": "news", "days": days,
            "max_results": max_results, "search_depth": "basic",
        })
        out: list[dict[str, Any]] = []
        for r in data.get("results", []) or []:
            out.append({
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "source": _domain(r.get("url", "")),
                "published_at": r.get("published_date") or "",
                "snippet": (r.get("content") or "")[:400],
                "relevance": r.get("score"),
            })
        return out

    async def deep_search(self, query: str, *, max_results: int = 5) -> dict[str, Any]:
        """Advanced search with raw content + a synthesized answer, for the dossier."""
        data = await self._post("/search", {
            "query": query, "search_depth": "advanced", "max_results": max_results,
            "include_answer": True, "include_raw_content": True,
        })
        results = []
        for r in data.get("results", []) or []:
            results.append({
                "title": r.get("title", ""), "url": r.get("url", ""),
                "content": (r.get("raw_content") or r.get("content") or "")[:6000],
            })
        return {"answer": data.get("answer") or "", "results": results}


def _domain(url: str) -> str:
    try:
        return url.split("//", 1)[-1].split("/", 1)[0].removeprefix("www.")
    except Exception:  # noqa: BLE001
        return ""

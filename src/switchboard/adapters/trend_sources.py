"""Trend-source read adapters (docs/trend-pipeline.md) — Tavily, Perplexity,
Firecrawl, NewsAPI. Owned by **research** (its outside-in news domain, PRD §6.2).

These are portfolio-scoped: during the per-brand morning observe pass they
return nothing (so a cycle never triples the API spend); the trend-scan feeder
invokes them once with ``brand='portfolio'``. Each emits one ``context`` entry
``kind='trend_signals'`` whose normalized items the TrendDetector clusters
together with the existing ``competitor_coverage`` RSS entries.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..db.enums import PORTFOLIO, EntryType
from ..interfaces import CostSpec, EntryDraft
from ..logging_ import get_logger
from .base import BaseAdapter
from .clients.firecrawl import FirecrawlClient
from .clients.newsapi import NewsApiClient
from .clients.perplexity import PerplexityClient
from .clients.tavily import TavilyClient

log = get_logger("adapter.trend_sources")

_SIGNAL_TTL = 24 * 3600
_MAX_ITEMS = 40


def _signals_draft(origin: str, items: list[dict[str, Any]]) -> EntryDraft:
    return EntryDraft(
        type=EntryType.CONTEXT, brand=PORTFOLIO, source_agent="research", source_system=origin,
        payload={
            "kind": "trend_signals", "origin": origin,
            "collected_at": datetime.now(timezone.utc).isoformat(),
            "item_count": len(items), "items": items[:_MAX_ITEMS],
        },
        ttl_seconds=_SIGNAL_TTL,
    )


def _norm(origin: str, item: dict[str, Any]) -> dict[str, Any]:
    return {
        "origin": origin,
        "source": item.get("source") or "",
        "title": item.get("title") or "",
        "url": item.get("url") or "",
        "published_at": item.get("published_at") or item.get("date") or "",
        "snippet": item.get("snippet") or "",
    }


class _TrendSourceAdapter(BaseAdapter):
    """Shared shape: portfolio-scoped, one trend_signals entry per pull."""

    owner_agent = "research"

    def _query(self) -> str:
        cfg = self.ctx.settings.trends
        terms = " OR ".join(cfg.watchlist[:6])
        return f"{cfg.base_query} {terms}".strip() if terms else cfg.base_query

    async def _observe(self, brand: str, **kwargs: Any) -> tuple[list[EntryDraft], CostSpec]:
        if brand != PORTFOLIO:  # the scout runs these portfolio-wide (see module docstring)
            return [], CostSpec()
        items = await self._pull()
        if not items:
            return [], CostSpec()
        return [_signals_draft(self.source_system, items)], CostSpec()

    async def _pull(self) -> list[dict[str, Any]]:  # pragma: no cover — overridden
        raise NotImplementedError


class TavilyTrendAdapter(_TrendSourceAdapter):
    name = "tavily_trends"
    source_system = "tavily"

    async def _pull(self) -> list[dict[str, Any]]:
        client = TavilyClient(self.ctx.creds.tavily_key())
        results = await client.search_news(self._query(), days=2, max_results=15)
        return [_norm("tavily", r) for r in results]


class NewsApiTrendAdapter(_TrendSourceAdapter):
    name = "newsapi_trends"
    source_system = "newsapi"

    async def _pull(self) -> list[dict[str, Any]]:
        client = NewsApiClient(self.ctx.creds.newsapi_key())
        results = await client.everything(self._query(), page_size=25)
        return [_norm("newsapi", r) for r in results]


class FirecrawlTrendAdapter(_TrendSourceAdapter):
    name = "firecrawl_trends"
    source_system = "firecrawl"

    async def _pull(self) -> list[dict[str, Any]]:
        client = FirecrawlClient(self.ctx.creds.firecrawl_key())
        results = await client.search(f"{self._query()} this week", limit=10)
        return [_norm("firecrawl", r) for r in results]


class PerplexityTrendAdapter(_TrendSourceAdapter):
    name = "perplexity_trends"
    source_system = "perplexity"

    async def _observe(self, brand: str, **kwargs: Any) -> tuple[list[EntryDraft], CostSpec]:
        if brand != PORTFOLIO:
            return [], CostSpec()
        client = PerplexityClient(self.ctx.creds.perplexity_key())
        result = await client.ask(
            f"What are the biggest {self._query()} stories from the last 24 hours? "
            "List each story on its own line.",
            system="You are a news wire. Be terse and factual.",
            max_tokens=700,
        )
        items = [_norm("perplexity", r) for r in result.get("search_results", [])]
        # Fall back to bare citations when no structured results came back.
        if not items:
            items = [
                {"origin": "perplexity", "source": "", "title": "", "url": u,
                 "published_at": "", "snippet": ""}
                for u in result.get("citations", [])
            ]
        items = [i for i in items if i["url"]]
        # Perplexity is a paid LLM call — meter it like other LLM spend.
        cost = CostSpec(llm_micros=int(result.get("micros", 0) or 0))
        if not items:
            return [], cost
        return [_signals_draft(self.source_system, items)], cost

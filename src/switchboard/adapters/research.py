"""Research-domain read adapters (PRD §6.2): outside-in context — Similarweb
competitor traffic and competitor news/RSS. Web-search verification (the
fact-gate) is a Phase-2 agent capability, not a read adapter.

Bing Search/Webmaster is intentionally absent — no account is wired in any repo
today (PRD §13.1/§13.2); add when confirmed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..db.enums import EntryType
from ..interfaces import CostSpec, EntryDraft
from ..logging_ import get_logger
from .base import AdapterUnavailable, BaseAdapter
from .clients.similarweb import SimilarwebClient

log = get_logger("adapter.research")

# Compact competitor set (from daily-reporting-agent's active SOURCES).
_COMPETITOR_FEEDS = {
    "Car and Driver": "https://www.caranddriver.com/rss/all.xml/",
    "Motor1": "https://www.motor1.com/rss/news/all/",
    "Carscoops": "https://www.carscoops.com/feed/",
    "The Drive": "https://www.thedrive.com/feed",
    "InsideEVs": "https://insideevs.com/rss/news/all/",
}


class SimilarwebAdapter(BaseAdapter):
    name = "similarweb"
    source_system = "similarweb"
    owner_agent = "research"

    async def _observe(self, brand: str, **kwargs: Any) -> tuple[list[EntryDraft], CostSpec]:
        client = SimilarwebClient(self.ctx.creds.similarweb_key())
        if brand == "portfolio":
            raise AdapterUnavailable("similarweb adapter is brand-scoped")
        bc = self.ctx.settings.brand(brand)
        rng = await client.available_range(bc.domain)
        end_month = (rng.get("end_date") or "")[:7]
        payload: dict[str, Any] = {"kind": "similarweb_range", "domain": bc.domain,
                                   "fresh_data": rng.get("fresh_data"), "end_month": end_month}
        if end_month:
            visits = await client.visits(bc.domain, end_month, end_month)
            payload["recent_daily_visits"] = visits[-7:]
        return [EntryDraft(type=EntryType.METRIC, brand=brand, source_agent="research",
                           source_system="similarweb", payload=payload, confidence=0.7)], CostSpec()


class CompetitorNewsAdapter(BaseAdapter):
    """Recent competitor coverage from RSS (coverage signal, not traffic). Emits
    a context entry (portfolio-scoped) plus a flag on a breaking cluster."""

    name = "competitor_news"
    source_system = "rss"
    owner_agent = "research"

    async def _observe(self, brand: str, **kwargs: Any) -> tuple[list[EntryDraft], CostSpec]:
        try:
            import feedparser  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise AdapterUnavailable("feedparser not installed (pip install .[research])") from exc
        import asyncio

        def _pull(url: str) -> list[dict[str, Any]]:
            parsed = feedparser.parse(url)
            out = []
            for e in parsed.entries[:15]:
                out.append({"title": getattr(e, "title", ""), "link": getattr(e, "link", ""),
                            "published": getattr(e, "published", "")})
            return out

        items: list[dict[str, Any]] = []
        for source, url in _COMPETITOR_FEEDS.items():
            try:
                for it in await asyncio.to_thread(_pull, url):
                    items.append({**it, "source": source})
            except Exception as exc:  # noqa: BLE001
                log.info("competitor feed %s failed: %s", source, exc)

        draft = EntryDraft(
            type=EntryType.CONTEXT, brand="portfolio", source_agent="research", source_system="rss",
            payload={"kind": "competitor_coverage", "collected_at": datetime.now(timezone.utc).isoformat(),
                     "item_count": len(items), "items": items[:60]},
            ttl_seconds=24 * 3600,
        )
        return [draft], CostSpec()

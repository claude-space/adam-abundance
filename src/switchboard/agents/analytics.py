"""Analytics agent (PRD §6.5): performance + pace. Runs the BigQuery (consum +
ODS), Sentinel, and Sheets adapters and writes ranked metric/flag entries.
Supersedes writers-dashboard's digest/monitor as the performance brain (reusing
its metric logic); writer-quota admin + sheet write-backs stay in that system."""

from __future__ import annotations

from ..db.enums import EntryType
from ..interfaces import EntryDraft
from ..logging_ import get_logger
from .base import BaseAgent

log = get_logger("agent.analytics")


class AnalyticsAgent(BaseAgent):
    name = "analytics"

    async def observe(self, brand: str) -> int:
        written = await super().observe(brand)
        written += await self._rollup(brand)
        return written

    async def _rollup(self, brand: str) -> int:
        """Rank what matters into one compact summary metric, computed from the
        entries just written to memory (so it works even when a live adapter was
        offline this run). Deterministic — no LLM, no external calls."""
        store = self.ctx.store
        metrics = await store.query(brand=brand, types=[EntryType.METRIC], source_agent="analytics",
                                    fresh_within_seconds=2 * 24 * 3600, limit=50)
        flags = await store.query(brand=brand, types=[EntryType.FLAG], source_agent="analytics",
                                  fresh_within_seconds=2 * 24 * 3600, limit=50)
        # metrics are newest-first → keep the FIRST occurrence per kind so the
        # rollup reflects the latest snapshot (not a stale/duplicate one).
        by_kind: dict = {}
        for m in metrics:
            k = (m.payload or {}).get("kind")
            if k and k not in by_kind:
                by_kind[k] = m.payload
        wp = by_kind.get("writer_performance", {})
        writers = wp.get("writers", [])
        summary = {
            "kind": "analytics_summary",
            "brand_avg_spa": wp.get("brand_avg_spa"),
            "writer_count": len(writers),
            "top_writer": writers[0] if writers else None,
            "at_risk_writers": sum(1 for f in flags if f.payload.get("kind") == "writer_below_index"),
            "sessions_yesterday": by_kind.get("sessions_daily", {}).get("visits"),
            "discover_clicks": by_kind.get("discover_performance", {}).get("clicks"),
            "top_article": (by_kind.get("top_articles", {}).get("articles") or [{}])[0],
        }
        await store.write(EntryDraft(type=EntryType.METRIC, brand=brand, source_agent="analytics",
                                     source_system="switchboard", payload=summary, confidence=0.9,
                                     ttl_seconds=2 * 24 * 3600))
        return 1

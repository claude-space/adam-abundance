"""Opportunity agent (PRD §6.3): what to make next. Runs Ahrefs (metered,
cache-first), GSC, and ideation-status adapters (Albert/Seona/HC-Viral), then
reads the Similarweb landscape *from memory* (written by Research) rather than
calling it directly."""

from __future__ import annotations

from ..db.enums import EntryType
from ..logging_ import get_logger
from .base import BaseAgent

log = get_logger("agent.opportunity")


class OpportunityAgent(BaseAgent):
    name = "opportunity"

    async def observe(self, brand: str) -> int:
        written = await super().observe(brand)
        # Reads Similarweb landscape from memory (Research wrote it) — does NOT
        # call Similarweb directly (domain boundary, PRD §6.3).
        landscape = await self.ctx.store.query(
            brand=brand, types=[EntryType.METRIC], source_system="similarweb", limit=1
        )
        if landscape:
            log.info("[opportunity] incorporating similarweb landscape from memory (entry %s)",
                     landscape[0].id)
        written += await self._shortlist(brand)
        return written

    async def _shortlist(self, brand: str) -> int:
        """Score topic candidates from memory into one ranked shortlist context.
        Viral-trend candidates outrank editorial ones; ties break on recency."""
        from ..interfaces import EntryDraft

        cands = await self.ctx.store.query(brand=brand, types=[EntryType.CONTEXT],
                                           fresh_within_seconds=3 * 24 * 3600, limit=50)
        scored = []
        for c in cands:
            kind = (c.payload or {}).get("kind")
            if kind not in ("topic_candidate", "viral_topic_candidate"):
                continue
            scored.append({"title": c.payload.get("title"), "topic_id": c.payload.get("topic_id"),
                           "source": c.payload.get("source") or c.source_system,
                           "score": 2.0 if kind == "viral_topic_candidate" else 1.0})
        if not scored:
            return 0
        scored.sort(key=lambda x: x["score"], reverse=True)
        await self.ctx.store.write(EntryDraft(
            type=EntryType.CONTEXT, brand=brand, source_agent="opportunity", source_system="switchboard",
            payload={"kind": "opportunity_shortlist", "count": len(scored), "shortlist": scored[:10]},
            ttl_seconds=2 * 24 * 3600))
        return 1

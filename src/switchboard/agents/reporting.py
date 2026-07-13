"""Reporting & Distribution agent (PRD §6.6): assembles outbound artifacts —
the per-brand daily digest, CarBuzz newsletter drafts, and social posts — from
published-performance data, and surfaces them for human review.

In observe it reads performance/competitor context *from memory* (written by
Analytics/Research) rather than re-querying, and writes ``report`` /
``distribution_draft`` entries that record which inputs are ready. The heavy
artifact assembly (HTML/PNG) and the human-approval-gated digest send are Phase-4
actions. **Nothing distributes autonomously.**
"""

from __future__ import annotations

from ..db.enums import EntryType
from ..interfaces import EntryDraft
from ..logging_ import get_logger
from .base import BaseAgent

log = get_logger("agent.reporting")


class ReportingAgent(BaseAgent):
    name = "reporting"

    async def observe(self, brand: str) -> int:
        store = self.ctx.store
        # Read what Analytics + Research already put in memory (no re-query).
        metrics = await store.query(
            brand=brand, types=[EntryType.METRIC], fresh_within_seconds=2 * 24 * 3600, limit=50
        )
        kinds = {m.payload.get("kind") for m in metrics}
        competitor = await store.query(
            brand="portfolio", include_portfolio=False, types=[EntryType.CONTEXT],
            source_system="rss", limit=1,
        )
        inputs = {
            "has_top_articles": "top_articles" in kinds,
            "has_sessions": "sessions_daily" in kinds,
            "has_discover": "discover_performance" in kinds,
            "has_writer_performance": "writer_performance" in kinds,
            "has_competitor_coverage": bool(competitor),
            "metric_entries": [m.id for m in metrics][:50],
        }

        written = 0
        # Daily digest report (inputs snapshot; artifact assembled in Phase 4).
        await store.write(EntryDraft(
            type=EntryType.REPORT, brand=brand, source_agent="reporting",
            source_system="daily_reporting",
            payload={"kind": "daily_digest_inputs", "ready": inputs["has_sessions"], "inputs": inputs},
            ttl_seconds=2 * 24 * 3600,
        ))
        written += 1

        # Newsletter draft is CarBuzz-only (draft artifact, human-send).
        if brand == "carbuzz":
            await store.write(EntryDraft(
                type=EntryType.DISTRIBUTION_DRAFT, brand=brand, source_agent="reporting",
                source_system="newsletter",
                payload={"kind": "newsletter_draft", "status": "inputs_ready",
                         "artifact_ref": None, "note": "assembled on approved plan_item (Phase 4)"},
                ttl_seconds=2 * 24 * 3600,
            ))
            written += 1

        # Social posts (draft artifacts, human-post).
        await store.write(EntryDraft(
            type=EntryType.DISTRIBUTION_DRAFT, brand=brand, source_agent="reporting",
            source_system="social",
            payload={"kind": "social_draft", "status": "inputs_ready", "artifact_ref": None,
                     "note": "images+captions assembled on approved plan_item (Phase 4)"},
            ttl_seconds=2 * 24 * 3600,
        ))
        written += 1

        log.info("[reporting] observe(%s) wrote %d report/draft entries", brand, written)
        return written

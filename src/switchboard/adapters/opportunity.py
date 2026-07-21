"""Opportunity-domain read adapters (PRD §6.3): Ahrefs (metered, cache-first),
GSC striking-distance (BigQuery), and viral-topic candidates from HC Viral Hits.
Ideation *triggers* (Albert/Seona/HC-Viral) are governor-gated actions (Phase 4).
"""

from __future__ import annotations

from typing import Any

from ..db.enums import EntryType
from ..interfaces import CostSpec, EntryDraft
from ..logging_ import get_logger
from ._http import get_json as _get_json
from .base import AdapterUnavailable, BaseAdapter
from .clients.ahrefs import CACHE_TTL_SECONDS, AhrefsClient
from .clients.bigquery import BigQueryClient
from .clients.hcviral import HCViralClient

log = get_logger("adapter.opportunity")


class AhrefsAdapter(BaseAdapter):
    """Metered Ahrefs read. Cache-first: reuses a fresh cached result from shared
    memory (7-day TTL, mirroring Seona's ahrefs_cache) before spending units, and
    refuses if the units cap would be exceeded."""

    name = "ahrefs"
    source_system = "ahrefs"
    owner_agent = "opportunity"

    async def _observe(self, brand: str, **kwargs: Any) -> tuple[list[EntryDraft], CostSpec]:
        if brand == "portfolio":
            raise AdapterUnavailable("ahrefs adapter is brand-scoped")
        bc = self.ctx.settings.brand(brand)
        endpoint = kwargs.get("endpoint", "site-explorer/overview")
        target = kwargs.get("target", bc.domain)

        # Cache-first: reuse a fresh cached result → zero units.
        cached = await self.ctx.store.query(
            brand=brand, types=[EntryType.CONTEXT], source_system="ahrefs",
            payload_contains={"kind": "ahrefs_cache", "target": target, "endpoint": endpoint},
            fresh_within_seconds=CACHE_TTL_SECONDS, limit=1,
        )
        if cached:
            log.info("[ahrefs] cache hit for %s %s — no units spent", target, endpoint)
            payload = dict(cached[0].payload)
            payload["from_cache"] = True
            return [EntryDraft(type=EntryType.METRIC, brand=brand, source_agent="opportunity",
                               source_system="ahrefs", payload=payload, confidence=0.8)], CostSpec()

        # Pre-check units cap before spending.
        est_units = int(kwargs.get("estimated_units", AhrefsClient.units_for(1)))
        if not await self.ctx.governor.within_caps("ahrefs_units", additional=est_units):
            raise AdapterUnavailable("ahrefs_units daily cap would be exceeded")

        client = AhrefsClient(self.ctx.creds.ahrefs_key())
        params = {"target": target, "mode": "domain", "output": "json",
                  **{k: v for k, v in kwargs.items() if k in ("date", "country", "limit")}}
        data = await client.get(endpoint, params)
        rows = data.get("rows") or data.get("keywords") or ([data] if data else [])
        units = AhrefsClient.units_for(len(rows) or 1)

        # Write a cache entry (context) + the metric.
        cache_payload = {"kind": "ahrefs_cache", "target": target, "endpoint": endpoint, "data": data}
        drafts = [
            EntryDraft(type=EntryType.CONTEXT, brand=brand, source_agent="opportunity",
                       source_system="ahrefs", payload=cache_payload, ttl_seconds=CACHE_TTL_SECONDS),
            EntryDraft(type=EntryType.METRIC, brand=brand, source_agent="opportunity",
                       source_system="ahrefs",
                       payload={"kind": "ahrefs_overview", "target": target, "row_count": len(rows),
                                "sample": rows[:5]}, confidence=0.8),
        ]
        return drafts, CostSpec(ahrefs_units=units)


class GSCAdapter(BaseAdapter):
    """Striking-distance keywords from the per-brand GSC export in BigQuery.
    The Auto trio's gsc tables are empty today (PRD §13.13) — emit a flag when so."""

    name = "gsc"
    source_system = "gsc"
    owner_agent = "opportunity"

    async def _observe(self, brand: str, **kwargs: Any) -> tuple[list[EntryDraft], CostSpec]:
        if brand == "portfolio":
            raise AdapterUnavailable("gsc adapter is brand-scoped")
        bc = self.ctx.settings.brand(brand)
        sql = f"""
            SELECT query, SUM(impressions) AS impressions, SUM(clicks) AS clicks,
                   AVG(position) AS avg_position
            FROM `data-science-458422.{bc.gsc_table}`
            WHERE _TABLE_SUFFIX >= FORMAT_DATE('%Y%m%d', DATE_SUB(CURRENT_DATE(), INTERVAL 28 DAY))
            GROUP BY query HAVING avg_position BETWEEN 5 AND 15
            ORDER BY impressions DESC LIMIT 25
        """
        client = BigQueryClient(self.ctx.creds.google_sa())
        try:
            await client.estimate_bytes(sql)
        except Exception as exc:  # noqa: BLE001 — table likely absent/empty
            return [EntryDraft(type=EntryType.FLAG, brand=brand, source_agent="opportunity",
                               source_system="gsc",
                               payload={"kind": "gsc_unavailable", "table": bc.gsc_table,
                                        "detail": str(exc)[:200], "severity": "low"})], CostSpec()
        res = await client.query(sql)
        if not res.rows:
            return [EntryDraft(type=EntryType.FLAG, brand=brand, source_agent="opportunity",
                               source_system="gsc",
                               payload={"kind": "gsc_empty", "table": bc.gsc_table, "severity": "low"})], \
                   CostSpec(bq_bytes=res.bytes_processed)
        return [EntryDraft(type=EntryType.METRIC, brand=brand, source_agent="opportunity",
                           source_system="gsc",
                           payload={"kind": "striking_distance", "keywords": res.rows},
                           confidence=0.85)], CostSpec(bq_bytes=res.bytes_processed)


class _IdeationStatusAdapter(BaseAdapter):
    """Shared base for reading ideation topic candidates from an Albert-family
    service (Claude Albert / Seona). The read path is env-configurable because
    the exact route wasn't confirmed during research; it degrades softly.
    Emits topic candidates as context entries (Opportunity turns the strongest
    into plan_item proposals)."""

    owner_agent = "opportunity"
    endpoint_key = ""      # settings.endpoints key
    path_env = ""          # env var overriding the read path
    default_path = "/api/topics"

    async def _observe(self, brand: str, **kwargs: Any) -> tuple[list[EntryDraft], CostSpec]:
        base = self.ctx.settings.endpoints.get(self.endpoint_key)
        if not base:
            raise AdapterUnavailable(f"{self.endpoint_key} endpoint not configured")
        path = self.ctx.creds.resolve(self.path_env, secret=False) or self.default_path
        data = await _get_json(base, path, params={"status": "proposed", "brand": brand})
        topics = data if isinstance(data, list) else data.get("topics", data.get("data", []))
        entries = [
            EntryDraft(type=EntryType.CONTEXT, brand=brand, source_agent="opportunity",
                       source_system=self.source_system,
                       payload={"kind": "topic_candidate", "source": self.source_system,
                                "topic_id": t.get("id") or t.get("topic_id"),
                                "title": t.get("title") or t.get("headline"),
                                "status": t.get("status", "proposed")},
                       ttl_seconds=2 * 24 * 3600)
            for t in (topics or [])[:25]
        ]
        return entries, CostSpec()


class AlbertIdeationAdapter(_IdeationStatusAdapter):
    """Claude Albert Discover/editorial topic candidates (read; trigger is Phase 4)."""

    name = "albert_ideation"
    source_system = "claude_albert"
    endpoint_key = "albert"
    path_env = "ALBERT_IDEATION_PATH"


class SeonaIdeationAdapter(_IdeationStatusAdapter):
    """Seona SEO topic candidates (read; trigger is Phase 4)."""

    name = "seona_ideation"
    source_system = "seona"
    endpoint_key = "seona"
    path_env = "SEONA_IDEATION_PATH"


class HCViralIdeationAdapter(BaseAdapter):
    """Viral-topic candidates: ready drafts from HC Viral Hits surfaced as topic
    opportunities."""

    name = "hc_viral_ideation"
    source_system = "hc_viral_hits"
    owner_agent = "opportunity"

    async def _observe(self, brand: str, **kwargs: Any) -> tuple[list[EntryDraft], CostSpec]:
        if brand not in ("hotcars", "topspeed"):
            raise AdapterUnavailable("HC Viral Hits serves hotcars + topspeed(-moto) only")
        client = HCViralClient(self.ctx.settings.endpoints["hc_viral_hits"],
                               self.ctx.creds.resolve("HC_VIRAL_HITS_API_KEY"))
        drafts_data = await client.list_drafts(brand, status="ready")
        entries = [
            EntryDraft(type=EntryType.CONTEXT, brand=brand, source_agent="opportunity",
                       source_system="hc_viral_hits",
                       payload={"kind": "viral_topic_candidate", "topic_id": d.get("topic_id") or d.get("id"),
                                "title": d.get("title"), "status": d.get("status", "ready")},
                       ttl_seconds=2 * 24 * 3600)
            for d in drafts_data[:25]
        ]
        return entries, CostSpec()

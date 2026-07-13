"""Analytics-domain read adapters (PRD §6.5): BigQuery (consum + ODS), Sentinel
traffic, and Google Sheets writer quotas. All observe-only. Metered reads
(BigQuery bytes) are cap-checked before running and charged after.

Metric logic reuses writers-dashboard's SQL (published performance) and
daily-reporting-agent's ODS query (Discover) — see docs/INTEGRATION-NOTES.md.
"""

from __future__ import annotations

from typing import Any

from ..db.enums import EntryType
from ..interfaces import CostSpec, EntryDraft
from ..logging_ import get_logger
from .base import AdapterUnavailable, BaseAdapter
from .clients.bigquery import BigQueryClient
from .clients.sentinel import SentinelClient
from .clients.sheets import SheetsClient

log = get_logger("adapter.analytics")

_CONSUM_TABLE = "`data-science-458422.pubinsights_consum_data.auto_new_article_analysis`"
_ODS_TABLE = "`data-science-458422.pubinsights_ods_data.new_article_analysis`"


class _BigQueryBase(BaseAdapter):
    owner_agent = "analytics"

    def _bq(self) -> BigQueryClient:
        return BigQueryClient(self.ctx.creds.google_sa())

    async def _guarded_query(self, client: BigQueryClient, sql: str, params: dict[str, Any]):
        """Estimate bytes, refuse if over the per-run cap, else run + return
        (rows, CostSpec)."""
        estimated = await client.estimate_bytes(sql, params)
        per_run = self.ctx.settings.caps.per_run("bq_bytes")
        if per_run is not None and estimated > per_run:
            raise AdapterUnavailable(
                f"query would scan {estimated} bytes > per-run cap {per_run}"
            )
        if not await self.ctx.governor.within_caps("bq_bytes", additional=estimated):
            raise AdapterUnavailable("bq_bytes daily cap would be exceeded")
        result = await client.query(sql, params)
        return result, CostSpec(bq_bytes=result.bytes_processed)


class BigQueryConsumAdapter(_BigQueryBase):
    """Published-performance from the consum table: writer pace + top articles."""

    name = "bigquery_consum"
    source_system = "bigquery"

    async def _observe(self, brand: str, **kwargs: Any) -> tuple[list[EntryDraft], CostSpec]:
        if brand == "portfolio":
            raise AdapterUnavailable("consum adapter is brand-scoped")
        bc = self.ctx.settings.brand(brand)
        client = self._bq()

        writer_sql = f"""
            SELECT Writer AS writer, Intent AS intent,
              COALESCE(SUM(ActSessSentinel),0) AS total_sessions,
              COUNT(DISTINCT URL) AS total_articles,
              SAFE_DIVIDE(COALESCE(SUM(ActSessSentinel),0), COUNT(DISTINCT URL)) AS spa
            FROM {_CONSUM_TABLE}
            WHERE Brand=@brand
              AND Intent IN ('Feed','Evergreen','Sniping','Short-Term')
              AND PubDate >= DATE_TRUNC(CURRENT_DATE('America/New_York'), MONTH)
              AND PubDate <  CURRENT_DATE('America/New_York')
              AND Writer IS NOT NULL AND Writer != '' AND ContentType != 'Resource'
            GROUP BY Writer, Intent ORDER BY Intent, spa DESC
        """
        top_sql = f"""
            SELECT ArticleTitle AS title, URL AS url, PriCat AS category, Intent AS intent,
              ActSessSentinel AS sessions, AVD AS avd,
              viewAvgEngagedDepthPercentage AS scroll_depth
            FROM {_CONSUM_TABLE}
            WHERE Brand=@brand
              AND PubDate >= DATE_SUB(CURRENT_DATE('America/New_York'), INTERVAL 2 DAY)
              AND PubDate <  CURRENT_DATE('America/New_York')
            ORDER BY ActSessSentinel DESC LIMIT 15
        """
        params = {"brand": bc.short_code}
        writer_res, cost1 = await self._guarded_query(client, writer_sql, params)
        top_res, cost2 = await self._guarded_query(client, top_sql, params)
        cost = cost1.merge(cost2)

        # Aggregate per-writer across intents; brand avg SPA = ΣS/ΣA.
        by_writer: dict[str, dict[str, float]] = {}
        tot_s = tot_a = 0.0
        for r in writer_res.rows:
            w = r["writer"]
            acc = by_writer.setdefault(w, {"sessions": 0.0, "articles": 0.0})
            acc["sessions"] += float(r["total_sessions"] or 0)
            acc["articles"] += float(r["total_articles"] or 0)
            tot_s += float(r["total_sessions"] or 0)
            tot_a += float(r["total_articles"] or 0)
        brand_avg_spa = (tot_s / tot_a) if tot_a else 0.0

        writers = []
        for w, acc in by_writer.items():
            spa = round(acc["sessions"] / acc["articles"]) if acc["articles"] else 0
            idx = round(spa / brand_avg_spa, 2) if brand_avg_spa else 0.0
            writers.append(
                {"writer": w, "articles": int(acc["articles"]), "sessions": int(acc["sessions"]),
                 "sessions_per_article": spa, "relative_index": idx}
            )
        writers.sort(key=lambda x: x["relative_index"], reverse=True)

        drafts: list[EntryDraft] = [
            EntryDraft(
                type=EntryType.METRIC, brand=brand, source_agent="analytics",
                source_system="bigquery",
                payload={"kind": "writer_performance", "period": "MTD",
                         "brand_avg_spa": round(brand_avg_spa, 1), "writers": writers},
                confidence=0.9,
            ),
            EntryDraft(
                type=EntryType.METRIC, brand=brand, source_agent="analytics",
                source_system="bigquery",
                payload={"kind": "top_articles", "window_days": 2, "articles": top_res.rows},
                confidence=0.9,
            ),
        ]
        # At-risk writers (index < 0.9 with a meaningful sample) → flags.
        for w in writers:
            if w["relative_index"] and w["relative_index"] < 0.9 and w["articles"] > 3:
                drafts.append(
                    EntryDraft(
                        type=EntryType.FLAG, brand=brand, source_agent="analytics",
                        source_system="bigquery",
                        payload={"kind": "writer_below_index", "writer": w["writer"],
                                 "relative_index": w["relative_index"], "articles": w["articles"],
                                 "severity": "medium"},
                    )
                )
        return drafts, cost


class BigQueryDiscoverAdapter(_BigQueryBase):
    """Discover performance from the ODS table (brandName = full name)."""

    name = "bigquery_discover"
    source_system = "bigquery"

    async def _observe(self, brand: str, **kwargs: Any) -> tuple[list[EntryDraft], CostSpec]:
        if brand == "portfolio":
            raise AdapterUnavailable("discover adapter is brand-scoped")
        bc = self.ctx.settings.brand(brand)
        sql = f"""
            SELECT permalink, intentName, writerName, primaryCategoryName,
              discoverClicks, discoverImpressions, discoverCTR
            FROM {_ODS_TABLE}
            WHERE brandName=@brand
              AND DATE(datePublished) BETWEEN DATE_SUB(CURRENT_DATE(), INTERVAL 10 DAY)
                                          AND DATE_SUB(CURRENT_DATE(), INTERVAL 3 DAY)
            ORDER BY discoverClicks DESC LIMIT 20
        """
        client = self._bq()
        res, cost = await self._guarded_query(client, sql, {"brand": bc.discover_name})
        clicks = sum(int(r.get("discoverClicks") or 0) for r in res.rows)
        impressions = sum(int(r.get("discoverImpressions") or 0) for r in res.rows)
        draft = EntryDraft(
            type=EntryType.METRIC, brand=brand, source_agent="analytics", source_system="bigquery",
            payload={"kind": "discover_performance", "clicks": clicks, "impressions": impressions,
                     "ctr": round(clicks / impressions, 4) if impressions else 0.0,
                     "top": res.rows},
            confidence=0.9,
        )
        return [draft], cost


class SentinelTrafficAdapter(BaseAdapter):
    """Day-of sessions/engagement from Sentinel Pro `traffic/`."""

    name = "sentinel_traffic"
    source_system = "sentinel"
    owner_agent = "analytics"

    async def _observe(self, brand: str, **kwargs: Any) -> tuple[list[EntryDraft], CostSpec]:
        if brand == "portfolio":
            raise AdapterUnavailable("sentinel adapter is brand-scoped")
        api_key, account = self.ctx.creds.sentinel()
        client = SentinelClient(api_key, account)
        bc = self.ctx.settings.brand(brand)
        from datetime import date, timedelta

        end = date.today()
        start = end - timedelta(days=1)
        payload = {
            "filters": {
                "date": {"gte": start.isoformat(), "lt": end.isoformat()},
                "propertyId": {"in": [bc.sentinel_property_id]},
            },
            "metrics": ["visits", "averageEngagedDuration", "averageEngagedDepth"],
            "dimensions": ["date", "propertyId"],
            "granularity": "daily",
        }
        rows = await client.traffic(payload, max_pages=3)
        visits = sum(float(r.get("visits") or 0) for r in rows)
        draft = EntryDraft(
            type=EntryType.METRIC, brand=brand, source_agent="analytics", source_system="sentinel",
            payload={"kind": "sessions_daily", "date": start.isoformat(), "visits": int(visits),
                     "rows": rows[:10]},
            confidence=0.95, ttl_seconds=2 * 24 * 3600,
        )
        return [draft], CostSpec()


class SheetsQuotaAdapter(BaseAdapter):
    """Writer quotas/baselines from the per-brand Google Sheet (read-only)."""

    name = "sheets_quota"
    source_system = "sheets"
    owner_agent = "analytics"

    async def _observe(self, brand: str, **kwargs: Any) -> tuple[list[EntryDraft], CostSpec]:
        sheet_id = self.ctx.creds.resolve(f"SHEET_ID_{brand.upper()}", secret=False)
        if not sheet_id:
            raise AdapterUnavailable(f"SHEET_ID_{brand.upper()} not configured")
        client = SheetsClient(self.ctx.creds.google_sa())
        # Convention (writers-dashboard): tab "Team Sheets", col A name, B email, H quota.
        records = await client.read_records(sheet_id, "Team Sheets")
        quotas = []
        for rec in records:
            name = (list(rec.values())[0] if rec else "") or ""
            if not name or "total" in str(name).lower():
                continue
            quotas.append({"writer": name, "row": rec})
        draft = EntryDraft(
            type=EntryType.CONTEXT, brand=brand, source_agent="analytics", source_system="sheets",
            payload={"kind": "writer_quotas", "count": len(quotas), "quotas": quotas[:100]},
        )
        return [draft], CostSpec()

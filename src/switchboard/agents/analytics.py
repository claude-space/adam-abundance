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
        written += await self._session_trends(brand)
        written += await self._writer_stats(brand)
        return written

    # -- top-writer stats (PRD §16.3) ----------------------------------------

    async def _writer_stats(self, brand: str) -> int:
        """Cohort-normalized writer_stats for the window from the consum table +
        top-N flagging (§16.3). Soft-fails when BigQuery is unavailable."""
        if brand == "portfolio":
            return 0
        from datetime import date, timedelta

        from sqlalchemy import delete as _delete

        from ..adapters.analytics import _CONSUM_TABLE
        from ..adapters.base import AdapterUnavailable
        from ..adapters.clients.bigquery import BigQueryClient
        from ..db.models import WriterStats
        from ..writers import normalize_writers

        def _int(key: str, default: int) -> int:
            try:
                return int(self.ctx.creds.resolve(key, secret=False) or default)
            except ValueError:
                return default
        window_days = _int("WRITER_STATS_WINDOW_DAYS", 90)
        top_n = _int("WRITER_TOP_N", 10)
        min_articles = _int("WRITER_MIN_ARTICLES", 5)

        bc = self.ctx.settings.brand(brand)
        try:
            client = BigQueryClient(self.ctx.creds.google_sa())
        except AdapterUnavailable:
            return 0
        sql = f"""
            SELECT Writer AS author, PriCat AS category, Intent AS intent,
                   COALESCE(ActSessSentinel, 0) AS sessions
            FROM {_CONSUM_TABLE}
            WHERE Brand=@brand AND Writer IS NOT NULL AND Writer != ''
              AND PubDate >= DATE_SUB(CURRENT_DATE('America/New_York'), INTERVAL {window_days} DAY)
              AND PubDate <  CURRENT_DATE('America/New_York')
        """
        params = {"brand": bc.short_code}
        try:
            estimated = await client.estimate_bytes(sql, params)
            if not await self.ctx.governor.within_caps("bq_bytes", additional=estimated):
                log.info("[analytics] writer_stats skipped for %s — bq_bytes cap", brand)
                return 0
            res = await client.query(sql, params)
        except Exception as exc:  # noqa: BLE001 — BQ offline/erroring → skip softly
            log.info("[analytics] writer_stats query failed for %s: %s", brand, exc)
            return 0
        await self.ctx.governor.charge("bq_bytes", getattr(res, "bytes_processed", 0) or 0, "analytics")

        ranked = normalize_writers(
            [{"author": r.get("author"), "category": r.get("category"),
              "intent": r.get("intent"), "sessions": r.get("sessions")} for r in res.rows],
            min_articles=min_articles, top_n=top_n)

        win_end = date.today()
        win_start = win_end - timedelta(days=window_days)
        await self.ctx.session.execute(_delete(WriterStats).where(
            WriterStats.brand == brand, WriterStats.window_start == win_start,
            WriterStats.window_end == win_end))
        for w in ranked:
            self.ctx.session.add(WriterStats(
                brand=brand, author=w["author"], window_start=win_start, window_end=win_end,
                article_count=w["article_count"], avg_sessions=w["avg_sessions"],
                norm_score=w["norm_score"], is_top=w["is_top"]))
        await self.ctx.session.flush()

        await self.ctx.store.write(EntryDraft(
            type=EntryType.METRIC, brand=brand, source_agent="analytics", source_system="bigquery",
            payload={"kind": "top_writers", "window_days": window_days, "top_n": top_n,
                     "writer_count": len(ranked), "top": [w for w in ranked if w["is_top"]]},
            confidence=0.9, ttl_seconds=7 * 24 * 3600))
        return len(ranked)

    # -- session trends (PRD §16.1) ------------------------------------------

    def _flag_pct(self) -> float:
        raw = self.ctx.creds.resolve("SESSION_TREND_FLAG_PCT", secret=False)
        try:
            return float(raw) if raw else 25.0
        except ValueError:
            return 25.0

    async def _session_trends(self, brand: str) -> int:
        """Weekly rollup + daily series for the last complete ISO week, with
        WoW/DoD deltas and notable-movement flags (§16.1). Soft-fails when
        Sentinel is unavailable — it's an enrichment, not a hard dependency."""
        if brand == "portfolio":
            return 0  # per-property; portfolio is the union of brand runs
        from datetime import date, timedelta

        from ..adapters.base import AdapterUnavailable
        from ..adapters.clients.sentinel import SentinelClient
        from ..session_trends import compute_session_trends, iso_week_start

        try:
            api_key, account = self.ctx.creds.sentinel()
            client = SentinelClient(api_key, account)
        except AdapterUnavailable:
            return 0
        bc = self.ctx.settings.brand(brand)
        week_start = iso_week_start(date.today()) - timedelta(days=7)  # last COMPLETE week
        try:
            this_rows = await self._sentinel_daily(client, bc, week_start, week_start + timedelta(days=7))
            prev_rows = await self._sentinel_daily(client, bc, week_start - timedelta(days=7), week_start)
        except Exception as exc:  # noqa: BLE001 — Sentinel offline/erroring → skip softly
            log.info("[analytics] session-trends fetch failed for %s: %s", brand, exc)
            return 0

        result = compute_session_trends(
            brand=brand, week_start=week_start, this_week_rows=this_rows,
            prev_week_rows=prev_rows, threshold_pct=self._flag_pct())
        drafts: list[EntryDraft] = [
            EntryDraft(type=EntryType.METRIC, brand=brand, source_agent="analytics",
                       source_system="sentinel", payload=result, confidence=0.95,
                       ttl_seconds=60 * 24 * 3600)  # retain ~60d so the week selector has history
        ]
        for f in result["flags"]:
            drafts.append(EntryDraft(
                type=EntryType.FLAG, brand=brand, source_agent="analytics", source_system="sentinel",
                payload={"kind": "session_movement", "metric": f["metric"], "change": f["kind"],
                         "pct": f["pct"], "direction": f["direction"], "iso_week": result["iso_week"],
                         **({"date": f["date"]} if f.get("date") else {}),
                         "severity": "high" if abs(f["pct"]) >= 2 * result["threshold_pct"] else "medium"}))
        await self.ctx.store.write_many(drafts)
        return len(drafts)

    async def _sentinel_daily(self, client, bc, start, end) -> list[dict]:
        """Daily-granularity Sentinel traffic rows for [start, end)."""
        from ..session_trends import DEFAULT_METRICS
        payload = {
            "filters": {"date": {"gte": start.isoformat(), "lt": end.isoformat()},
                        "propertyId": {"in": [bc.sentinel_property_id]}},
            "metrics": list(DEFAULT_METRICS),
            "dimensions": ["date", "propertyId"],
            "granularity": "daily",
        }
        return await client.traffic(payload, max_pages=3)

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

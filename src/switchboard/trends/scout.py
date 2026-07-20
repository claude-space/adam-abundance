"""The Trend Scout — one scan pass (docs/trend-pipeline.md):

  pull sources → read signals from memory → cluster + score → upsert trends →
  build dossiers → create trigger requests (pending human approval) → notify.

The scout observes and proposes only. It cannot approve anything (the repo
rejects non-human actors), and every proposal is capped
(``TREND_MAX_OPEN_PIPELINES``) and perishable (``TREND_TTL_HOURS``).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..adapters.base import AdapterUnavailable
from ..adapters.research import CompetitorNewsAdapter
from ..adapters.trend_sources import (
    FirecrawlTrendAdapter,
    NewsApiTrendAdapter,
    PerplexityTrendAdapter,
    SemrushTrendAdapter,
    TavilyTrendAdapter,
    XTrendAdapter,
    YouTubeTrendAdapter,
)
from ..context import RunContext
from ..db.enums import PORTFOLIO, EntryType, TrendStatus
from ..db.models import Trend
from ..interfaces import EntryDraft
from ..logging_ import get_logger
from ..orchestrator.slack import notify_trend_event
from . import detector
from .dossier import collect_dossier
from .lifecycle import LifecycleError, validate_content_types
from .repo import PipelineRepo, TrendRepo

log = get_logger("trends.scout")

_SOURCE_ADAPTERS = (TavilyTrendAdapter, NewsApiTrendAdapter, FirecrawlTrendAdapter,
                    PerplexityTrendAdapter, YouTubeTrendAdapter, XTrendAdapter,
                    SemrushTrendAdapter, CompetitorNewsAdapter)
_MAX_NEW_PROPOSALS_PER_SCAN = 3

# Neighbourhood scoring (§13.19 F2/F3): map a related trend's lifecycle state to a
# same-topic momentum q∈[-1,1] and to an adjacent-theme decline weight d∈[0,1].
_STATE_MOMENTUM = {"emerging": 0.0, "rising": 0.6, "peak": 0.2, "declining": -0.6, "dormant": -0.9}
_STATE_DECLINE = {"declining": 0.6, "dormant": 0.9}
_SAME_TOPIC_SIM = 0.6      # ≥ this ⇒ the same topic recurring (drives F2 momentum q)
_ADJACENT_SIM = 0.3        # [this, _SAME_TOPIC_SIM) ⇒ an adjacent theme (drives F3 fatigue)
_FATIGUE_AGE_DAYS = 3      # an adjacent theme must have trended "a little while" to fatigue


def _evidence(item: dict[str, Any]) -> dict[str, Any]:
    """Trim a normalized signal item (see _signals_from_memory) to the
    {origin, source, title, url, published_at} shape Trend.evidence stores."""
    return {
        "origin": item.get("origin", ""), "source": item.get("source", ""),
        "title": item.get("title", ""), "url": item.get("url", ""),
        "published_at": item.get("published_at", ""),
    }


class TrendScout:
    def __init__(self, ctx: RunContext) -> None:
        self.ctx = ctx
        self.trends = TrendRepo(ctx.session)
        self.pipelines = PipelineRepo(ctx.session)

    async def scan(self, brand: str = PORTFOLIO) -> dict[str, Any]:
        """Full scan pass. ``brand`` scopes the *proposals*; sourcing is always
        portfolio-wide (competitor news doesn't respect brand boundaries)."""
        cfg = self.ctx.settings.trends
        if not cfg.enabled:
            log.info("[scout] trend pipeline disabled (TREND_PIPELINE_ENABLED=0)")
            return {"enabled": False}
        if not self.ctx.settings.is_valid_scope(brand):
            log.warning("[scout] invalid brand scope %r — scan refused", brand)
            return {"enabled": True, "error": f"invalid brand '{brand}' "
                    f"(use portfolio or one of {list(self.ctx.settings.brand_keys)})"}

        pulled = await self._pull_sources()
        items = await self._signals_from_memory()
        clusters = detector.cluster_signals(items)
        our_titles = await self._our_titles()
        hc_titles = await self._hc_viral_titles()
        dr_titles = await self._daily_reporting_titles()
        neigh = await self._neighborhood_trends(brand)
        session_mom = await self._session_momentum()
        # Operator-tuned score weights (§13.19) — loaded once per scan.
        from .weights import load_effective
        score_weights = await load_effective(self.ctx.session)

        expired = await self.trends.expire_stale()
        summary: dict[str, Any] = {
            "enabled": True, "signals": len(items), "clusters": len(clusters),
            "sources_pulled": pulled, "expired": expired,
            "new_trends": 0, "updated_trends": 0, "proposed": 0, "suppressed": 0,
            "corroborated": 0,
        }

        proposals_left = _MAX_NEW_PROPOSALS_PER_SCAN
        for cluster in sorted(clusters, key=lambda c: -len(c.items)):
            if len(cluster.sources) < cfg.min_sources:
                continue
            covered = detector.covered_by_titles(cluster, our_titles) if our_titles else None
            # Cross-source corroboration (§13.19): which independent monitoring
            # systems also landed on this topic. Our own sourcing is the baseline;
            # HC-Viral + daily-reporting each add confidence.
            monitors: list[str] = []
            if hc_titles and detector.corroborated_by_titles(cluster, hc_titles):
                monitors.append("hc_viral_hits")
            if dr_titles and detector.corroborated_by_titles(cluster, dr_titles):
                monitors.append("daily_reporting")
            if monitors:
                summary["corroborated"] += 1
            # Neighbourhood signals (§13.19 F2/F3): same-topic momentum (real
            # reader-session slope, proxy fallback) + adjacent-theme fatigue.
            momentum, fatigue = self._cluster_signals(cluster, neigh, session_mom)
            score, breakdown = detector.score_cluster(
                cluster, watchlist=cfg.watchlist, covered=covered,
                corroborating_monitors=monitors,
                topic_momentum=momentum, theme_fatigue=fatigue,
                weights=score_weights)
            trend, created = await self.trends.upsert(
                brand=brand, cluster_key=cluster.cluster_key(), headline=cluster.headline,
                score=score, score_breakdown=breakdown,
                velocity=detector.cluster_velocity(cluster),
                source_count=len(cluster.sources), signal_count=len(cluster.items),
                covered_by_us=covered,
                entities={"oems": list(cluster.oem_anchor),
                          "corroborated_by": monitors},
                evidence=[_evidence(i) for i in cluster.items],
                ttl_hours=cfg.ttl_hours, dedup_days=cfg.dedup_days,
            )
            if trend is None:
                summary["suppressed"] += 1
                continue
            summary["new_trends" if created else "updated_trends"] += 1
            # Opportunity suppression gate (§16.2): never propose a fading trend.
            if (trend.status == TrendStatus.DETECTED.value and score >= cfg.score_threshold
                    and not trend.suppressed and proposals_left > 0):
                if await self._propose(trend):
                    summary["proposed"] += 1
                    proposals_left -= 1

        summary["lifecycle_declined"] = await self._update_lifecycle(brand)
        log.info("[scout] scan done: %s", summary)
        return summary

    # -- steps -------------------------------------------------------------------

    async def _pull_sources(self) -> int:
        """Run the sourcing adapters (portfolio-wide) and persist their signals."""
        pulled = 0
        for cls in _SOURCE_ADAPTERS:
            adapter = cls(self.ctx)
            try:
                drafts = await adapter.observe(PORTFOLIO)
                if drafts:
                    await self.ctx.store.write_many(drafts)
                    pulled += 1
            except Exception as exc:  # noqa: BLE001 — BaseAdapter already soft-fails; belt & braces
                log.info("[scout] source %s failed: %s", cls.name, exc)
        return pulled

    async def _signals_from_memory(self) -> list[dict[str, Any]]:
        """Flatten fresh trend_signals + competitor_coverage entries into items."""
        items: list[dict[str, Any]] = []
        entries = await self.ctx.store.query(
            brand=PORTFOLIO, types=[EntryType.CONTEXT],
            fresh_within_seconds=24 * 3600, limit=100,
        )
        for entry in entries:
            payload = entry.payload or {}
            kind = payload.get("kind")
            if kind == "trend_signals":
                items.extend(i for i in payload.get("items", []) if i.get("title") or i.get("url"))
            elif kind == "competitor_coverage":
                for i in payload.get("items", []):
                    items.append({
                        "origin": "rss", "source": i.get("source", ""),
                        "title": i.get("title", ""), "url": i.get("link", ""),
                        "published_at": i.get("published", ""), "snippet": "",
                    })
        return items

    async def _our_titles(self) -> list[str]:
        """Recent titles from our own brand feeds — the coverage-gap check.
        Env-overridable per brand (OUR_NEWS_FEED_<BRAND>); degrades to unknown."""
        try:
            import feedparser  # type: ignore
        except ImportError:
            return []
        import asyncio

        titles: list[str] = []
        for key, bc in self.ctx.settings.brands.items():
            url = (self.ctx.creds.resolve(f"OUR_NEWS_FEED_{key.upper()}", secret=False)
                   or f"https://www.{bc.domain}/feed/")
            try:
                parsed = await asyncio.to_thread(feedparser.parse, url)
                titles.extend(getattr(e, "title", "") for e in parsed.entries[:40])
            except Exception as exc:  # noqa: BLE001
                log.info("[scout] own feed %s failed: %s", key, exc)
        return [t for t in titles if t]

    async def _hc_viral_titles(self) -> list[str]:
        """Titles HC Viral Hits has independently landed on — the cross-monitor
        corroboration signal. Prefers its topics surface (all statuses); falls
        back to the ready-draft queue when that endpoint isn't exposed yet.
        Soft-fails to [] — corroboration is a bonus, never a hard dependency."""
        from ..adapters.clients.hcviral import HCViralClient

        base = self.ctx.settings.endpoints.get("hc_viral_hits")
        key = self.ctx.creds.resolve("HC_VIRAL_HITS_API_KEY")
        if not (base and key):
            return []
        client = HCViralClient(base, key)
        titles: list[str] = []
        for brand in ("hotcars", "topspeed"):  # HC-Viral's served brands
            rows: list[dict[str, Any]] = []
            try:
                rows = await client.list_topics(brand)
            except Exception:  # noqa: BLE001 — topics surface absent → ready drafts
                try:
                    rows = await client.list_drafts(brand, status="ready")
                except Exception as exc:  # noqa: BLE001 — soft-fail
                    log.info("[scout] hc-viral corroboration fetch failed (%s): %s", brand, exc)
            titles.extend(r.get("title", "") for r in rows if r.get("title"))
        return [t for t in titles if t]

    async def _daily_reporting_titles(self) -> list[str]:
        """Trend/topic titles Anthony's daily-reporting agent has surfaced — the
        third corroboration source (§13.19). Read from shared memory, where a
        feeder exports that agent's findings (kind ``daily_report_trends``). Soft-
        empty until it's connected, so corroboration degrades to us + HC-Viral."""
        try:
            entries = await self.ctx.store.query(
                brand=PORTFOLIO, types=[EntryType.CONTEXT],
                payload_contains={"kind": "daily_report_trends"},
                fresh_within_seconds=48 * 3600, limit=50)
        except Exception as exc:  # noqa: BLE001 — corroboration is a bonus, never a dependency
            log.info("[scout] daily-reporting corroboration fetch failed: %s", exc)
            return []
        titles: list[str] = []
        for e in entries:
            for it in (e.payload or {}).get("items", []):
                t = it.get("title") or it.get("topic") or it.get("headline")
                if t:
                    titles.append(t)
        return [t for t in titles if t]

    async def _neighborhood_trends(self, brand: str) -> list[dict[str, Any]]:
        """Precompute recent tracked trends' (tokens, oems, lifecycle state, age)
        for the per-cluster momentum/fatigue signals (§13.19 F2/F3). Their `state`
        is set by _update_lifecycle from each trend's activity history, so it's a
        real read on how a topic has been performing. Soft-empty on any error —
        these factors are refinements, not hard dependencies."""
        now = datetime.now(timezone.utc)
        out: list[dict[str, Any]] = []
        try:
            rows = await self.trends.list(brand=brand or None, limit=120)
        except Exception as exc:  # noqa: BLE001
            log.info("[scout] neighborhood load failed: %s", exc)
            return out
        for t in rows:
            out.append({
                "tokens": detector.tokens(t.headline or ""),
                "oems": tuple((t.entities or {}).get("oems", []) or []),
                "state": (t.state or "").lower(),
                "age_days": (now - t.created_at).days if t.created_at else 0,
            })
        return out

    def _cluster_signals(self, cluster: Any, neigh: list[dict[str, Any]],
                         session_mom: dict[str, float]) -> tuple[float | None, float | None]:
        """(topic_momentum q, theme_fatigue r) for a cluster from its neighbourhood.

        q (F2 — is THIS topic performing?) prefers the REAL per-OEM reader-session
        slope for the cluster's make(s); it falls back to the best same-topic
        (sim≥0.6) recurring trend's lifecycle momentum only when we have no session
        signal. r (F3 — is an adjacent theme tiring?) = max similarity×decline over
        adjacent (0.3≤sim<0.6), aging, declining themes."""
        from .sessions import momentum_for_oems

        q: float | None = momentum_for_oems(cluster.oem_anchor, session_mom)  # real sessions first
        best = 0.0
        r = 0.0
        for nb in neigh:
            sim = detector.topic_similarity(cluster, nb["tokens"], nb["oems"])
            if sim >= _SAME_TOPIC_SIM:
                if q is None and sim > best:            # proxy only when no session signal
                    best, q = sim, _STATE_MOMENTUM.get(nb["state"], 0.0)
            elif sim >= _ADJACENT_SIM and nb["age_days"] >= _FATIGUE_AGE_DAYS \
                    and nb["state"] in _STATE_DECLINE:
                r = max(r, round(sim * _STATE_DECLINE[nb["state"]], 3))
        return q, (r or None)

    async def _session_momentum(self) -> dict[str, float]:
        """Per-OEM reader-session momentum from the consum table (last 8 weeks,
        portfolio-wide): are our articles on a given make trending up or down in
        sessions? Feeds F2's same-topic performance q with real data (§13.19).
        Governor-guarded; soft-empty on any failure — a refinement, not a
        dependency."""
        from ..adapters.analytics import _CONSUM_TABLE
        from ..adapters.base import AdapterUnavailable
        from ..adapters.clients.bigquery import BigQueryClient
        from .sessions import compute_session_momentum

        try:
            client = BigQueryClient(self.ctx.creds.google_sa())
        except AdapterUnavailable:
            return {}
        brands = [b.short_code for k, b in self.ctx.settings.brands.items() if k != "portfolio"]
        if not brands:
            return {}
        sql = f"""
            SELECT ArticleTitle AS title, COALESCE(ActSessSentinel, 0) AS sessions,
                   FORMAT_DATE('%G%V', PubDate) AS week
            FROM {_CONSUM_TABLE}
            WHERE Brand IN UNNEST(@brands)
              AND PubDate >= DATE_SUB(CURRENT_DATE('America/New_York'), INTERVAL 56 DAY)
              AND PubDate <  CURRENT_DATE('America/New_York')
              AND ArticleTitle IS NOT NULL AND ArticleTitle != '' AND ContentType != 'Resource'
        """
        params = {"brands": brands}
        try:
            estimated = await client.estimate_bytes(sql, params)
            if not await self.ctx.governor.within_caps("bq_bytes", additional=estimated):
                log.info("[scout] session-momentum skipped — bq_bytes cap")
                return {}
            res = await client.query(sql, params)
        except Exception as exc:  # noqa: BLE001
            log.info("[scout] session-momentum query failed: %s", exc)
            return {}
        await self.ctx.governor.charge("bq_bytes", getattr(res, "bytes_processed", 0) or 0, "trend_scout")
        return compute_session_momentum(res.rows)

    async def _update_lifecycle(self, brand: str) -> int:
        """Record today's activity for the brand's tracked trends, recompute each
        one's state, and auto-suppress fading ones (§16.2). Returns the count that
        newly flipped to `declining`. Idempotent per day."""
        from datetime import date, datetime, timezone

        from ..db.enums import TrendState
        from .monitor import compute_trend_state

        today = date.today()
        now = datetime.now(timezone.utc)
        flipped = 0
        for t in await self.trends.list_for_lifecycle(brand):
            # External-interest proxy from the activity we actually observe in our
            # sources (breadth + volume + velocity), 0..100. Upgradeable to a real
            # Trends/SerpAPI interest-over-time series later (§13.19).
            ext = min(100.0, round((t.velocity or 0.0) * 6.0
                                   + (t.source_count or 0) * 6.0
                                   + (t.signal_count or 0) * 1.5, 1))
            await self.trends.record_activity(
                t.id, today, external_score=ext,
                article_count=(len(t.evidence) if t.evidence else None))
            res = compute_trend_state(await self.trends.activity_series(t.id), evergreen=t.evergreen)
            peak_at = now if (res["state"] == TrendState.PEAK.value and t.peak_at is None) else None
            was = t.state
            await self.trends.set_lifecycle(t.id, res["state"], res["suppressed"], peak_at=peak_at)
            if res["state"] == TrendState.DECLINING.value and was != TrendState.DECLINING.value:
                flipped += 1
                await self._write_lifecycle_flag(t, res)
        return flipped

    async def _write_lifecycle_flag(self, trend: Trend, res: dict[str, Any]) -> None:
        """Surface a newly-declining trend to the morning plan."""
        await self.ctx.store.write(EntryDraft(
            type=EntryType.FLAG, brand=trend.brand, source_agent="trend_scout",
            source_system="trend_monitor",
            payload={"kind": "trend_declining", "trend_id": trend.id, "headline": trend.headline,
                     "state": res["state"], "delta_pct": res.get("delta_pct"),
                     "suppressed": res["suppressed"], "severity": "medium"},
            ttl_seconds=7 * 24 * 3600))

    async def _propose(self, trend: Trend) -> bool:
        """Turn a hot trend into a pending trigger request + dossier + notify."""
        cfg = self.ctx.settings.trends
        # Validate config BEFORE spending dossier money — a bad
        # TREND_DEFAULT_CONTENT_TYPES must not burn LLM budget every scan.
        try:
            content_types = validate_content_types(list(cfg.default_content_types))
        except LifecycleError as exc:
            log.warning("[scout] TREND_DEFAULT_CONTENT_TYPES invalid (%s) — cannot propose", exc)
            return False
        open_count = await self.pipelines.open_count(trend.brand)
        if open_count >= cfg.max_open_pipelines:
            log.info("[scout] proposal cap reached (%d open) — flagging only", open_count)
            await self._write_flag(trend, note="proposal cap reached")
            return False

        if cfg.auto_dossier:
            trend.status = TrendStatus.DOSSIER_BUILDING.value
            await self.ctx.session.flush()
            try:
                await collect_dossier(self.ctx, trend)
            except Exception as exc:  # noqa: BLE001
                log.warning("[scout] dossier failed for trend %s: %s", trend.id, exc)

        try:
            pipeline = await self.pipelines.create(
                trend_id=trend.id, brand=trend.brand,
                content_types=content_types, requested_by="trend_scout",
            )
        except LifecycleError as exc:
            # create() only refuses when an open pipeline already exists, so the
            # trend is de-facto proposed. (No trend.pipelines access here — the
            # upsert path doesn't eager-load the collection.)
            log.info("[scout] not proposing trend %s: %s", trend.id, exc)
            trend.status = TrendStatus.PROPOSED.value
            await self.ctx.session.flush()
            return False
        trend.status = TrendStatus.PROPOSED.value
        await self.ctx.session.flush()
        await self._write_flag(trend)
        await notify_trend_event(
            self.ctx, trend.brand, "trigger_requested",
            headline=trend.headline, trend_id=trend.id, pipeline_id=pipeline.id,
            score=trend.score,
        )
        return True

    async def _write_flag(self, trend: Trend, note: str | None = None) -> None:
        """Surface the trend to the morning planner as a flag entry — superseding
        the previous flag for the same trend so repeat scans don't pile them up."""
        stale = await self.ctx.store.query(
            brand=trend.brand, types=[EntryType.FLAG],
            payload_contains={"kind": "competitor_trend", "trend_id": trend.id}, limit=10,
        )
        if stale:
            await self.ctx.store.supersede([e.id for e in stale])
        severity = "high" if detector.is_breaking_text(trend.headline) else "medium"
        await self.ctx.store.write(EntryDraft(
            type=EntryType.FLAG, brand=trend.brand, source_agent="trend_scout",
            source_system="trend_scan",
            payload={"kind": "competitor_trend", "trend_id": trend.id,
                     "headline": trend.headline, "score": trend.score,
                     "severity": severity, **({"note": note} if note else {})},
            source_urls=[e.get("url") for e in (trend.evidence or [])[:5] if e.get("url")] or None,
            ttl_seconds=3 * 24 * 3600,
        ))


async def add_manual_trend(ctx: RunContext, *, topic: str, brand: str, actor: str,
                           url: str | None = None) -> Trend:
    """Editor-initiated trend ("run the pipeline on this") — rides the exact
    same dossier/trigger path as a detected trend."""
    repo = TrendRepo(ctx.session)
    cfg = ctx.settings.trends
    evidence = [{"origin": "manual", "source": actor, "title": topic, "url": url or "",
                 "published_at": datetime.now(timezone.utc).isoformat()}]
    cluster_key = "-".join(sorted(detector.tokens(topic)))[:120] or f"manual-{topic[:40]}"
    trend, _created = await repo.upsert(
        brand=brand, cluster_key=cluster_key, headline=topic,
        score=cfg.score_threshold,  # manual trends are pre-qualified by the editor
        score_breakdown={"manual": cfg.score_threshold}, velocity=0.0,
        source_count=1, signal_count=1, covered_by_us=None,
        entities={"oems": list(detector.oems(topic))}, evidence=evidence,
        ttl_hours=cfg.ttl_hours, dedup_days=0, origin="manual",
    )
    if trend is None:  # dedup_days=0 makes this unreachable, but stay safe
        raise AdapterUnavailable("could not create manual trend")
    # A manual topic can collide with an existing trend's cluster; never regress
    # a trend that has already been actioned (approved etc.) back to 'proposed'.
    may_advance = trend.status in (TrendStatus.DETECTED.value,
                                   TrendStatus.DOSSIER_BUILDING.value,
                                   TrendStatus.PROPOSED.value)
    if cfg.auto_dossier and (may_advance or not trend.dossier):
        if may_advance:
            trend.status = TrendStatus.DOSSIER_BUILDING.value
            await ctx.session.flush()
        try:
            await collect_dossier(ctx, trend)
        except Exception as exc:  # noqa: BLE001
            log.warning("[scout] manual dossier failed: %s", exc)
    if may_advance:
        trend.status = TrendStatus.PROPOSED.value
    await ctx.session.flush()
    return trend


async def run_trend_scan(brand: str = PORTFOLIO) -> dict[str, Any]:
    """Entry point for the scheduler/CLI: one scan in its own transaction."""
    async with RunContext.open() as ctx:
        return await TrendScout(ctx).scan(brand)

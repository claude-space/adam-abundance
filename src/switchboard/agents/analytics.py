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


async def refresh_pay_baseline(ctx, brand: str, window_days: int = 180) -> bool:
    """Refresh the brand-default WriterPayBaseline row (§16.4 Phase 10c) from the
    consum table's real per-article ``WriterCost``: median USD per article and
    median USD per word over the window. Inserts a new row only when none exists
    or a rate moved >1% (rate history rides ``effective_at``). SENSITIVE data —
    written to the access-controlled table only, never to shared memory. Soft-
    fails to False when BigQuery is unavailable or the brand has no cost rows."""
    from sqlalchemy import select

    from ..adapters.analytics import _CONSUM_TABLE
    from ..adapters.base import AdapterUnavailable
    from ..adapters.clients.bigquery import BigQueryClient
    from ..db.models import WriterPayBaseline

    if brand == "portfolio":
        return False
    try:
        bc = ctx.settings.brand(brand)
        client = BigQueryClient(ctx.creds.google_sa())
    except (KeyError, AdapterUnavailable):
        return False
    sql = f"""
        SELECT APPROX_QUANTILES(WriterCost, 2)[OFFSET(1)] AS usd_per_article,
               APPROX_QUANTILES(SAFE_DIVIDE(WriterCost, NULLIF(WordCount, 0)), 2)[OFFSET(1)] AS usd_per_word,
               COUNT(*) AS n
        FROM {_CONSUM_TABLE}
        WHERE Brand=@brand AND WriterCost IS NOT NULL AND WriterCost > 0
          AND PubDate >= DATE_SUB(CURRENT_DATE('America/New_York'), INTERVAL {int(window_days)} DAY)
          AND PubDate <  CURRENT_DATE('America/New_York')
    """
    params = {"brand": bc.short_code}
    try:
        estimated = await client.estimate_bytes(sql, params)
        if not await ctx.governor.within_caps("bq_bytes", additional=estimated):
            log.info("[analytics] pay baseline skipped for %s — bq_bytes cap", brand)
            return False
        res = await client.query(sql, params)
    except Exception as exc:  # noqa: BLE001 — BQ offline/erroring → skip softly
        log.info("[analytics] pay baseline query failed for %s: %s", brand, exc)
        return False
    await ctx.governor.charge("bq_bytes", getattr(res, "bytes_processed", 0) or 0, "analytics")

    row = res.rows[0] if res.rows else {}
    per_article = row.get("usd_per_article")
    per_word = row.get("usd_per_word")
    n = int(row.get("n") or 0)
    if not n or per_article is None:
        log.info("[analytics] pay baseline: no WriterCost rows for %s in %sd", brand, window_days)
        return False
    per_article = round(float(per_article), 2)
    per_word = round(float(per_word), 4) if per_word is not None else None

    current = (await ctx.session.execute(
        select(WriterPayBaseline)
        .where(WriterPayBaseline.brand == brand, WriterPayBaseline.author.is_(None))
        .order_by(WriterPayBaseline.effective_at.desc())
        .limit(1))).scalar_one_or_none()

    def _close(a: float | None, b: float | None) -> bool:
        if a is None or b is None:
            return a == b
        return abs(a - b) <= 0.01 * max(abs(a), abs(b), 1e-9)

    if current is not None and _close(current.usd_per_article, per_article) \
            and _close(current.usd_per_word, per_word):
        return False
    ctx.session.add(WriterPayBaseline(
        brand=brand, author=None,
        usd_per_article=per_article, usd_per_word=per_word))
    await ctx.session.flush()
    log.info("[analytics] pay baseline refreshed for %s (n=%s articles, %sd window)",
             brand, n, window_days)
    return True


class AnalyticsAgent(BaseAgent):
    name = "analytics"

    async def observe(self, brand: str) -> int:
        written = await super().observe(brand)
        written += await self._rollup(brand)
        written += await self._session_trends(brand)
        written += await self._writer_stats(brand)
        written += await self._topic_demand(brand)
        written += await self._style_profile(brand)
        # Pay baseline (§16.4): sensitive → DB row only, never a memory entry,
        # so it contributes nothing to `written`.
        await refresh_pay_baseline(self.ctx, brand,
                                   self._int_cred("WRITER_PAY_WINDOW_DAYS", 180))
        return written

    def _int_cred(self, key: str, default: int) -> int:
        try:
            return int(self.ctx.creds.resolve(key, secret=False) or default)
        except (ValueError, TypeError):
            return default

    # -- topic demand (PRD §16.3) --------------------------------------------

    async def _topic_demand(self, brand: str) -> int:
        """Which content categories pull the most reader sessions for this brand
        (§16.3), from the consum article-analysis table. Snapshot replace: the
        latest set per brand is the truth. Feeds the Topic-demand panel AND the
        Trend Score's brand_demand factor. Soft-fails when BigQuery is down."""
        if brand == "portfolio":
            return 0
        from datetime import datetime, timezone

        from sqlalchemy import delete as _delete

        from ..adapters.analytics import _CONSUM_TABLE
        from ..adapters.base import AdapterUnavailable
        from ..adapters.clients.bigquery import BigQueryClient
        from ..db.models import BrandTopicDemand

        try:
            bc = self.ctx.settings.brand(brand)
            client = BigQueryClient(self.ctx.creds.google_sa())
        except (KeyError, AdapterUnavailable):
            return 0
        window_days = self._int_cred("TOPIC_DEMAND_WINDOW_DAYS", 180)
        min_articles = self._int_cred("TOPIC_DEMAND_MIN_ARTICLES", 15)
        top_n = self._int_cred("TOPIC_DEMAND_TOP_N", 12)
        sql = f"""
            SELECT PriCat AS category, COUNT(*) AS articles,
                   AVG(COALESCE(ActSessSentinel, 0)) AS avg_sessions,
                   AVG(COALESCE(RPM, 0)) AS avg_rpm
            FROM {_CONSUM_TABLE}
            WHERE Brand=@brand AND PriCat IS NOT NULL AND PriCat != ''
              AND PubDate >= DATE_SUB(CURRENT_DATE('America/New_York'), INTERVAL {window_days} DAY)
              AND PubDate <  CURRENT_DATE('America/New_York')
            GROUP BY PriCat
            HAVING COUNT(*) >= {min_articles}
            ORDER BY avg_sessions DESC
        """
        params = {"brand": bc.short_code}
        try:
            if not await self.ctx.governor.within_caps("bq_bytes", additional=await client.estimate_bytes(sql, params)):
                log.info("[analytics] topic_demand skipped for %s — bq_bytes cap", brand)
                return 0
            res = await client.query(sql, params)
        except Exception as exc:  # noqa: BLE001
            log.info("[analytics] topic_demand query failed for %s: %s", brand, exc)
            return 0
        await self.ctx.governor.charge("bq_bytes", getattr(res, "bytes_processed", 0) or 0, "analytics")

        rows = [r for r in res.rows if (r.get("avg_sessions") or 0) > 0]
        if not rows:
            return 0
        # demand_index = category avg sessions ÷ the brand's mean across categories
        # (so >1 = above-average pull). Ranked by raw avg sessions.
        mean = sum(float(r["avg_sessions"]) for r in rows) / len(rows)
        rows.sort(key=lambda r: float(r["avg_sessions"]), reverse=True)

        await self.ctx.session.execute(_delete(BrandTopicDemand).where(BrandTopicDemand.brand == brand))
        kept = rows[:top_n]
        for i, r in enumerate(kept, start=1):
            self.ctx.session.add(BrandTopicDemand(
                brand=brand, category=str(r["category"]), articles=int(r["articles"]),
                avg_sessions=round(float(r["avg_sessions"]), 1),
                avg_rpm=round(float(r["avg_rpm"]), 2) if r.get("avg_rpm") is not None else None,
                demand_index=round(float(r["avg_sessions"]) / mean, 3) if mean > 0 else 1.0,
                rank=i, window_days=window_days))
        await self.ctx.session.flush()

        await self.ctx.store.write(EntryDraft(
            type=EntryType.METRIC, brand=brand, source_agent="analytics", source_system="bigquery",
            payload={"kind": "topic_demand", "window_days": window_days,
                     "top": [{"category": str(r["category"]),
                              "avg_sessions": round(float(r["avg_sessions"]), 1)} for r in kept[:5]]},
            confidence=0.9, ttl_seconds=14 * 24 * 3600))
        log.info("[analytics] topic_demand for %s — %d categories (top: %s)",
                 brand, len(kept), kept[0]["category"] if kept else "—")
        return 1

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
                   COALESCE(ActSessSentinel, 0) AS sessions,
                   WriterCost AS cost, WordCount AS words
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
              "intent": r.get("intent"), "sessions": r.get("sessions"),
              "cost": r.get("cost"), "words": r.get("words")} for r in res.rows],
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
                norm_score=w["norm_score"], is_top=w["is_top"],
                usd_per_article=w.get("usd_per_article"), usd_per_word=w.get("usd_per_word")))
        await self.ctx.session.flush()

        await self.ctx.store.write(EntryDraft(
            type=EntryType.METRIC, brand=brand, source_agent="analytics", source_system="bigquery",
            payload={"kind": "top_writers", "window_days": window_days, "top_n": top_n,
                     "writer_count": len(ranked), "top": [w for w in ranked if w["is_top"]]},
            confidence=0.9, ttl_seconds=7 * 24 * 3600))
        return len(ranked)

    # -- style profile (PRD §16.3) -------------------------------------------

    async def _style_profile(self, brand: str) -> int:
        """Distil the brand's aggregate writer style profile: scrape the top
        writers' best recent articles, extract shared style features via the
        LLM, and persist a new active WriterStyleProfile version.

        Opt-in (``WRITER_STYLE_PROFILE_ENABLED``) because it scrapes external
        sites and spends LLM/BigQuery budget, and rate-limited to every
        ``WRITER_STYLE_REFRESH_DAYS``. Soft-fails to 0 on any missing dependency
        (flag off, BigQuery down, too few exemplars scraped, LLM unavailable) so
        it can never break the observe cycle."""
        if brand == "portfolio":
            return 0
        enabled = (self.ctx.creds.resolve("WRITER_STYLE_PROFILE_ENABLED", secret=False) or "0")
        if enabled.strip().lower() not in ("1", "true", "yes", "on"):
            return 0

        from datetime import datetime, timedelta, timezone

        from sqlalchemy import func as _func, select, update as _update

        from ..adapters.analytics import _CONSUM_TABLE
        from ..adapters.base import AdapterUnavailable
        from ..adapters.clients.bigquery import BigQueryClient
        from ..adapters.clients.llm import LLMClient
        from ..db.models import WriterStats, WriterStyleProfile
        from .. import style as style_mod

        refresh_days = self._int_cred("WRITER_STYLE_REFRESH_DAYS", 14)
        active = (await self.ctx.session.execute(
            select(WriterStyleProfile)
            .where(WriterStyleProfile.brand == brand, WriterStyleProfile.active.is_(True))
            .order_by(WriterStyleProfile.version.desc()).limit(1))).scalar_one_or_none()
        if active is not None and (datetime.now(timezone.utc) - active.created_at) < timedelta(days=refresh_days):
            return 0  # a fresh profile already exists — nothing to refresh

        top_authors = list(dict.fromkeys((await self.ctx.session.execute(
            select(WriterStats.author)
            .where(WriterStats.brand == brand, WriterStats.is_top.is_(True))
            .order_by(WriterStats.norm_score.desc()))).scalars().all()))
        if len(top_authors) < 2:
            return 0

        bc = self.ctx.settings.brand(brand)
        try:
            client = BigQueryClient(self.ctx.creds.google_sa())
        except AdapterUnavailable:
            return 0
        window_days = self._int_cred("WRITER_STATS_WINDOW_DAYS", 90)
        # Candidate exemplars ordered by RECENCY, not all-time sessions: the site
        # re-slugs articles, so old viral URLs 404, but recent ones still resolve.
        # Recent top writers' articles characterize current style just as well.
        sql = f"""
            SELECT Writer AS author, ArticleTitle AS title, URL AS url,
                   COALESCE(ActSessSentinel, 0) AS sessions
            FROM {_CONSUM_TABLE}
            WHERE Brand=@brand AND Writer IN UNNEST(@authors)
              AND URL IS NOT NULL AND URL != ''
              AND PubDate >= DATE_SUB(CURRENT_DATE('America/New_York'), INTERVAL {window_days} DAY)
              AND PubDate <  CURRENT_DATE('America/New_York')
            ORDER BY PubDate DESC LIMIT 200
        """
        params = {"brand": bc.short_code, "authors": top_authors}
        try:
            estimated = await client.estimate_bytes(sql, params)
            if not await self.ctx.governor.within_caps("bq_bytes", additional=estimated):
                log.info("[analytics] style_profile skipped for %s — bq_bytes cap", brand)
                return 0
            res = await client.query(sql, params)
        except Exception as exc:  # noqa: BLE001
            log.info("[analytics] style_profile query failed for %s: %s", brand, exc)
            return 0
        await self.ctx.governor.charge("bq_bytes", getattr(res, "bytes_processed", 0) or 0, "analytics")

        # Over-fetch candidates (a few may still 404) — _scrape_exemplars drops
        # failures and we only need >=3 to succeed.
        exemplars = style_mod.select_exemplars(
            top_authors,
            [{"author": r.get("author"), "title": r.get("title"), "url": r.get("url"),
              "sessions": r.get("sessions")} for r in res.rows],
            per_author=self._int_cred("WRITER_STYLE_PER_AUTHOR", 3),
            cap=self._int_cred("WRITER_STYLE_EXEMPLARS", 14))
        if len(exemplars) < 3:
            return 0

        scraped = await self._scrape_exemplars(exemplars)
        if len(scraped) < 3:
            log.info("[analytics] style_profile: only %d/%d exemplars scraped for %s — skip",
                     len(scraped), len(exemplars), brand)
            return 0

        max_chars = self._int_cred("WRITER_STYLE_MAX_CHARS", 2500)
        try:
            result = await LLMClient(self.ctx).complete(
                system=style_mod.STYLE_SYSTEM,
                prompt=style_mod.build_distill_prompt(brand, scraped, max_chars=max_chars),
                model=self.ctx.settings.models.default, max_tokens=1200, agent="analytics")
        except AdapterUnavailable as exc:
            log.info("[analytics] style_profile LLM unavailable for %s: %s", brand, exc)
            return 0
        features = style_mod.parse_style_features(result.text)
        if not features or not any(features.values()):
            log.info("[analytics] style_profile: empty features for %s — skip", brand)
            return 0

        next_version = int((await self.ctx.session.execute(
            select(_func.coalesce(_func.max(WriterStyleProfile.version), 0))
            .where(WriterStyleProfile.brand == brand))).scalar_one()) + 1
        await self.ctx.session.execute(
            _update(WriterStyleProfile)
            .where(WriterStyleProfile.brand == brand, WriterStyleProfile.active.is_(True))
            .values(active=False))
        used_authors = list(dict.fromkeys(e["author"] for e in scraped))
        self.ctx.session.add(WriterStyleProfile(
            brand=brand, version=next_version, source_authors=used_authors,
            features=features, active=True,
            exemplar_refs={"urls": [e["url"] for e in scraped],
                           "titles": [e["title"] for e in scraped]}))
        await self.ctx.session.flush()

        await self.ctx.store.write(EntryDraft(
            type=EntryType.METRIC, brand=brand, source_agent="analytics", source_system="switchboard",
            payload={"kind": "style_profile_updated", "version": next_version,
                     "source_authors": used_authors, "exemplars": len(scraped),
                     "features": features},
            confidence=0.85, ttl_seconds=30 * 24 * 3600))
        log.info("[analytics] style_profile v%d for %s from %d exemplars", next_version, brand, len(scraped))
        return 1

    async def distill_writer_persona(self, brand: str, author: str) -> int | None:
        """Distil a per-writer persona (§16.3) from ONE real writer's best recent
        articles: scrape their top exemplars, extract style features via the LLM,
        and upsert a WriterPersona(kind='writer'). Returns the persona id, or None
        if it couldn't be built (BQ down, too few exemplars scraped, LLM off).
        Reuses the same scrape+extract path as the aggregate house profile."""
        from datetime import datetime, timezone
        from sqlalchemy import select as _select

        from ..adapters.analytics import _CONSUM_TABLE
        from ..adapters.base import AdapterUnavailable
        from ..adapters.clients.bigquery import BigQueryClient
        from ..adapters.clients.llm import LLMClient
        from ..db.models import WriterPersona
        from .. import style as style_mod

        try:
            bc = self.ctx.settings.brand(brand)
            client = BigQueryClient(self.ctx.creds.google_sa())
        except (KeyError, AdapterUnavailable):
            return None
        window_days = self._int_cred("WRITER_STATS_WINDOW_DAYS", 90)
        # Recent-first: the writer's latest articles still resolve (older ones get
        # re-slugged → 404). Over-fetch so a few stale URLs don't sink the distill.
        sql = f"""
            SELECT Writer AS author, ArticleTitle AS title, URL AS url,
                   COALESCE(ActSessSentinel, 0) AS sessions
            FROM {_CONSUM_TABLE}
            WHERE Brand=@brand AND Writer=@author AND URL IS NOT NULL AND URL != ''
              AND PubDate >= DATE_SUB(CURRENT_DATE('America/New_York'), INTERVAL {window_days} DAY)
              AND PubDate <  CURRENT_DATE('America/New_York')
            ORDER BY PubDate DESC LIMIT 60
        """
        params = {"brand": bc.short_code, "author": author}
        try:
            if not await self.ctx.governor.within_caps("bq_bytes", additional=await client.estimate_bytes(sql, params)):
                return None
            res = await client.query(sql, params)
        except Exception as exc:  # noqa: BLE001
            log.info("[analytics] persona distill query failed for %s/%s: %s", brand, author, exc)
            return None
        await self.ctx.governor.charge("bq_bytes", getattr(res, "bytes_processed", 0) or 0, "analytics")

        exemplars = style_mod.select_exemplars(
            [author],
            [{"author": r.get("author"), "title": r.get("title"), "url": r.get("url"),
              "sessions": r.get("sessions")} for r in res.rows],
            per_author=self._int_cred("WRITER_PERSONA_EXEMPLARS", 10),
            cap=self._int_cred("WRITER_PERSONA_EXEMPLARS", 10))
        scraped = await self._scrape_exemplars(exemplars)
        if len(scraped) < 2:
            log.info("[analytics] persona distill: only %d exemplars for %s/%s", len(scraped), brand, author)
            return None
        try:
            result = await LLMClient(self.ctx).complete(
                system=style_mod.STYLE_SYSTEM,
                prompt=style_mod.build_distill_prompt(brand, scraped,
                                                      max_chars=self._int_cred("WRITER_STYLE_MAX_CHARS", 2500)),
                model=self.ctx.settings.models.default, max_tokens=1200, agent="analytics")
        except AdapterUnavailable:
            return None
        features = style_mod.parse_style_features(result.text)
        if not features or not any(features.values()):
            return None

        refs = {"urls": [e["url"] for e in scraped], "titles": [e["title"] for e in scraped]}
        existing = (await self.ctx.session.execute(
            _select(WriterPersona).where(WriterPersona.brand == brand,
                                         WriterPersona.kind == "writer",
                                         WriterPersona.name == author))).scalar_one_or_none()
        if existing is None:
            p = WriterPersona(brand=brand, kind="writer", name=author, author=author,
                              features=features, exemplar_refs=refs, enabled=True)
            self.ctx.session.add(p)
            await self.ctx.session.flush()
            pid = p.id
        else:
            existing.features = features
            existing.exemplar_refs = refs
            existing.updated_at = datetime.now(timezone.utc)
            await self.ctx.session.flush()
            pid = existing.id
        log.info("[analytics] writer persona for %s/%s (id=%s, %d exemplars)", brand, author, pid, len(scraped))
        return pid

    async def _scrape_exemplars(self, exemplars: list[dict]) -> list[dict]:
        """Fetch + extract article body text for each exemplar URL (trafilatura).
        Best-effort: drops any that error, 4xx/5xx, or come back too short to
        characterize style."""
        try:
            import httpx  # type: ignore
            import trafilatura  # type: ignore
        except ImportError:
            return []
        max_chars = self._int_cred("WRITER_STYLE_MAX_CHARS", 2500)
        headers = {"User-Agent": "Mozilla/5.0 (compatible; SwitchboardBot/1.0; +analytics)"}
        out: list[dict] = []
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True, headers=headers) as client:
            for ex in exemplars:
                # The consum table stores protocol-less URLs (e.g. "www.hotcars.com/…");
                # give httpx an absolute URL.
                url = (ex.get("url") or "").strip()
                if url and not url.startswith(("http://", "https://")):
                    url = "https://" + url.lstrip("/")
                if not url:
                    continue
                try:
                    resp = await client.get(url)
                    if resp.status_code >= 400:
                        continue
                    text = (trafilatura.extract(resp.text, include_comments=False,
                                                include_tables=False) or "").strip()
                except Exception as exc:  # noqa: BLE001
                    log.debug("[analytics] scrape failed %s: %s", url, exc)
                    continue
                if len(text) < 400:   # too short to say anything about style
                    continue
                out.append({**ex, "url": url, "text": text[:max_chars]})
        return out

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

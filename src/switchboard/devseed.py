"""Dev seed: inject representative memory entries so the full pipeline
(observe → plan → approve → dispatch) can be exercised offline, with no live
external systems. For local development/demo only — every entry is provenance-
tagged like the real thing so the planner, governor, and approval surface behave
exactly as they would in production.
"""

from __future__ import annotations


async def seed_brand(brand: str) -> int:  # pragma: no cover - dev utility
    from .context import RunContext
    from .db.enums import EntryType
    from .interfaces import EntryDraft

    drafts: list[EntryDraft] = [
        EntryDraft(type=EntryType.METRIC, brand=brand, source_agent="analytics", source_system="bigquery",
                   payload={"kind": "writer_performance", "period": "MTD", "brand_avg_spa": 1800,
                            "writers": [{"writer": "A. Writer", "articles": 12, "sessions": 30000,
                                         "sessions_per_article": 2500, "relative_index": 1.39},
                                        {"writer": "B. Writer", "articles": 9, "sessions": 9000,
                                         "sessions_per_article": 1000, "relative_index": 0.56}]},
                   confidence=0.9),
        EntryDraft(type=EntryType.METRIC, brand=brand, source_agent="analytics", source_system="bigquery",
                   payload={"kind": "top_articles", "window_days": 2,
                            "articles": [{"title": "Best trucks 2026", "sessions": 41000},
                                         {"title": "EV range kings", "sessions": 33000}]}, confidence=0.9),
        EntryDraft(type=EntryType.METRIC, brand=brand, source_agent="analytics", source_system="sentinel",
                   payload={"kind": "sessions_daily", "date": "2026-07-12", "visits": 512340}, confidence=0.95),
        EntryDraft(type=EntryType.METRIC, brand=brand, source_agent="production", source_system="hc_viral_hits",
                   payload={"kind": "hc_viral_queue", "ready_count": 11,
                            "ready_topic_ids": [101, 102, 103]}, confidence=0.95),
        EntryDraft(type=EntryType.FLAG, brand=brand, source_agent="analytics", source_system="bigquery",
                   payload={"kind": "writer_below_index", "writer": "B. Writer", "relative_index": 0.56,
                            "articles": 9, "severity": "medium"}),
        EntryDraft(type=EntryType.FLAG, brand=brand, source_agent="production", source_system="asana",
                   payload={"kind": "overdue_outlines", "count": 4, "severity": "high"}),
        EntryDraft(type=EntryType.FLAG, brand=brand, source_agent="production", source_system="hc_viral_hits",
                   payload={"kind": "emaki_backlog", "ready_count": 11, "severity": "medium"}),
        EntryDraft(type=EntryType.FLAG, brand=brand, source_agent="decay_scan", source_system="seona",
                   payload={"kind": "decay_candidate", "url": f"https://www.{brand}.com/some-post",
                            "pos_delta": 3.1, "click_ratio": 0.55, "severity": "medium"},
                   source_urls=[f"https://www.{brand}.com/some-post"]),
        EntryDraft(type=EntryType.FLAG, brand=brand, source_agent="content_audit",
                   source_system="content_depth_auditor",
                   payload={"kind": "content_audit_finding", "url": f"https://www.{brand}.com/thin-post",
                            "depth_pct": 22, "avd_seconds": 18, "severity": "medium"},
                   source_urls=[f"https://www.{brand}.com/thin-post"]),
        EntryDraft(type=EntryType.CONTEXT, brand=brand, source_agent="opportunity", source_system="hc_viral_hits",
                   payload={"kind": "viral_topic_candidate", "topic_id": 101,
                            "title": "Why this new EV is going viral", "status": "ready"}),
        EntryDraft(type=EntryType.CONTEXT, brand=brand, source_agent="opportunity", source_system="claude_albert",
                   payload={"kind": "topic_candidate", "topic_id": "alb-55",
                            "title": "2026 truck towing comparison", "status": "proposed"}),
        EntryDraft(type=EntryType.REPORT, brand=brand, source_agent="reporting", source_system="daily_reporting",
                   payload={"kind": "daily_digest_inputs", "ready": True,
                            "inputs": {"has_sessions": True, "has_top_articles": True}}),
        EntryDraft(type=EntryType.CLAIM, brand=brand, source_agent="opportunity", source_system="ahrefs",
                   payload={"kind": "keyword_gap", "statement": "Competitor ranks #1 for 'best hybrid SUV 2026'",
                            "needs_verification": True}),
        EntryDraft(type=EntryType.DISTRIBUTION_DRAFT, brand=brand, source_agent="reporting",
                   source_system="social",
                   payload={"kind": "social_draft", "status": "inputs_ready", "artifact_ref": None}),
    ]
    if brand == "carbuzz":
        drafts.append(EntryDraft(type=EntryType.DISTRIBUTION_DRAFT, brand=brand, source_agent="reporting",
                                 source_system="newsletter",
                                 payload={"kind": "newsletter_draft", "status": "inputs_ready",
                                          "artifact_ref": None}))
    # Trend-pipeline vocabulary (docs/trend-pipeline.md): raw signals the scout
    # clusters, plus the flag the scout raises for the morning planner.
    drafts += [
        EntryDraft(type=EntryType.CONTEXT, brand="portfolio", source_agent="research",
                   source_system="tavily",
                   payload={"kind": "trend_signals", "origin": "tavily", "item_count": 2,
                            "items": [
                                {"origin": "tavily", "source": "caranddriver.com",
                                 "title": "Tesla recalls 300k Model Y over steering fault",
                                 "url": "https://example.com/tesla-recall-cd",
                                 "published_at": "2026-07-13T09:00:00+00:00", "snippet": ""},
                                {"origin": "tavily", "source": "motor1.com",
                                 "title": "Tesla Model Y recall: what owners need to know",
                                 "url": "https://example.com/tesla-recall-m1",
                                 "published_at": "2026-07-13T10:30:00+00:00", "snippet": ""},
                            ]},
                   ttl_seconds=24 * 3600),
        EntryDraft(type=EntryType.FLAG, brand=brand, source_agent="trend_scout",
                   source_system="trend_scan",
                   payload={"kind": "competitor_trend", "trend_id": 0,
                            "headline": "Tesla recalls 300k Model Y over steering fault",
                            "score": 74, "severity": "high"},
                   source_urls=["https://example.com/tesla-recall-cd"]),
        EntryDraft(type=EntryType.CLAIM, brand=brand, source_agent="trend_scout",
                   source_system="trend_dossier",
                   payload={"kind": "trend_key_fact", "trend_id": 0,
                            "statement": "The recall covers 300,000 Model Y vehicles built 2024-2026",
                            "needs_verification": True},
                   source_urls=["https://example.com/tesla-recall-cd"], confidence=0.5),
    ]
    async with RunContext.open() as ctx:
        rows = await ctx.store.write_many(drafts)
    return len(rows)

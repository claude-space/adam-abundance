"""Registry mapping each worker agent to the adapter classes it owns.

This is also the *enforcement point* for the no-overlap rule (PRD §2, §6): each
adapter appears under exactly one owner. An agent builds only its own adapters
(``build_adapters(agent_name, ctx)``) and therefore cannot touch another domain's
tools.
"""

from __future__ import annotations

from typing import Type

from ..context import RunContext
from .analytics import (
    BigQueryConsumAdapter,
    BigQueryDiscoverAdapter,
    SentinelTrafficAdapter,
    SheetsQuotaAdapter,
)
from .base import BaseAdapter
from .opportunity import (
    AhrefsAdapter,
    AlbertIdeationAdapter,
    GSCAdapter,
    HCViralIdeationAdapter,
    SeonaIdeationAdapter,
)
from .paid_media import (
    BingAdsAdapter,
    GoogleAdsAdapter,
    LeadFeedsAdapter,
    MetaAdsAdapter,
    PaidMediaSheetAdapter,
    SentinelEventsAdapter,
)
from .actions import (
    AlbertRouteToWriterAdapter,
    AsanaTaskAdapter,
    DigestAssembleAdapter,
    DigestSendAdapter,
    EmakiPublishAdapter,
    IdeationTriggerAdapter,
    NewsletterAssembleAdapter,
    SeonaDecayRefreshAdapter,
    SocialAssembleAdapter,
)
from .production import (
    AlbertWriterQueueAdapter,
    AsanaAdapter,
    HCViralDraftQueueAdapter,
    OutlineReviewAdapter,
)
from .research import CompetitorNewsAdapter, SimilarwebAdapter
from .trend_sources import (
    FirecrawlTrendAdapter,
    NewsApiTrendAdapter,
    PerplexityTrendAdapter,
    SemrushTrendAdapter,
    TavilyTrendAdapter,
    XTrendAdapter,
    YouTubeTrendAdapter,
)

# agent name -> adapter classes it owns
REGISTRY: dict[str, list[Type[BaseAdapter]]] = {
    "research": [
        SimilarwebAdapter,
        CompetitorNewsAdapter,
        # Trend-pipeline sources: portfolio-scoped, no-op during per-brand
        # observe; the trend_scan feeder drives them (docs/trend-pipeline.md).
        TavilyTrendAdapter,
        NewsApiTrendAdapter,
        FirecrawlTrendAdapter,
        PerplexityTrendAdapter,
        YouTubeTrendAdapter,
        XTrendAdapter,
        SemrushTrendAdapter,
    ],
    "opportunity": [
        AhrefsAdapter,
        GSCAdapter,
        HCViralIdeationAdapter,
        AlbertIdeationAdapter,
        SeonaIdeationAdapter,
    ],
    "production": [
        AsanaAdapter,
        HCViralDraftQueueAdapter,
        AlbertWriterQueueAdapter,
        OutlineReviewAdapter,
    ],
    "analytics": [
        BigQueryConsumAdapter,
        BigQueryDiscoverAdapter,
        SentinelTrafficAdapter,
        SheetsQuotaAdapter,
    ],
    # Reporting & Distribution's Phase-1 inputs come from memory (Analytics +
    # Research); its owned systems are exercised as Phase-4 assembly actions.
    "reporting": [],
    "paid_media": [
        PaidMediaSheetAdapter,
        GoogleAdsAdapter,
        MetaAdsAdapter,
        BingAdsAdapter,
        SentinelEventsAdapter,
        LeadFeedsAdapter,
    ],
}


def build_adapters(agent_name: str, ctx: RunContext) -> list[BaseAdapter]:
    return [cls(ctx) for cls in REGISTRY.get(agent_name, [])]


def owned_tool_names(agent_name: str) -> list[str]:
    return [cls.name for cls in REGISTRY.get(agent_name, [])]


# action_type -> action adapter class (Phase 4). Each adapter's ``owner_agent``
# must match the agent dispatched to run it (enforced in BaseAgent.execute).
ACTION_REGISTRY: dict[str, Type[BaseAdapter]] = {
    "trigger_ideation": IdeationTriggerAdapter,
    "create_asana_task": AsanaTaskAdapter,
    "route_to_writer": AlbertRouteToWriterAdapter,
    "queue_decay_refresh": SeonaDecayRefreshAdapter,
    "emaki_publish_draft": EmakiPublishAdapter,
    "assemble_digest": DigestAssembleAdapter,
    "send_digest_email": DigestSendAdapter,
    "assemble_newsletter": NewsletterAssembleAdapter,
    "assemble_social_post": SocialAssembleAdapter,
}


def build_action_adapter(action_type: str, ctx: RunContext) -> BaseAdapter | None:
    cls = ACTION_REGISTRY.get(action_type)
    return cls(ctx) if cls else None

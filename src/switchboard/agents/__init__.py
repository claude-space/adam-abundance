"""The six worker agents (PRD §6). Each owns one data domain, is stateless
between runs, and touches only shared memory + its own adapters — never another
agent or another agent's tools (PRD §5, §14).
"""

from __future__ import annotations

from ..context import RunContext
from ..logging_ import get_logger
from .analytics import AnalyticsAgent
from .base import BaseAgent
from .opportunity import OpportunityAgent
from .paid_media import PaidMediaAgent
from .production import ProductionAgent
from .reporting import ReportingAgent
from .research import ResearchAgent

log = get_logger("agents")

_AGENT_CLASSES: dict[str, type[BaseAgent]] = {
    "research": ResearchAgent,
    "analytics": AnalyticsAgent,
    "opportunity": OpportunityAgent,
    "production": ProductionAgent,
    "paid_media": PaidMediaAgent,
    "reporting": ReportingAgent,
}

# Observe order: context/measurement first; Reporting last (it reads the others'
# memory rather than re-querying).
AGENT_ORDER = ["research", "analytics", "opportunity", "production", "paid_media", "reporting"]


def build_agent(name: str, ctx: RunContext) -> BaseAgent:
    if name not in _AGENT_CLASSES:
        raise KeyError(f"Unknown agent '{name}'")
    return _AGENT_CLASSES[name](ctx)


def all_agents(ctx: RunContext) -> list[BaseAgent]:
    return [build_agent(n, ctx) for n in AGENT_ORDER]


async def run_all_observe(brand: str) -> dict[str, str]:
    """Run every agent's observe pass for a brand. Each agent runs in its own
    transaction so one failure doesn't roll back another's findings."""
    results: dict[str, str] = {}
    for name in AGENT_ORDER:
        try:
            async with RunContext.open() as ctx:
                await build_agent(name, ctx).observe(brand)
            results[name] = "ok"
        except Exception as exc:  # noqa: BLE001
            log.exception("agent %s observe failed for %s", name, brand)
            results[name] = f"error: {exc}"
    return results


__all__ = [
    "BaseAgent",
    "ResearchAgent",
    "AnalyticsAgent",
    "OpportunityAgent",
    "ProductionAgent",
    "PaidMediaAgent",
    "ReportingAgent",
    "build_agent",
    "all_agents",
    "run_all_observe",
    "AGENT_ORDER",
]

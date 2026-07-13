"""Orchestration plane (PRD §5, §6.1): the chief-of-staff cycle.

Each morning: run every agent's observe pass → synthesize a prioritized draft
plan → surface it for human approval → on approval, dispatch approved items
through the governor to the assigned agents. The orchestrator does no domain work
and holds the governor on the dispatch path.
"""

from .cycle import run_morning_cycle
from .planner import Planner
from .plans import PlanRepo

__all__ = ["run_morning_cycle", "Planner", "PlanRepo"]

"""Production agent (PRD §6.4): pipeline state across both writing pipelines
(Claude Albert + HC Viral Hits) and Asana. Runs its read adapters in observe;
executes approved production actions in Phase 4."""

from __future__ import annotations

from .base import BaseAgent


class ProductionAgent(BaseAgent):
    name = "production"

"""Component contracts (PRD §10). Small, explicit, and decoupled from the DB
session so adapters/agents can be unit-tested against plain data.

Two DTOs cross the boundary between "produce data" and "persist data":

* :class:`EntryDraft` — what an adapter's ``observe()`` / an agent returns; the
  memory store turns it into a ``memory_entry`` row (assigning TTL, provenance).
* :class:`PlanItemView` — an immutable snapshot of a ``plan_item`` row handed to
  ``act()`` / ``execute()`` so a side-effecting call never mutates the row
  directly; it returns an :class:`ActionResult` the caller records.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from .db.enums import EntryType


class CostSpec(BaseModel):
    """The three metered resources, plus a derived USD figure for logs/UI.
    Mirrors the ``cost_estimate`` / ``tool_call_log.cost`` JSON shapes."""

    ahrefs_units: int = 0
    llm_micros: int = 0
    bq_bytes: int = 0
    usd: float | None = None

    def merge(self, other: "CostSpec") -> "CostSpec":
        return CostSpec(
            ahrefs_units=self.ahrefs_units + other.ahrefs_units,
            llm_micros=self.llm_micros + other.llm_micros,
            bq_bytes=self.bq_bytes + other.bq_bytes,
            usd=(self.usd or 0.0) + (other.usd or 0.0) if (self.usd or other.usd) else None,
        )


class EntryDraft(BaseModel):
    """A typed memory entry an adapter/agent wants persisted.

    ``verified=True`` is a *request*; the memory store / governor downgrades a
    ``fact`` to a ``claim`` unless the Research fact-gate cleared it (PRD §8).
    """

    type: EntryType
    brand: str
    source_agent: str
    source_system: str | None = None
    payload: dict[str, Any]
    verified: bool = False
    confidence: float | None = None
    source_urls: list[str] | None = None
    # Provide either an absolute expiry or a relative TTL; the store resolves it.
    expires_at: datetime | None = None
    ttl_seconds: int | None = None
    status: str = "active"


class PlanItemView(BaseModel):
    """Read-only snapshot of a ``plan_item`` passed into a side-effecting call."""

    model_config = ConfigDict(frozen=True)

    id: int | None = None
    plan_id: int | None = None
    rank: int = 0
    assigned_agent: str
    action_type: str
    params: dict[str, Any] = Field(default_factory=dict)
    rationale: str | None = None
    status: str = "proposed"
    dry_run: bool = True
    brand: str = "portfolio"
    cost_estimate: dict[str, Any] | None = None


class ActionResult(BaseModel):
    """Outcome of an ``act()`` / ``execute()`` call."""

    ok: bool
    dry_run: bool
    action_type: str
    summary: str = ""
    result_ref: dict[str, Any] = Field(default_factory=dict)
    cost: CostSpec = Field(default_factory=CostSpec)
    error: str | None = None
    # Entries the action wants written back to memory (e.g. a report/draft row).
    entries: list[EntryDraft] = Field(default_factory=list)


class Decision(BaseModel):
    """Governor verdict on whether an action may proceed (PRD §8, §10)."""

    allowed: bool
    reason: str = ""
    # The dry_run the action MUST run with if allowed. The governor forces
    # dry_run=True unless the item is approved and live has been authorized.
    dry_run: bool = True
    violations: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Protocols
# ---------------------------------------------------------------------------


@runtime_checkable
class ToolAdapter(Protocol):
    """Wraps one existing system or external API (PRD §10)."""

    name: str

    async def observe(self, brand: str, **kwargs: Any) -> list[EntryDraft]:
        """Read from the underlying system; return typed entries to persist.
        Logs a 'read' tool_call_log row. MUST NOT perform side effects."""
        ...

    async def act(self, item: PlanItemView, *, dry_run: bool) -> ActionResult:
        """Perform a side effect for an approved plan_item. Governor-gated by the
        caller; logs an 'act' tool_call_log row. MUST honor dry_run (log-only)."""
        ...


@runtime_checkable
class Agent(Protocol):
    """A worker agent. Stateless between runs; all state lives in shared memory.
    An agent touches ONLY shared memory + its own owned adapters — never another
    agent or another agent's tools (PRD §5, §14)."""

    name: str
    owned_tools: list[str]

    async def observe(self, brand: str) -> None:
        """Query owned adapters + memory; write findings/flags/facts to memory."""

    async def execute(self, item: PlanItemView) -> ActionResult:
        """Run one approved plan_item via an owned adapter (governor-gated)."""


@runtime_checkable
class Governor(Protocol):
    """Policy component invoked by the orchestrator on dispatch and by every
    action adapter (PRD §8, §10)."""

    def check_action(self, item: PlanItemView) -> Decision: ...

    async def charge(self, metric: str, amount: int, agent: str) -> None: ...

    async def within_caps(self, metric: str, *, additional: int = 0) -> bool: ...

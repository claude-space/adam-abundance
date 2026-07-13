"""The shared-memory access layer.

All cross-agent coordination flows through this class. It enforces two of the
PRD's load-bearing invariants at the persistence boundary:

* **Provenance / fact-gate (PRD §8).** A ``verified=True`` entry is only written
  when the caller presents ``fact_gate_ok=True`` (the Research agent sets this
  after search-confirmation). Otherwise ``verified`` is forced to ``False`` and a
  ``fact`` is downgraded to a ``claim``.
* **Freshness / TTL (PRD §7.1).** Every entry gets an ``expires_at`` (from an
  explicit value, a relative ``ttl_seconds``, or the per-type default), and
  :meth:`expire_stale` sweeps expired rows to ``status='expired'``.

A store instance wraps one :class:`AsyncSession`; use it inside ``session_scope``
so writes commit/rollback atomically.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Sequence

from sqlalchemy import and_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..db.enums import EntryStatus, EntryType
from ..db.models import MemoryEntry, ToolCallLog
from ..interfaces import CostSpec, EntryDraft
from ..logging_ import get_logger, redact

log = get_logger("memory")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _redact_json(obj: Any) -> Any:
    """Deep-redact a JSON-able structure by scrubbing secrets from its rendered
    form, then re-parsing. Guarantees no credential lands in ``tool_call_log``."""
    if obj is None:
        return None
    try:
        return json.loads(redact(json.dumps(obj, default=str)))
    except (TypeError, ValueError):
        return {"redacted_repr": redact(str(obj))}


class MemoryStore:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self._settings = get_settings()

    # -- writes ---------------------------------------------------------------

    def _resolve_expiry(self, draft: EntryDraft) -> datetime | None:
        if draft.expires_at is not None:
            return draft.expires_at
        if draft.ttl_seconds is not None:
            return _utcnow() + timedelta(seconds=draft.ttl_seconds)
        ttl = self._settings.ttl_for(draft.type.value)
        return _utcnow() + timedelta(seconds=ttl) if ttl else None

    def _apply_fact_gate(self, draft: EntryDraft, fact_gate_ok: bool) -> tuple[EntryType, bool]:
        """Enforce the provenance rule. Returns the (possibly downgraded) type +
        verified flag."""
        etype, verified = draft.type, draft.verified
        if verified and not fact_gate_ok:
            log.info(
                "Downgrading unverified %s from %s to claim (fact-gate not cleared)",
                draft.type.value,
                draft.source_agent,
            )
            verified = False
            if etype == EntryType.FACT:
                etype = EntryType.CLAIM
        return etype, verified

    async def write(self, draft: EntryDraft, *, fact_gate_ok: bool = False) -> MemoryEntry:
        if not self._settings.is_valid_scope(draft.brand):
            raise ValueError(f"Invalid brand scope '{draft.brand}'")
        etype, verified = self._apply_fact_gate(draft, fact_gate_ok)
        entry = MemoryEntry(
            type=etype,
            brand=draft.brand,
            source_agent=draft.source_agent,
            source_system=draft.source_system,
            payload=draft.payload,
            verified=verified,
            confidence=draft.confidence,
            source_urls=draft.source_urls,
            expires_at=self._resolve_expiry(draft),
            status=draft.status or EntryStatus.ACTIVE.value,
        )
        self.session.add(entry)
        await self.session.flush()
        return entry

    async def write_many(
        self, drafts: Iterable[EntryDraft], *, fact_gate_ok: bool = False
    ) -> list[MemoryEntry]:
        return [await self.write(d, fact_gate_ok=fact_gate_ok) for d in drafts]

    async def supersede(self, entry_ids: Sequence[int]) -> int:
        if not entry_ids:
            return 0
        result = await self.session.execute(
            update(MemoryEntry)
            .where(MemoryEntry.id.in_(entry_ids))
            .values(status=EntryStatus.SUPERSEDED.value)
        )
        return result.rowcount or 0

    # -- reads ----------------------------------------------------------------

    async def query(
        self,
        *,
        brand: str | None = None,
        include_portfolio: bool = True,
        types: Sequence[EntryType] | None = None,
        source_agent: str | None = None,
        source_system: str | None = None,
        verified: bool | None = None,
        since: datetime | None = None,
        fresh_within_seconds: int | None = None,
        payload_contains: dict[str, Any] | None = None,
        status: str | None = EntryStatus.ACTIVE.value,
        limit: int = 200,
    ) -> list[MemoryEntry]:
        """Precise, scoped read. Agents query for what they need — never a
        firehose. ``brand`` also matches ``portfolio`` entries unless
        ``include_portfolio=False``."""
        conds = []
        if brand is not None:
            if include_portfolio and brand != "portfolio":
                conds.append(MemoryEntry.brand.in_([brand, "portfolio"]))
            else:
                conds.append(MemoryEntry.brand == brand)
        if types:
            conds.append(MemoryEntry.type.in_(list(types)))
        if source_agent is not None:
            conds.append(MemoryEntry.source_agent == source_agent)
        if source_system is not None:
            conds.append(MemoryEntry.source_system == source_system)
        if verified is not None:
            conds.append(MemoryEntry.verified.is_(verified))
        if status is not None:
            conds.append(MemoryEntry.status == status)
        if since is not None:
            conds.append(MemoryEntry.created_at >= since)
        if fresh_within_seconds is not None:
            conds.append(MemoryEntry.created_at >= _utcnow() - timedelta(seconds=fresh_within_seconds))
        if payload_contains is not None:
            # JSONB containment (@>) — uses the GIN index.
            conds.append(MemoryEntry.payload.contains(payload_contains))

        stmt = (
            select(MemoryEntry)
            .where(and_(*conds) if conds else True)
            .order_by(MemoryEntry.created_at.desc())
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def latest(
        self, *, brand: str, type: EntryType, source_system: str | None = None
    ) -> MemoryEntry | None:
        rows = await self.query(
            brand=brand, types=[type], source_system=source_system, limit=1
        )
        return rows[0] if rows else None

    # -- TTL sweep (PRD §7.1) -------------------------------------------------

    async def expire_stale(self) -> int:
        """Mark active entries whose ``expires_at`` has passed as ``expired``.
        Returns the number swept."""
        result = await self.session.execute(
            update(MemoryEntry)
            .where(
                and_(
                    MemoryEntry.status == EntryStatus.ACTIVE.value,
                    MemoryEntry.expires_at.is_not(None),
                    MemoryEntry.expires_at < _utcnow(),
                )
            )
            .values(status=EntryStatus.EXPIRED.value)
        )
        swept = result.rowcount or 0
        if swept:
            log.info("TTL sweep expired %d stale memory entries", swept)
        return swept

    async def supersede_duplicates(self) -> int:
        """Keep only the latest active snapshot per (brand, type, source_system,
        payload.kind) for snapshot-style types; mark older ones superseded. Flags,
        facts, claims, decisions, and plan_items are never touched (they aren't
        snapshots)."""
        from sqlalchemy import text

        sql = text(
            """
            UPDATE memory_entry SET status = 'superseded'
            WHERE status = 'active'
              AND type IN ('metric','context','report','distribution_draft')
              AND id NOT IN (
                SELECT DISTINCT ON (brand, type, source_system, (payload->>'kind')) id
                FROM memory_entry
                WHERE status = 'active'
                  AND type IN ('metric','context','report','distribution_draft')
                ORDER BY brand, type, source_system, (payload->>'kind'), created_at DESC
              )
            """
        )
        result = await self.session.execute(sql)
        swept = result.rowcount or 0
        if swept:
            log.info("superseded %d duplicate snapshot entries", swept)
        return swept

    # -- audit ----------------------------------------------------------------

    async def log_tool_call(
        self,
        *,
        agent: str,
        tool: str,
        action: str,
        dry_run: bool,
        brand: str | None = None,
        request: dict[str, Any] | None = None,
        ok: bool | None = None,
        cost: CostSpec | dict[str, Any] | None = None,
    ) -> ToolCallLog:
        """Persist a per-call audit row. ``request`` is deep-redacted first so no
        secret is ever written (PRD §8)."""
        cost_json: dict[str, Any] | None
        if isinstance(cost, CostSpec):
            cost_json = cost.model_dump()
        else:
            cost_json = cost
        row = ToolCallLog(
            agent=agent,
            tool=tool,
            action=action,
            brand=brand,
            dry_run=dry_run,
            request=_redact_json(request),
            ok=ok,
            cost=cost_json,
        )
        self.session.add(row)
        await self.session.flush()
        return row

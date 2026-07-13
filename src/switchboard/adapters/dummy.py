"""A dummy read adapter used by ``switchboard selfcheck`` to satisfy the Phase 0
acceptance criterion ("a dummy adapter can write/read a memory_entry"). It has no
``_act``, so it is structurally read-only."""

from __future__ import annotations

from typing import Any

from ..db.enums import EntryType
from ..interfaces import CostSpec, EntryDraft
from .base import BaseAdapter


class DummyAdapter(BaseAdapter):
    name = "dummy"
    source_system = "dummy"
    owner_agent = "system"

    async def _observe(self, brand: str, **kwargs: Any) -> tuple[list[EntryDraft], CostSpec]:
        draft = EntryDraft(
            type=EntryType.METRIC,
            brand=brand,
            source_agent="system",
            source_system=self.source_system,
            payload={"kind": "healthcheck", "value": 1, "note": "dummy adapter observe()"},
            confidence=1.0,
            ttl_seconds=3600,
        )
        return [draft], CostSpec()

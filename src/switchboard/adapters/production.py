"""Production-domain read adapters (PRD §6.4): Asana (tasks + outline-approval
workflow) and the HC Viral Hits draft queue. Albert AI-writer/outline-review
reads are added once that system's API surface is confirmed. All observe-only;
the write actions (create task, route to writer, Emaki push) are Phase 4.
"""

from __future__ import annotations

from typing import Any

from ..db.enums import EntryType
from ..interfaces import CostSpec, EntryDraft
from ..logging_ import get_logger
from ._http import get_json
from .base import AdapterUnavailable, BaseAdapter
from .clients.hcviral import HCViralClient

log = get_logger("adapter.production")

_ASANA_BASE = "https://app.asana.com/api/1.0"


class AsanaAdapter(BaseAdapter):
    """Reads outline-approval + task state per brand. Project/section GIDs come
    from config (``ASANA_PROJECT_<BRAND>`` / ``ASANA_SECTION_OUTLINE_<BRAND>``);
    absent config degrades softly."""

    name = "asana"
    source_system = "asana"
    owner_agent = "production"

    async def _observe(self, brand: str, **kwargs: Any) -> tuple[list[EntryDraft], CostSpec]:
        pat = self.ctx.creds.asana_pat()
        if not pat:
            raise AdapterUnavailable("ASANA_PAT not configured")
        section_gid = self.ctx.creds.resolve(f"ASANA_SECTION_OUTLINE_{brand.upper()}", secret=False)
        project_gid = self.ctx.creds.resolve(f"ASANA_PROJECT_{brand.upper()}", secret=False)
        if not (section_gid or project_gid):
            raise AdapterUnavailable(f"No Asana GID configured for {brand}")

        try:
            import httpx  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise AdapterUnavailable("httpx not installed") from exc

        headers = {"Authorization": f"Bearer {pat}", "Accept": "application/json"}
        fields = "completed,name,due_on,assignee.name,memberships.section.name,modified_at"
        if section_gid:
            url = f"{_ASANA_BASE}/sections/{section_gid}/tasks"
        else:
            url = f"{_ASANA_BASE}/projects/{project_gid}/tasks"
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(url, params={"opt_fields": fields, "limit": 100}, headers=headers)
            resp.raise_for_status()
            tasks = resp.json().get("data", [])

        incomplete = [t for t in tasks if not t.get("completed")]
        overdue = [t for t in incomplete if t.get("due_on") and t["due_on"] < _today()]
        drafts: list[EntryDraft] = [
            EntryDraft(type=EntryType.METRIC, brand=brand, source_agent="production",
                       source_system="asana",
                       payload={"kind": "outline_queue", "total": len(tasks),
                                "incomplete": len(incomplete), "overdue": len(overdue),
                                "tasks": [{"name": t.get("name"), "due_on": t.get("due_on"),
                                           "assignee": (t.get("assignee") or {}).get("name")}
                                          for t in incomplete[:25]]},
                       confidence=0.95)
        ]
        if overdue:
            drafts.append(EntryDraft(type=EntryType.FLAG, brand=brand, source_agent="production",
                                     source_system="asana",
                                     payload={"kind": "overdue_outlines", "count": len(overdue),
                                              "severity": "high" if len(overdue) > 3 else "medium"}))
        return drafts, CostSpec()


class HCViralDraftQueueAdapter(BaseAdapter):
    """HC Viral Hits draft queue depth by status (pipeline state)."""

    name = "hc_viral_queue"
    source_system = "hc_viral_hits"
    owner_agent = "production"

    async def _observe(self, brand: str, **kwargs: Any) -> tuple[list[EntryDraft], CostSpec]:
        if brand not in ("hotcars", "topspeed"):
            raise AdapterUnavailable("HC Viral Hits serves hotcars + topspeed(-moto) only")
        client = HCViralClient(self.ctx.settings.endpoints["hc_viral_hits"],
                               self.ctx.creds.resolve("HC_VIRAL_HITS_API_KEY"))
        ready = await client.list_drafts(brand, status="ready")
        drafts = [
            EntryDraft(type=EntryType.METRIC, brand=brand, source_agent="production",
                       source_system="hc_viral_hits",
                       payload={"kind": "hc_viral_queue", "ready_count": len(ready),
                                "ready_topic_ids": [d.get("topic_id") or d.get("id") for d in ready][:50]},
                       confidence=0.95)
        ]
        if len(ready) >= 10:
            drafts.append(EntryDraft(type=EntryType.FLAG, brand=brand, source_agent="production",
                                     source_system="hc_viral_hits",
                                     payload={"kind": "emaki_backlog", "ready_count": len(ready),
                                              "severity": "medium"}))
        return drafts, CostSpec()


class AlbertWriterQueueAdapter(BaseAdapter):
    """Claude Albert AI-writer queue depth by draft state
    (queued→researching→writing→fact_checking→editing→ready→published/failed).
    Read path is env-configurable (ALBERT_WRITER_PATH); degrades softly."""

    name = "albert_writer_queue"
    source_system = "claude_albert"
    owner_agent = "production"

    async def _observe(self, brand: str, **kwargs: Any) -> tuple[list[EntryDraft], CostSpec]:
        base = self.ctx.settings.endpoints.get("albert")
        if not base:
            raise AdapterUnavailable("albert endpoint not configured")
        path = self.ctx.creds.resolve("ALBERT_WRITER_PATH", secret=False) or "/api/writer/queue"
        data = await get_json(base, path, params={"brand": brand})
        items = data if isinstance(data, list) else data.get("items", data.get("drafts", []))
        by_state: dict[str, int] = {}
        for it in items or []:
            by_state[it.get("state", "unknown")] = by_state.get(it.get("state", "unknown"), 0) + 1
        drafts = [EntryDraft(type=EntryType.METRIC, brand=brand, source_agent="production",
                             source_system="claude_albert",
                             payload={"kind": "albert_writer_queue", "by_state": by_state,
                                      "total": len(items or [])}, confidence=0.9)]
        if by_state.get("failed"):
            drafts.append(EntryDraft(type=EntryType.FLAG, brand=brand, source_agent="production",
                                     source_system="claude_albert",
                                     payload={"kind": "writer_failures", "count": by_state["failed"],
                                              "severity": "high"}))
        return drafts, CostSpec()


class OutlineReviewAdapter(BaseAdapter):
    """Claude Albert Outline Reviewer queue/verdicts. Surfaces stuck outlines in
    the "Outline Approval Request" workflow. Env-configurable path; degrades
    softly. (The reviewer itself defaults to dry-run via
    ALBERT_OUTLINE_REVIEWER_DRY_RUN — Switchboard only reads its state.)"""

    name = "outline_review"
    source_system = "claude_albert"
    owner_agent = "production"

    async def _observe(self, brand: str, **kwargs: Any) -> tuple[list[EntryDraft], CostSpec]:
        base = self.ctx.settings.endpoints.get("albert")
        if not base:
            raise AdapterUnavailable("albert endpoint not configured")
        path = self.ctx.creds.resolve("ALBERT_OUTLINE_PATH", secret=False) or "/api/outline-review/status"
        data = await get_json(base, path, params={"brand": brand})
        pending = data.get("pending", data.get("queue_depth", 0)) if isinstance(data, dict) else len(data)
        drafts = [EntryDraft(type=EntryType.METRIC, brand=brand, source_agent="production",
                             source_system="claude_albert",
                             payload={"kind": "outline_review", "pending": pending,
                                      "detail": data if isinstance(data, dict) else {}}, confidence=0.9)]
        if isinstance(pending, int) and pending > 5:
            drafts.append(EntryDraft(type=EntryType.FLAG, brand=brand, source_agent="production",
                                     source_system="claude_albert",
                                     payload={"kind": "stuck_outlines", "pending": pending,
                                              "severity": "medium"}))
        return drafts, CostSpec()


def _today() -> str:
    from datetime import date

    return date.today().isoformat()

"""The planner (PRD §6.1): read shared memory → synthesize a prioritized draft
plan of ``plan_item``s with rationales and cost estimates.

Deterministic at its core (so it runs without any LLM), it turns flags,
opportunity candidates, and distribution readiness into ranked, governor-priced
proposals. An optional LLM pass writes a human-readable brief for Slack; if the
LLM is unavailable the brief degrades to a deterministic summary.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from ..adapters.base import AdapterUnavailable
from ..adapters.clients.llm import LLMClient
from ..context import RunContext
from ..db.enums import EntryType
from ..logging_ import get_logger
from .flag_text import describe_flag
from .plans import PlanRepo

log = get_logger("orchestrator.planner")

_GiB = 1024**3

# action_type -> (assigned_agent, default cost estimate). Costs are conservative
# ceilings the governor checks before a live run (PRD §7.2, §8).
ACTION_SPECS: dict[str, tuple[str, dict[str, int]]] = {
    "notify": ("orchestrator", {}),
    "trigger_ideation": ("opportunity", {"llm_micros": 500_000}),
    "route_to_writer": ("production", {"llm_micros": 800_000}),
    "create_asana_task": ("production", {}),
    "queue_decay_refresh": ("production", {"ahrefs_units": 100}),
    "emaki_publish_draft": ("production", {}),
    "assemble_digest": ("reporting", {"bq_bytes": 2 * _GiB}),
    "send_digest_email": ("reporting", {}),
    "assemble_newsletter": ("reporting", {"llm_micros": 1_500_000, "bq_bytes": _GiB}),
    "assemble_social_post": ("reporting", {"llm_micros": 800_000}),
}

_SEVERITY_SCORE = {"high": 100, "medium": 60, "low": 30}


@dataclass
class _Candidate:
    score: int
    action_type: str
    params: dict[str, Any]
    rationale: str

    @property
    def agent(self) -> str:
        return ACTION_SPECS.get(self.action_type, ("orchestrator", {}))[0]

    @property
    def cost(self) -> dict[str, int]:
        return dict(ACTION_SPECS.get(self.action_type, ("orchestrator", {}))[1])


class Planner:
    def __init__(self, ctx: RunContext) -> None:
        self.ctx = ctx
        self.repo = PlanRepo(ctx.session)

    async def plan(self, brand: str, plan_date: date | None = None) -> tuple[int, str]:
        plan_date = plan_date or date.today()
        store = self.ctx.store
        flags = await store.query(brand=brand, types=[EntryType.FLAG], fresh_within_seconds=3 * 24 * 3600, limit=50)
        candidates_ctx = await store.query(brand=brand, types=[EntryType.CONTEXT],
                                           fresh_within_seconds=3 * 24 * 3600, limit=50)
        reports = await store.query(brand=brand, types=[EntryType.REPORT], fresh_within_seconds=2 * 24 * 3600, limit=10)
        drafts = await store.query(brand=brand, types=[EntryType.DISTRIBUTION_DRAFT],
                                   fresh_within_seconds=2 * 24 * 3600, limit=10)
        queue_metrics = await store.query(brand=brand, types=[EntryType.METRIC],
                                          source_system="hc_viral_hits", limit=5)

        candidates: list[_Candidate] = []
        candidates += self._from_flags(flags, queue_metrics)
        candidates += self._from_candidates(candidates_ctx)
        candidates += self._from_distribution(reports, drafts, brand)

        # Sort by score desc, keep it a plan (not a firehose): top 12.
        candidates.sort(key=lambda c: c.score, reverse=True)
        candidates = candidates[:12] or [
            _Candidate(10, "notify", {"message": f"No significant signals for {brand} today."},
                       "No high-priority findings in memory for this brand/day.")
        ]

        plan = await self.repo.create_plan(brand, plan_date)
        for rank, cand in enumerate(candidates, start=1):
            await self.repo.add_item(
                plan, rank=rank, assigned_agent=cand.agent, action_type=cand.action_type,
                params=cand.params, rationale=cand.rationale, cost_estimate=cand.cost, dry_run=True,
            )
        brief = await self._brief(brand, plan_date, candidates)
        log.info("planner produced plan %s for %s with %d items", plan.id, brand, len(candidates))
        return plan.id, brief

    # -- candidate synthesis --------------------------------------------------

    def _from_flags(self, flags, queue_metrics) -> list[_Candidate]:
        out: list[_Candidate] = []
        ready_ids: list[Any] = []
        for m in queue_metrics:
            if m.payload.get("kind") == "hc_viral_queue":
                ready_ids = m.payload.get("ready_topic_ids", [])
        for f in flags:
            p = f.payload or {}
            kind = p.get("kind", "flag")
            sev = _SEVERITY_SCORE.get(p.get("severity", "medium"), 60)
            if kind == "emaki_backlog" and ready_ids:
                out.append(_Candidate(sev + 20, "emaki_publish_draft",
                                      {"topic_id": ready_ids[0], "source": "hc_viral_hits"},
                                      f"HC-Viral has {p.get('ready_count')} drafts ready; push the top one to Emaki (unpublished)."))
            elif kind in ("overdue_outlines", "stuck_outlines"):
                out.append(_Candidate(sev, "notify",
                                      {"message": f"{kind}: {p.get('count', p.get('pending'))} item(s) need attention."},
                                      f"Production flag '{kind}' — surface to the editor."))
            elif kind == "writer_failures":
                out.append(_Candidate(sev, "notify", {"message": f"AI-writer failures: {p.get('count')}"},
                                      "Albert writer reported failed drafts."))
            elif kind == "spend_cap_exceeded":
                out.append(_Candidate(_SEVERITY_SCORE["high"], "notify",
                                      {"message": f"Spend cap hit: {p.get('metric')} ({p.get('scope')})"},
                                      "Governor refused an action on a spend cap — review caps/plan."))
            elif kind == "decay_candidate":
                out.append(_Candidate(sev + 5, "queue_decay_refresh",
                                      {"url": p.get("url"), "pos_delta": p.get("pos_delta")},
                                      f"Ranking decay on {p.get('url')} (Δpos {p.get('pos_delta')}) — queue an update refresh."))
            elif kind == "content_audit_finding":
                out.append(_Candidate(sev, "create_asana_task",
                                      {"name": f"Content depth: refresh {p.get('url')}",
                                       "notes": f"Auditor flagged low depth ({p.get('depth_pct')}%) / AVD ({p.get('avd_seconds')}s)."},
                                      f"Content-depth finding on {p.get('url')} — create a refresh task."))
            else:
                # writer_below_index + any other unhandled kind → a human title +
                # rationale. Shared with the backfill (flag_text.describe_flag) so
                # stored (old) and newly-planned items read identically.
                title, rationale = describe_flag(p)
                out.append(_Candidate(sev, "notify", {"message": title, "flag": p}, rationale))
        return out

    def _from_candidates(self, ctx_entries) -> list[_Candidate]:
        out: list[_Candidate] = []
        for c in ctx_entries:
            p = c.payload or {}
            kind = p.get("kind")
            if kind in ("topic_candidate", "viral_topic_candidate"):
                title = p.get("title") or "(untitled)"
                out.append(_Candidate(
                    55 if kind == "viral_topic_candidate" else 50,
                    "route_to_writer",
                    {"topic_id": p.get("topic_id"), "title": title, "source": p.get("source", c.source_system)},
                    f"Topic candidate from {c.source_system}: “{title}” — route to a writer.",
                ))
        # De-dup by title/topic_id, keep top few.
        seen: set = set()
        deduped = []
        for cand in out:
            key = cand.params.get("topic_id") or cand.params.get("title")
            if key in seen:
                continue
            seen.add(key)
            deduped.append(cand)
        return deduped[:5]

    def _from_distribution(self, reports, drafts, brand: str) -> list[_Candidate]:
        out: list[_Candidate] = []
        for r in reports:
            if r.payload.get("kind") == "daily_digest_inputs" and r.payload.get("ready"):
                out.append(_Candidate(55, "assemble_digest", {"report_entry_id": r.id},
                                      "Daily digest inputs are ready — assemble the per-brand digest for review."))
        for d in drafts:
            p = d.payload or {}
            if p.get("kind") == "newsletter_draft":
                out.append(_Candidate(45, "assemble_newsletter", {"draft_entry_id": d.id},
                                      "CarBuzz newsletter inputs ready — assemble draft (HTML) for human review."))
            elif p.get("kind") == "social_draft":
                out.append(_Candidate(40, "assemble_social_post", {"draft_entry_id": d.id},
                                      "Social post inputs ready — assemble images+captions for human review."))
        return out

    async def _brief(self, brand: str, plan_date: date, candidates: list[_Candidate]) -> str:
        lines = [f"*Switchboard — {brand} plan for {plan_date.isoformat()}*",
                 f"{len(candidates)} proposed item(s), dry-run by default. Approve/edit/reject in the dashboard."]
        for i, c in enumerate(candidates, 1):
            lines.append(f"{i}. `{c.action_type}` → {c.agent} — {c.rationale}")
        deterministic = "\n".join(lines)
        # Optional LLM polish (degrades to deterministic).
        try:
            llm = LLMClient(self.ctx)
            res = await llm.complete(
                system=("You are a chief-of-staff writing a terse morning brief for an editor. "
                        "Summarize the proposed plan in <=6 bullet points. Do not invent items."),
                prompt=deterministic, model=self.ctx.settings.models.synthesis, max_tokens=400,
                agent="orchestrator",
            )
            return res.text.strip() or deterministic
        except AdapterUnavailable:
            return deterministic
        except Exception as exc:  # noqa: BLE001
            log.info("LLM brief unavailable (%s); using deterministic brief", exc)
            return deterministic

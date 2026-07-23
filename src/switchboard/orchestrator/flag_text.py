"""Human descriptions for flags surfaced as ``notify`` plan items.

One source of truth for turning a flag payload into a (title, rationale) pair, so
the planner (new items) and the backfill (existing items) read identically. Known
kinds get a bespoke, detail-rich line; anything else is humanized (snake_case →
"Sentence case") and tagged with whatever identifying field the payload carries,
so no card is ever an opaque ``Flag surfaced: <snake_case>``.
"""

from __future__ import annotations

from typing import Any

_IDENT_FIELDS = ("writer", "url", "name", "title", "metric", "topic")


def describe_flag(payload: dict[str, Any] | None) -> tuple[str, str]:
    """(title, rationale) for a flag payload."""
    p = payload if isinstance(payload, dict) else {}
    kind = str(p.get("kind") or "flag")

    if kind == "writer_below_index":
        writer = p.get("writer") or "A writer"
        idx = p.get("relative_index")
        arts = p.get("articles")
        bits: list[str] = []
        if isinstance(idx, (int, float)):
            bits.append(f"{idx * 100:.0f}% of the cohort average")
        if arts:
            bits.append(f"{arts} articles")
        detail = f" ({' · '.join(bits)})" if bits else ""
        rationale = (
            f"{writer} is scoring below the performance index"
            + (f" — relative index {idx} (1.0 = cohort average)"
               if isinstance(idx, (int, float)) else "")
            + (f", {arts} recent articles" if arts else "")
            + ". Consider coaching, reassigning topics, or rebalancing the writer mix."
        )
        return f"Underperforming writer: {writer}{detail}", rationale

    label = kind.replace("_", " ")
    ident = next((str(p[k]) for k in _IDENT_FIELDS if p.get(k) not in (None, "")), None)
    title = (label[:1].upper() + label[1:]) if label else "Flag"
    rationale = (
        f"Flag “{label}” surfaced"
        + (f" ({ident})" if ident else "")
        + " — review the details (expand params) and action or dismiss."
    )
    return title + (f" — {ident}" if ident else ""), rationale


async def backfill_flag_descriptions(session: Any) -> int:
    """Rewrite existing ``notify`` plan items that carry a raw flag payload
    (``params.flag``) so their title + rationale match the current descriptive
    format. Idempotent — items already current are skipped. Returns the count
    updated. Commits via the caller's RunContext."""
    from sqlalchemy import select

    from ..db.models import PlanItem

    rows = (await session.execute(
        select(PlanItem).where(PlanItem.action_type == "notify")
    )).scalars().all()
    updated = 0
    for it in rows:
        params = it.params if isinstance(it.params, dict) else {}
        flag = params.get("flag")
        if not isinstance(flag, dict):
            continue
        title, rationale = describe_flag(flag)
        if params.get("message") == title and it.rationale == rationale:
            continue  # already current
        it.params = {**params, "message": title}  # new dict → SQLAlchemy sees the change
        it.rationale = rationale
        updated += 1
    await session.flush()
    return updated

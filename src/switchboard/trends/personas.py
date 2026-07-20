"""Writer-replication personas (§16.3): the selectable voices the AI writer can
write in. Two kinds — ``writer`` (distilled from one real top writer's corpus,
see AnalyticsAgent.distill_writer_persona) and ``house`` (an operator-defined
named style brief). Generation rotates round-robin through a brand's ``enabled``
personas, or a specific persona is chosen at trigger time.

This module is CRUD + selection + prompt-text composition; the expensive
per-writer distillation lives in the analytics agent (it needs the scraper).
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import func, select

from ..db.models import ContentJob, WriterPersona
from .. import style as style_mod


def persona_style_text(persona: WriterPersona) -> str:
    """Compose the style guidance injected into the writer's system prompt for
    this persona: the distilled feature guide (if any) plus a freeform brief."""
    parts: list[str] = []
    guide = style_mod.style_guide_text(persona.features or None)
    if guide:
        parts.append(guide)
    if persona.style_brief:
        parts.append(f"House style brief — write in this voice:\n{persona.style_brief.strip()}")
    return "\n\n".join(parts).strip()


async def list_personas(session: Any, brand: str) -> list[WriterPersona]:
    return list((await session.execute(
        select(WriterPersona).where(WriterPersona.brand == brand)
        .order_by(WriterPersona.kind, WriterPersona.name)
    )).scalars().all())


async def enabled_personas(session: Any, brand: str) -> list[WriterPersona]:
    """The rotation pool for a brand: enabled personas, stable order by id."""
    return list((await session.execute(
        select(WriterPersona).where(WriterPersona.brand == brand, WriterPersona.enabled.is_(True))
        .order_by(WriterPersona.id)
    )).scalars().all())


async def get_persona(session: Any, persona_id: int) -> WriterPersona | None:
    return (await session.execute(
        select(WriterPersona).where(WriterPersona.id == persona_id))).scalar_one_or_none()


async def pick_persona(session: Any, brand: str, persona_id: int | None = None) -> WriterPersona | None:
    """Resolve the persona for a new job. Explicit ``persona_id`` → that persona
    (any of the brand's, so an operator can force even a disabled one). Otherwise
    round-robin across the enabled pool, advanced by how many jobs already carry a
    persona — deterministic, needs no cursor. None when the brand has no personas
    (the generator then falls back to the aggregate house-style profile)."""
    if persona_id is not None:
        p = await get_persona(session, persona_id)
        return p if (p is not None and p.brand == brand) else None
    pool = await enabled_personas(session, brand)
    if not pool:
        return None
    # Rotate per PIPELINE (a pipeline's jobs share one voice): advance by how many
    # of the brand's pipelines already carry a persona.
    used = int((await session.execute(
        select(func.count(func.distinct(ContentJob.pipeline_id)))
        .select_from(ContentJob)
        .join(WriterPersona, WriterPersona.id == ContentJob.persona_id)
        .where(WriterPersona.brand == brand))).scalar_one() or 0)
    return pool[used % len(pool)]


async def create_house_persona(session: Any, brand: str, name: str, *,
                               style_brief: str, features: dict | None = None,
                               created_by: str | None = None) -> WriterPersona:
    p = WriterPersona(brand=brand, kind="house", name=name.strip(), author=None,
                      features=features, style_brief=style_brief.strip() or None,
                      enabled=True, created_by=created_by)
    session.add(p)
    await session.flush()
    return p


async def set_enabled(session: Any, persona_id: int, enabled: bool) -> WriterPersona | None:
    from datetime import datetime, timezone
    p = await get_persona(session, persona_id)
    if p is None:
        return None
    p.enabled = enabled
    p.updated_at = datetime.now(timezone.utc)
    await session.flush()
    return p

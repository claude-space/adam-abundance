"""Phase 9b (§16.3): the style-profile injection path. Verifies the generator's
active-profile lookup + versioning against Postgres, and that a persisted
profile renders into a non-empty style guide — without any network or LLM call
(the scrape/distill side is exercised by the pure helpers in test_style.py).
Uses a synthetic brand and cleans up. Skips if no DB is reachable.
"""

from __future__ import annotations

import pytest
from sqlalchemy import delete

from switchboard.context import RunContext
from switchboard.db.models import WriterStyleProfile
from switchboard.style import style_guide_text
from switchboard.trends.generators import _active_style_profile

BRAND = "itest_style"

FEATURES = {"voice": "wry and confident", "tone": "authoritative",
            "structure": "news lede then context", "dos": ["lead with the news"],
            "donts": ["no clickbait"]}


@pytest.fixture
async def ctx():
    try:
        async with RunContext.open() as c:
            await c.store.expire_stale()
            yield c
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no reachable Postgres: {exc}")


async def _cleanup(session):
    await session.execute(delete(WriterStyleProfile).where(WriterStyleProfile.brand == BRAND))
    await session.flush()


async def test_active_profile_lookup_and_versioning(ctx):
    await _cleanup(ctx.session)

    # No profile yet → lookup is None and the guide is empty.
    assert await _active_style_profile(ctx, BRAND) is None

    ctx.session.add(WriterStyleProfile(brand=BRAND, version=1, source_authors=["Alice"],
                                       features={"voice": "old"}, active=True))
    await ctx.session.flush()
    got = await _active_style_profile(ctx, BRAND)
    assert got is not None and got.version == 1

    # A new active version supersedes the old one; lookup returns the newest active.
    got.active = False
    ctx.session.add(WriterStyleProfile(brand=BRAND, version=2, source_authors=["Alice", "Bob"],
                                       features=FEATURES, active=True))
    await ctx.session.flush()
    got2 = await _active_style_profile(ctx, BRAND)
    assert got2.version == 2

    # The persisted features render into a usable style guide for the prompt.
    guide = style_guide_text(got2.features)
    assert "HOUSE STYLE GUIDE" in guide and "wry and confident" in guide
    assert "- Do: lead with the news" in guide

    await _cleanup(ctx.session)

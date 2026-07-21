"""WriterPersona CRUD + selection + prompt-text composition (§16.3) — unit tests.

``personas.py`` types its ``session`` as ``Any`` and only touches ``execute`` /
``add`` / ``flush``, so a deterministic in-memory fake session drives the async DB
helpers (no Postgres). ``persona_style_text`` is pure and is exercised against the
REAL ``style_guide_text`` renderer. Assertions reflect ACTUAL behaviour — including
that a whitespace-only ``style_brief`` still emits the brief label, that an empty
``features`` dict is treated as "no features", and that round-robin selection
advances by the distinct-pipeline job count with wrap-around.
"""
from __future__ import annotations

from datetime import datetime, timezone

from switchboard.trends import personas
from switchboard.db.models import WriterPersona


# -- fakes ---------------------------------------------------------------------

class _Scalars:
    def __init__(self, rows):
        self._rows = list(rows)

    def all(self):
        return list(self._rows)


class FakeResult:
    """Duck-typed Result covering the three accessors personas.py uses:
    ``.scalars().all()`` (list_/enabled_personas), ``.scalar_one_or_none()``
    (get_persona), and ``.scalar_one()`` (pick_persona's distinct-job count)."""

    def __init__(self, *, rows=None, scalar=None):
        self._rows = list(rows) if rows is not None else []
        self._scalar = scalar

    def scalars(self):
        return _Scalars(self._rows)

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None

    def scalar_one(self):
        return self._scalar


class FakeSession:
    """Duck-typed AsyncSession. Serves pre-seeded results FIFO (each test seeds
    exactly what its call path consumes) and records add()/flush()/statements."""

    def __init__(self, results=None):
        self._results = list(results or [])
        self.statements: list = []
        self.added: list = []
        self.flushes = 0

    async def execute(self, stmt):
        self.statements.append(stmt)
        assert self._results, "unexpected execute() — no result queued"
        return self._results.pop(0)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        self.flushes += 1


def _persona(**kw) -> WriterPersona:
    """A plain WriterPersona (no DB). Defaults are overridable per test."""
    defaults = dict(id=1, brand="hotcars", kind="house", name="Default", author=None,
                    features=None, style_brief=None, enabled=True, created_by=None)
    defaults.update(kw)
    return WriterPersona(**defaults)


# -- persona_style_text (pure) -------------------------------------------------

def test_style_text_empty_when_no_features_no_brief():
    assert personas.persona_style_text(_persona()) == ""


def test_style_text_empty_features_dict_is_treated_as_none():
    # {} is falsy → `features or None` → None → style_guide_text('') ; no brief → ''
    assert personas.persona_style_text(_persona(features={})) == ""


def test_style_text_features_without_usable_keys_render_empty():
    # non-empty dict, but no recognised style keys → guide renders '' → overall ''
    assert personas.persona_style_text(_persona(features={"nonsense": "x"})) == ""


def test_style_text_guide_only():
    out = personas.persona_style_text(_persona(features={"voice": "wry and dry"}))
    assert out.startswith("HOUSE STYLE GUIDE")
    assert "wry and dry" in out
    assert "House style brief" not in out


def test_style_text_brief_only_is_stripped_and_labelled():
    out = personas.persona_style_text(_persona(style_brief="  punchy and fast  "))
    assert out == "House style brief — write in this voice:\npunchy and fast"


def test_style_text_whitespace_only_brief_still_emits_label():
    # `if persona.style_brief:` is truthy for "   "; the stripped body is empty and
    # the trailing newline is removed by the final .strip() → just the label line.
    out = personas.persona_style_text(_persona(style_brief="   "))
    assert out == "House style brief — write in this voice:"


def test_style_text_none_brief_skipped():
    assert personas.persona_style_text(_persona(style_brief=None, features=None)) == ""


def test_style_text_guide_and_brief_joined_guide_first():
    out = personas.persona_style_text(_persona(
        features={"voice": "wry", "dos": ["lead hard"], "donts": ["no fluff"]},
        style_brief="  keep it tight  "))
    assert "HOUSE STYLE GUIDE" in out
    assert "House style brief — write in this voice:\nkeep it tight" in out
    assert out.index("HOUSE STYLE GUIDE") < out.index("House style brief")
    assert "\n\n" in out                                  # the two blocks are joined
    assert "- Do: lead hard" in out and "- Don't: no fluff" in out


# -- list_personas -------------------------------------------------------------

async def test_list_personas_returns_scalar_rows_as_list():
    p1, p2 = _persona(id=1, name="A"), _persona(id=2, name="B")
    s = FakeSession([FakeResult(rows=[p1, p2])])
    out = await personas.list_personas(s, "hotcars")
    assert out == [p1, p2]
    assert isinstance(out, list)


async def test_list_personas_empty():
    s = FakeSession([FakeResult(rows=[])])
    assert await personas.list_personas(s, "hotcars") == []


async def test_list_personas_query_filters_brand_and_orders_by_kind_name():
    s = FakeSession([FakeResult(rows=[])])
    await personas.list_personas(s, "carbuzz")
    sql = str(s.statements[0])
    assert "writer_persona.brand =" in sql
    assert "ORDER BY writer_persona.kind, writer_persona.name" in sql


# -- enabled_personas ----------------------------------------------------------

async def test_enabled_personas_returns_list():
    p = _persona(enabled=True)
    s = FakeSession([FakeResult(rows=[p])])
    assert await personas.enabled_personas(s, "hotcars") == [p]


async def test_enabled_personas_empty():
    s = FakeSession([FakeResult(rows=[])])
    assert await personas.enabled_personas(s, "hotcars") == []


async def test_enabled_personas_query_filters_enabled_and_orders_by_id():
    s = FakeSession([FakeResult(rows=[])])
    await personas.enabled_personas(s, "hotcars")
    sql = str(s.statements[0])
    assert "writer_persona.brand =" in sql
    assert "writer_persona.enabled IS" in sql
    assert "ORDER BY writer_persona.id" in sql


# -- get_persona ---------------------------------------------------------------

async def test_get_persona_found():
    p = _persona(id=7)
    s = FakeSession([FakeResult(rows=[p])])
    assert await personas.get_persona(s, 7) is p


async def test_get_persona_missing_returns_none():
    s = FakeSession([FakeResult(rows=[])])
    assert await personas.get_persona(s, 999) is None


async def test_get_persona_query_filters_by_id():
    s = FakeSession([FakeResult(rows=[])])
    await personas.get_persona(s, 42)
    assert "writer_persona.id =" in str(s.statements[0])


# -- pick_persona: explicit persona_id -----------------------------------------

async def test_pick_persona_explicit_id_match_returns_it():
    p = _persona(id=5, brand="hotcars")
    s = FakeSession([FakeResult(rows=[p])])          # get_persona → p
    assert await personas.pick_persona(s, "hotcars", persona_id=5) is p
    assert len(s.statements) == 1                    # no rotation count query


async def test_pick_persona_explicit_id_brand_mismatch_returns_none():
    p = _persona(id=5, brand="carbuzz")              # belongs to a different brand
    s = FakeSession([FakeResult(rows=[p])])
    assert await personas.pick_persona(s, "hotcars", persona_id=5) is None


async def test_pick_persona_explicit_id_not_found_returns_none():
    s = FakeSession([FakeResult(rows=[])])           # get_persona → None
    assert await personas.pick_persona(s, "hotcars", persona_id=123) is None


# -- pick_persona: round-robin rotation ----------------------------------------

async def test_pick_persona_rotation_no_pool_returns_none():
    s = FakeSession([FakeResult(rows=[])])           # empty enabled pool
    assert await personas.pick_persona(s, "hotcars") is None
    assert len(s.statements) == 1                    # count query short-circuited


async def test_pick_persona_rotation_first_slot_when_zero_used():
    pool = [_persona(id=1), _persona(id=2), _persona(id=3)]
    s = FakeSession([FakeResult(rows=pool), FakeResult(scalar=0)])
    assert await personas.pick_persona(s, "hotcars") is pool[0]


async def test_pick_persona_rotation_advances_by_used_count():
    pool = [_persona(id=1), _persona(id=2), _persona(id=3)]
    s = FakeSession([FakeResult(rows=pool), FakeResult(scalar=1)])
    assert await personas.pick_persona(s, "hotcars") is pool[1]
    assert len(s.statements) == 2                    # enabled pool + distinct-job count


async def test_pick_persona_rotation_wraps_around_to_zero():
    pool = [_persona(id=1), _persona(id=2), _persona(id=3)]
    s = FakeSession([FakeResult(rows=pool), FakeResult(scalar=3)])   # 3 % 3 == 0
    assert await personas.pick_persona(s, "hotcars") is pool[0]


async def test_pick_persona_rotation_wraps_around_nonzero():
    pool = [_persona(id=1), _persona(id=2), _persona(id=3)]
    s = FakeSession([FakeResult(rows=pool), FakeResult(scalar=7)])   # 7 % 3 == 1
    assert await personas.pick_persona(s, "hotcars") is pool[1]


async def test_pick_persona_rotation_count_none_defaults_to_zero():
    pool = [_persona(id=1), _persona(id=2)]
    s = FakeSession([FakeResult(rows=pool), FakeResult(scalar=None)])   # `or 0`
    assert await personas.pick_persona(s, "hotcars") is pool[0]


async def test_pick_persona_single_persona_pool_always_that_one():
    pool = [_persona(id=9)]
    s = FakeSession([FakeResult(rows=pool), FakeResult(scalar=5)])   # 5 % 1 == 0
    assert await personas.pick_persona(s, "hotcars") is pool[0]


# -- create_house_persona ------------------------------------------------------

async def test_create_house_persona_defaults_and_strip():
    s = FakeSession()
    p = await personas.create_house_persona(s, "hotcars", "  Snappy Voice  ",
                                            style_brief="  be punchy  ")
    assert p.kind == "house"
    assert p.name == "Snappy Voice"                  # name.strip()
    assert p.author is None
    assert p.style_brief == "be punchy"              # style_brief.strip()
    assert p.features is None                         # default
    assert p.enabled is True
    assert p.created_by is None                       # default
    assert p.brand == "hotcars"
    assert s.added == [p]                             # added to the session
    assert s.flushes == 1                             # flushed once


async def test_create_house_persona_blank_brief_becomes_none():
    s = FakeSession()
    p = await personas.create_house_persona(s, "hotcars", "X", style_brief="   ")
    assert p.style_brief is None                      # "   ".strip() or None → None


async def test_create_house_persona_empty_brief_becomes_none():
    s = FakeSession()
    p = await personas.create_house_persona(s, "hotcars", "X", style_brief="")
    assert p.style_brief is None


async def test_create_house_persona_with_features_and_created_by():
    s = FakeSession()
    feats = {"voice": "wry"}
    p = await personas.create_house_persona(s, "carbuzz", "Named", style_brief="brief",
                                            features=feats, created_by="editor@x.com")
    assert p.features is feats
    assert p.created_by == "editor@x.com"
    assert p.brand == "carbuzz"
    assert p.style_brief == "brief"


# -- set_enabled ---------------------------------------------------------------

async def test_set_enabled_missing_returns_none_and_does_not_flush():
    s = FakeSession([FakeResult(rows=[])])           # get_persona → None
    assert await personas.set_enabled(s, 1, True) is None
    assert s.flushes == 0


async def test_set_enabled_disables_and_stamps_updated_at():
    p = _persona(id=3, enabled=True)
    s = FakeSession([FakeResult(rows=[p])])
    out = await personas.set_enabled(s, 3, False)
    assert out is p
    assert p.enabled is False
    assert isinstance(p.updated_at, datetime)
    assert p.updated_at.tzinfo is timezone.utc        # timezone-aware UTC stamp
    assert s.flushes == 1


async def test_set_enabled_enables():
    p = _persona(id=4, enabled=False)
    s = FakeSession([FakeResult(rows=[p])])
    out = await personas.set_enabled(s, 4, True)
    assert out is p and p.enabled is True
    assert s.flushes == 1

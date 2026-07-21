"""Trend Score weight persistence (§13.19) — unit tests.

``weights.py`` types its ``session`` as ``Any`` and only needs ``execute`` /
``add`` / ``flush``, so these tests drive it with a deterministic in-memory fake
session (no Postgres, no aiosqlite). The fake orders rows by a monotonic logical
clock, mirroring the module's "latest row per key wins" contract. Assertions
reflect ACTUAL behaviour: clamping to each weight's meta range, 4-decimal
rounding, and the "pinned-back-to-default is dropped" read semantics.
"""
from switchboard.trends import weights
from switchboard.trends.detector import DEFAULT_SCORE_WEIGHTS, SCORE_WEIGHT_META
from switchboard.db.models import TrendScoreWeight

_RANGES = {m["key"]: (m["min"], m["max"]) for m in SCORE_WEIGHT_META}


# -- fake async session --------------------------------------------------------

class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class FakeSession:
    """Duck-typed AsyncSession. weights.py issues one fixed SELECT (key, value,
    effective_at ORDER BY effective_at ASC); we ignore the statement and return
    rows in insertion order via a logical clock — the same ordering the real
    server_default=now() gives, but collision-free and deterministic."""

    def __init__(self):
        self.rows: list[TrendScoreWeight] = []
        self._clock = 0
        self.flushes = 0

    async def execute(self, _stmt):
        ordered = sorted(self.rows, key=lambda r: r.effective_at)
        return _Result([(r.key, r.value, r.effective_at) for r in ordered])

    def add(self, obj):
        self._clock += 1
        if getattr(obj, "effective_at", None) is None:
            obj.effective_at = self._clock
        self.rows.append(obj)

    async def flush(self):
        self.flushes += 1


def _seed(session, key, value):
    """Append a persisted row the way a prior write would have."""
    session.add(TrendScoreWeight(key=key, value=value))


# -- load_overrides ------------------------------------------------------------

async def test_load_overrides_empty_session():
    assert await weights.load_overrides(FakeSession()) == {}


async def test_load_overrides_ignores_unknown_keys():
    s = FakeSession()
    _seed(s, "not_a_real_weight", 99.0)
    assert await weights.load_overrides(s) == {}


async def test_load_overrides_drops_values_equal_to_default():
    s = FakeSession()
    _seed(s, "novelty_max", DEFAULT_SCORE_WEIGHTS["novelty_max"])   # == default
    assert await weights.load_overrides(s) == {}


async def test_load_overrides_keeps_differing_value_as_float():
    s = FakeSession()
    _seed(s, "novelty_max", 25)                                     # int on the way in
    out = await weights.load_overrides(s)
    assert out == {"novelty_max": 25.0}
    assert isinstance(out["novelty_max"], float)


async def test_load_overrides_latest_row_per_key_wins():
    s = FakeSession()
    _seed(s, "novelty_max", 20.0)
    _seed(s, "novelty_max", 30.0)                                   # later write
    assert await weights.load_overrides(s) == {"novelty_max": 30.0}


async def test_load_overrides_pinned_back_to_default_is_dropped():
    s = FakeSession()
    _seed(s, "novelty_max", 30.0)
    _seed(s, "novelty_max", DEFAULT_SCORE_WEIGHTS["novelty_max"])   # reset to default
    assert await weights.load_overrides(s) == {}


async def test_load_overrides_multiple_keys():
    s = FakeSession()
    _seed(s, "novelty_max", 25.0)
    _seed(s, "breaking", 20.0)
    _seed(s, "watchlist", DEFAULT_SCORE_WEIGHTS["watchlist"])       # default → dropped
    assert await weights.load_overrides(s) == {"novelty_max": 25.0, "breaking": 20.0}


# -- load_effective ------------------------------------------------------------

async def test_load_effective_empty_is_all_defaults():
    eff = await weights.load_effective(FakeSession())
    assert eff == DEFAULT_SCORE_WEIGHTS
    assert eff is not DEFAULT_SCORE_WEIGHTS                         # a merged copy


async def test_load_effective_applies_override_over_defaults():
    s = FakeSession()
    _seed(s, "novelty_max", 25.0)
    eff = await weights.load_effective(s)
    assert eff["novelty_max"] == 25.0
    assert set(eff) == set(DEFAULT_SCORE_WEIGHTS)                   # full key set
    # untouched keys keep their shipped defaults
    assert eff["breaking"] == DEFAULT_SCORE_WEIGHTS["breaking"]


# -- save_weights --------------------------------------------------------------

async def test_save_weights_writes_a_changed_key():
    s = FakeSession()
    written = await weights.save_weights(s, {"novelty_max": 25.0}, updated_by="andrew")
    assert written == ["novelty_max"]
    assert s.flushes == 1
    assert await weights.load_overrides(s) == {"novelty_max": 25.0}


async def test_save_weights_unchanged_default_writes_nothing():
    s = FakeSession()
    written = await weights.save_weights(s, {"novelty_max": DEFAULT_SCORE_WEIGHTS["novelty_max"]})
    assert written == []
    assert s.rows == []
    assert s.flushes == 0                                           # no flush when nothing written


async def test_save_weights_skips_unknown_key():
    s = FakeSession()
    assert await weights.save_weights(s, {"totally_made_up": 5.0}) == []
    assert s.rows == []


async def test_save_weights_skips_none_and_non_numeric():
    s = FakeSession()
    written = await weights.save_weights(s, {
        "novelty_max": None,        # raw is None → skipped
        "breaking": "abc",          # float() ValueError → skipped
        "watchlist": [1, 2],        # float() TypeError → skipped
    })
    assert written == []
    assert s.rows == []


async def test_save_weights_clamps_to_meta_range():
    s = FakeSession()
    await weights.save_weights(s, {
        "novelty_max": 999,         # above max 40 → 40.0
        "watchlist": -5,            # below min 0 → 0.0
        "sat_outlets": 0.2,         # below min 1 → 1.0
    })
    assert _RANGES["novelty_max"] == (0.0, 40.0)
    assert _RANGES["sat_outlets"] == (1.0, 20.0)
    out = await weights.load_overrides(s)
    assert out == {"novelty_max": 40.0, "watchlist": 0.0, "sat_outlets": 1.0}


async def test_save_weights_rounds_to_four_decimals():
    s = FakeSession()
    await weights.save_weights(s, {"novelty_max": 12.345678})
    assert await weights.load_overrides(s) == {"novelty_max": 12.3457}


async def test_save_weights_near_default_rounds_away_and_skips():
    s = FakeSession()
    default = DEFAULT_SCORE_WEIGHTS["novelty_max"]                  # 18.0
    written = await weights.save_weights(s, {"novelty_max": default + 1e-8})
    assert written == []                                           # round(…,4) == default → skip
    assert s.rows == []


async def test_save_weights_idempotent_second_write():
    s = FakeSession()
    assert await weights.save_weights(s, {"novelty_max": 25.0}) == ["novelty_max"]
    assert await weights.save_weights(s, {"novelty_max": 25.0}) == []   # unchanged vs override
    assert s.flushes == 1                                          # only the first flushed
    assert len(s.rows) == 1


async def test_save_weights_multiple_keys_returns_all_changed():
    s = FakeSession()
    written = await weights.save_weights(s, {
        "novelty_max": 25.0,
        "breaking": DEFAULT_SCORE_WEIGHTS["breaking"],   # unchanged → not written
        "watchlist": 20.0,
    })
    assert written == ["novelty_max", "watchlist"]                  # dict/iteration order
    assert await weights.load_overrides(s) == {"novelty_max": 25.0, "watchlist": 20.0}


async def test_save_weights_records_updated_by():
    s = FakeSession()
    await weights.save_weights(s, {"novelty_max": 25.0}, updated_by="editor@x.com")
    row = next(r for r in s.rows if r.key == "novelty_max")
    assert row.updated_by == "editor@x.com"
    assert row.value == 25.0


async def test_save_weights_none_values_returns_empty():
    s = FakeSession()
    assert await weights.save_weights(s, None) == []               # `values or {}`
    assert s.rows == []


async def test_save_weights_pin_to_default_writes_row_but_reads_empty():
    s = FakeSession()
    await weights.save_weights(s, {"novelty_max": 25.0})           # now an override
    written = await weights.save_weights(
        s, {"novelty_max": DEFAULT_SCORE_WEIGHTS["novelty_max"]})  # pin back to default
    assert written == ["novelty_max"]                              # a row IS written
    assert len(s.rows) == 2
    assert await weights.load_overrides(s) == {}                   # but reads as "no override"


# -- reset_weights -------------------------------------------------------------

async def test_reset_weights_on_fresh_session_is_zero():
    s = FakeSession()
    n = await weights.reset_weights(s, updated_by="andrew")
    assert n == 0
    assert s.rows == []
    assert s.flushes == 0


async def test_reset_weights_pins_only_overridden_keys():
    s = FakeSession()
    await weights.save_weights(s, {"novelty_max": 25.0, "breaking": 20.0})
    n = await weights.reset_weights(s, updated_by="andrew")
    assert n == 2                                                  # exactly the two that differed
    assert await weights.load_overrides(s) == {}                   # everything back to default


async def test_reset_then_effective_is_defaults():
    s = FakeSession()
    await weights.save_weights(s, {"novelty_max": 40.0, "sat_outlets": 12.0})
    await weights.reset_weights(s)
    assert await weights.load_effective(s) == DEFAULT_SCORE_WEIGHTS

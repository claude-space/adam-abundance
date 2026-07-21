"""Cover pricing.load_rates + pricing.seed_pricing without a real DB, using a
duck-typed fake async session (mirrors the FakeSession pattern used elsewhere).
test_pricing.py already covers the pure metric_to_usd / seed_rows logic."""

from __future__ import annotations

from typing import Any

from switchboard import pricing


class _Result:
    def __init__(self, rows: list | None = None, scalar: Any = None):
        self._rows = rows or []
        self._scalar = scalar

    def all(self):
        return self._rows

    def scalar_one(self):
        return self._scalar


class _Session:
    """Returns queued results from execute(); records add()/flush()."""

    def __init__(self, results: list[_Result]):
        self._results = list(results)
        self.added: list[Any] = []
        self.flushed = 0

    async def execute(self, _stmt):
        return self._results.pop(0)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        self.flushed += 1


async def test_load_rates_from_config_rows():
    s = _Session([_Result(rows=[("bq_tb", 7.0), ("ahrefs_unit", 0.01)])])
    assert await pricing.load_rates(s) == {"bq_tb": 7.0, "ahrefs_unit": 0.01}


async def test_load_rates_falls_back_to_defaults_when_unseeded():
    s = _Session([_Result(rows=[])])
    rates = await pricing.load_rates(s)
    assert rates["bq_tb"] == pricing.DEFAULT_BQ_USD_PER_TB
    assert rates["ahrefs_unit"] == pricing.DEFAULT_AHREFS_USD_PER_UNIT


async def test_load_rates_partial_row_keeps_other_default():
    s = _Session([_Result(rows=[("bq_tb", 9.0)])])
    rates = await pricing.load_rates(s)
    assert rates["bq_tb"] == 9.0
    assert rates["ahrefs_unit"] == pricing.DEFAULT_AHREFS_USD_PER_UNIT


async def test_seed_pricing_inserts_when_empty():
    s = _Session([_Result(scalar=0)])
    n = await pricing.seed_pricing(s)
    assert n == len(pricing.seed_rows())
    assert len(s.added) == n and s.flushed == 1


async def test_seed_pricing_is_idempotent_when_populated():
    s = _Session([_Result(scalar=5)])
    n = await pricing.seed_pricing(s)
    assert n == 0 and s.added == [] and s.flushed == 0

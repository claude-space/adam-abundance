"""Unit tests for app-configurable spend caps (governor/caps_config.py).
Pure overlay logic + a duck-typed fake session for save/resolve/reset — no DB."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from switchboard.config import SpendCaps
from switchboard.governor import caps_config as cc

_GIB = 1024**3


# --- fake session ---------------------------------------------------------

class _Result:
    def __init__(self, scalar: Any = None):
        self._scalar = scalar

    def scalar_one_or_none(self):
        return self._scalar


class _Session:
    def __init__(self, results: list[_Result]):
        self._results = list(results)
        self.added: list[Any] = []
        self.deleted: list[Any] = []
        self.flushed = 0

    async def execute(self, _stmt):
        return self._results.pop(0)

    def add(self, obj):
        self.added.append(obj)

    async def delete(self, obj):
        self.deleted.append(obj)

    async def flush(self):
        self.flushed += 1


# --- caps_to_ui / overlay -------------------------------------------------

def test_caps_to_ui_converts_native_units():
    assert cc.caps_to_ui(SpendCaps()) == {
        "enabled": True, "llm_usd_per_day": 20.0, "bq_gib_per_day": 100.0,
        "ahrefs_units_per_day": 5000,
    }


def test_overlay_none_returns_base_unchanged():
    base = SpendCaps()
    assert cc._overlay(base, None) is base


def test_overlay_applies_values_and_clamps_per_run_to_per_day():
    eff = cc._overlay(SpendCaps(), {"enabled": True, "llm_usd_per_day": 3,
                                    "bq_gib_per_day": 50, "ahrefs_units_per_day": 100})
    assert eff.llm_micros_per_day == 3_000_000
    assert eff.llm_micros_per_run == 3_000_000        # clamped down from $5 default
    assert eff.bq_bytes_per_day == 50 * _GIB
    assert eff.ahrefs_units_per_day == 100
    assert eff.ahrefs_units_per_run == 100            # clamped down from 1000 default


def test_overlay_disabled_makes_caps_unenforced():
    eff = cc._overlay(SpendCaps(), {"enabled": False, "llm_usd_per_day": 20,
                                    "bq_gib_per_day": 100, "ahrefs_units_per_day": 5000})
    assert eff.enabled is False
    assert eff.per_day("llm_micros") is None
    assert eff.per_run("ahrefs_units") is None


def test_overlay_bad_or_negative_values_fall_back_to_base():
    base = SpendCaps()
    eff = cc._overlay(base, {"enabled": True, "llm_usd_per_day": "oops",
                             "bq_gib_per_day": -5, "ahrefs_units_per_day": 100})
    assert eff.llm_micros_per_day == base.llm_micros_per_day   # non-numeric -> base
    assert eff.bq_bytes_per_day == base.bq_bytes_per_day        # negative -> base
    assert eff.ahrefs_units_per_day == 100                      # valid -> applied


# --- save / load / resolve / reset (fake session) -------------------------

async def test_save_inserts_when_absent_and_coerces():
    s = _Session([_Result(scalar=None)])
    out = await cc.save_caps(s, enabled=1, llm_usd_per_day=10.126, bq_gib_per_day=50,
                             ahrefs_units_per_day=200.9)
    assert out == {"enabled": True, "llm_usd_per_day": 10.13, "bq_gib_per_day": 50.0,
                   "ahrefs_units_per_day": 200}  # usd rounded to cents, ahrefs -> int
    assert len(s.added) == 1 and s.flushed == 1


async def test_save_updates_when_present():
    existing = SimpleNamespace(value={"enabled": True}, updated_by=None)
    s = _Session([_Result(scalar=existing)])
    await cc.save_caps(s, enabled=False, llm_usd_per_day=1, bq_gib_per_day=1,
                       ahrefs_units_per_day=1, updated_by="a@b.com")
    assert s.added == [] and existing.value["enabled"] is False
    assert existing.updated_by == "a@b.com"


async def test_save_rejects_non_numeric():
    with pytest.raises(ValueError):
        await cc.save_caps(_Session([]), enabled=True, llm_usd_per_day="x",
                           bq_gib_per_day=1, ahrefs_units_per_day=1)


async def test_resolve_overlays_stored_override():
    s = _Session([_Result(scalar={"enabled": False, "llm_usd_per_day": 5,
                                  "bq_gib_per_day": 10, "ahrefs_units_per_day": 50})])
    eff = await cc.resolve_caps(s, SpendCaps())
    assert eff.enabled is False and eff.llm_micros_per_day == 5_000_000


async def test_resolve_no_override_returns_base():
    eff = await cc.resolve_caps(_Session([_Result(scalar=None)]), SpendCaps())
    assert cc.caps_to_ui(eff) == cc.caps_to_ui(SpendCaps())


async def test_reset_deletes_when_present():
    existing = SimpleNamespace()
    s = _Session([_Result(scalar=existing)])
    assert await cc.reset_caps(s) is True and s.deleted == [existing]


async def test_reset_noop_when_absent():
    s = _Session([_Result(scalar=None)])
    assert await cc.reset_caps(s) is False and s.deleted == []

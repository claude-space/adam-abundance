"""App-configurable spend caps.

The env/code :class:`~switchboard.config.SpendCaps` are the DEFAULT. An admin can
override them at runtime from the Governor page — toggle enforcement on/off and
set the three daily ceilings — stored as JSON in ``app_setting[SPEND_CAPS_KEY]``.
``resolve_caps`` overlays the override on the env default; the governor enforces
the result. Same posture as the trend-alert config (``notifications.py``) and the
trend-score weights: DB override over shipped defaults, admin-gated, best-effort.

The stored/UI shape is operator-friendly (dollars / GiB / units); the overlay
converts to the SpendCaps native units (micros / bytes / units).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from ..config import SpendCaps, get_settings
from ..db.models import AppSetting
from ..logging_ import get_logger

log = get_logger("governor.caps")

SPEND_CAPS_KEY = "spend_caps"
_GIB = 1024**3
_USD_MICROS = 1_000_000


def caps_to_ui(caps: SpendCaps) -> dict[str, Any]:
    """SpendCaps -> the operator-facing shape (dollars / GiB / units)."""
    return {
        "enabled": caps.enabled,
        "llm_usd_per_day": round(caps.llm_micros_per_day / _USD_MICROS, 2),
        "bq_gib_per_day": round(caps.bq_bytes_per_day / _GIB, 2),
        "ahrefs_units_per_day": caps.ahrefs_units_per_day,
    }


def _overlay(base: SpendCaps, override: dict[str, Any] | None) -> SpendCaps:
    """Return ``base`` with the operator override applied. Missing/invalid fields
    fall back to ``base``. per-run ceilings are clamped to the (possibly lower)
    per-day so per_run never exceeds per_day."""
    if not isinstance(override, dict):
        return base

    def _num(v: Any, default: float) -> float:
        try:
            n = float(v)
        except (TypeError, ValueError):
            return default
        return n if n >= 0 else default

    llm_day = int(_num(override.get("llm_usd_per_day"), base.llm_micros_per_day / _USD_MICROS) * _USD_MICROS)
    bq_day = int(_num(override.get("bq_gib_per_day"), base.bq_bytes_per_day / _GIB) * _GIB)
    ah_day = int(_num(override.get("ahrefs_units_per_day"), base.ahrefs_units_per_day))
    return SpendCaps(
        enabled=bool(override.get("enabled", base.enabled)),
        llm_micros_per_day=llm_day,
        llm_micros_per_run=min(base.llm_micros_per_run, llm_day),
        bq_bytes_per_day=bq_day,
        bq_bytes_per_run=min(base.bq_bytes_per_run, bq_day),
        ahrefs_units_per_day=ah_day,
        ahrefs_units_per_run=min(base.ahrefs_units_per_run, ah_day),
    )


async def load_override(session: Any) -> dict[str, Any] | None:
    """The stored caps override (operator-shaped dict), or None if unset."""
    row = (await session.execute(
        select(AppSetting.value).where(AppSetting.key == SPEND_CAPS_KEY)
    )).scalar_one_or_none()
    return row if isinstance(row, dict) else None


async def resolve_caps(session: Any, base: SpendCaps | None = None) -> SpendCaps:
    """Effective caps = env/code default overlaid with the DB override (if any).
    Best-effort: a failed override read falls back to the default rather than
    breaking enforcement."""
    base = base if base is not None else get_settings().caps
    try:
        override = await load_override(session)
    except Exception as exc:  # noqa: BLE001 — never break enforcement on a config read
        log.warning("[caps] override load failed, using defaults: %s", exc)
        return base
    return _overlay(base, override)


async def save_caps(session: Any, *, enabled: Any, llm_usd_per_day: Any,
                    bq_gib_per_day: Any, ahrefs_units_per_day: Any,
                    updated_by: str | None = None) -> dict[str, Any]:
    """Validate + upsert the caps override. Raises ValueError on a bad value.
    Returns the stored operator-shaped dict."""
    def _nonneg(v: Any, field: str) -> float:
        try:
            n = float(v)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field} must be a number") from exc
        if n < 0:
            raise ValueError(f"{field} must be >= 0")
        return n

    value = {
        "enabled": bool(enabled),
        "llm_usd_per_day": round(_nonneg(llm_usd_per_day, "llm_usd_per_day"), 2),
        "bq_gib_per_day": round(_nonneg(bq_gib_per_day, "bq_gib_per_day"), 2),
        "ahrefs_units_per_day": int(_nonneg(ahrefs_units_per_day, "ahrefs_units_per_day")),
    }
    existing = (await session.execute(
        select(AppSetting).where(AppSetting.key == SPEND_CAPS_KEY)
    )).scalar_one_or_none()
    if existing is None:
        session.add(AppSetting(key=SPEND_CAPS_KEY, value=value, updated_by=updated_by))
    else:
        existing.value = value
        existing.updated_by = updated_by
    await session.flush()
    return value


async def reset_caps(session: Any) -> bool:
    """Delete the override so caps revert to the shipped env defaults.
    Returns True if a row was removed."""
    existing = (await session.execute(
        select(AppSetting).where(AppSetting.key == SPEND_CAPS_KEY)
    )).scalar_one_or_none()
    if existing is None:
        return False
    await session.delete(existing)
    await session.flush()
    return True

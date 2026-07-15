"""Cost → USD pricing (PRD §16.4). One pricing source feeds BOTH the governor's
caps and the read-only Expenditure view — not two. Pure conversion + a seeder
that reuses the existing per-model cost model (`costs.py`).

LLM spend is already metered in micro-USD in `spend_ledger`, so its USD value is
a straight /1e6; BigQuery bytes convert via a $/TiB rate; Ahrefs units via a
$/unit rate. The Ahrefs rate is a PLACEHOLDER until confirmed (§13.17).
"""

from __future__ import annotations

from typing import Any

from . import costs

DEFAULT_BQ_USD_PER_TB = 6.25            # GCP BigQuery on-demand, ~$6.25 / TiB scanned
DEFAULT_AHREFS_USD_PER_UNIT = 0.005     # PLACEHOLDER — confirm the real Ahrefs unit price (§13.17)
_BYTES_PER_TIB = 1024 ** 4


def seed_rows() -> list[dict[str, Any]]:
    """The pricing_config rows to seed: per-model LLM $/token (from
    ``costs._PRICING``) plus the Ahrefs-unit and BigQuery-per-TiB rates the
    ledger→USD conversion needs."""
    rows: list[dict[str, Any]] = []
    for model, (usd_in_per_m, usd_out_per_m) in costs._PRICING.items():
        rows.append({"kind": "llm_input", "key": model, "usd_per_unit": usd_in_per_m / 1_000_000.0})
        rows.append({"kind": "llm_output", "key": model, "usd_per_unit": usd_out_per_m / 1_000_000.0})
    rows.append({"kind": "bq_tb", "key": None, "usd_per_unit": DEFAULT_BQ_USD_PER_TB})
    rows.append({"kind": "ahrefs_unit", "key": None, "usd_per_unit": DEFAULT_AHREFS_USD_PER_UNIT})
    return rows


def metric_to_usd(metric: str, amount: float, *,
                  bq_tb: float = DEFAULT_BQ_USD_PER_TB,
                  ahrefs_unit: float = DEFAULT_AHREFS_USD_PER_UNIT) -> float:
    """Convert one spend_ledger metric amount to USD."""
    amount = float(amount or 0)
    if metric == "llm_micros":
        return round(amount / 1_000_000.0, 6)
    if metric == "bq_bytes":
        return round((amount / _BYTES_PER_TIB) * bq_tb, 6)
    if metric == "ahrefs_units":
        return round(amount * ahrefs_unit, 6)
    return 0.0


async def load_rates(session: Any) -> dict[str, float]:
    """Read the non-LLM conversion rates from pricing_config (falling back to the
    module defaults when unseeded)."""
    from sqlalchemy import select

    from .db.models import PricingConfig

    rows = (await session.execute(
        select(PricingConfig.kind, PricingConfig.usd_per_unit)
        .where(PricingConfig.kind.in_(["bq_tb", "ahrefs_unit"]))
    )).all()
    got = {k: float(v) for k, v in rows}
    return {"bq_tb": got.get("bq_tb", DEFAULT_BQ_USD_PER_TB),
            "ahrefs_unit": got.get("ahrefs_unit", DEFAULT_AHREFS_USD_PER_UNIT)}


async def seed_pricing(session: Any) -> int:
    """Idempotently seed pricing_config (no-op if already populated). Returns the
    number of rows inserted."""
    from sqlalchemy import func as _func, select

    from .db.models import PricingConfig

    count = int((await session.execute(select(_func.count()).select_from(PricingConfig))).scalar_one())
    if count:
        return 0
    rows = seed_rows()
    for r in rows:
        session.add(PricingConfig(**r))
    await session.flush()
    return len(rows)

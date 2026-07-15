"""Phase 10 (§16.4) cost → USD pricing."""
from switchboard.pricing import metric_to_usd, seed_rows


def test_metric_to_usd():
    assert metric_to_usd("llm_micros", 1_000_000) == 1.0          # micro-USD → USD
    assert metric_to_usd("bq_bytes", 1024 ** 4, bq_tb=6.25) == 6.25   # 1 TiB scanned
    assert metric_to_usd("ahrefs_units", 100, ahrefs_unit=0.005) == 0.5
    assert metric_to_usd("unknown", 999) == 0.0
    assert metric_to_usd("llm_micros", 0) == 0.0


def test_seed_rows_covers_llm_and_conversion_rates():
    rows = seed_rows()
    kinds = {r["kind"] for r in rows}
    assert {"llm_input", "llm_output", "bq_tb", "ahrefs_unit"} <= kinds
    llm = [r for r in rows if r["kind"] == "llm_input"]
    assert llm and all(r["key"] and r["usd_per_unit"] > 0 for r in llm)

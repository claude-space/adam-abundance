"""LLM cost model (PRD §8 reuse). Dependency-free — runs without the stack."""

from switchboard.costs import compute_llm_micros, micros_to_usd, price_per_million


def test_known_model_pricing():
    assert price_per_million("claude-sonnet-4-6") == (3.00, 15.00)
    assert price_per_million("claude-haiku-4-5") == (1.00, 5.00)


def test_dated_snapshot_normalizes():
    assert price_per_million("claude-haiku-4-5-20251001") == (1.00, 5.00)


def test_unknown_model_falls_back():
    assert price_per_million("some-future-model") == (3.00, 15.00)


def test_compute_micros_matches_formula():
    # 1M input + 1M output on Sonnet = $3 + $15 = $18 = 18,000,000 micros.
    micros = compute_llm_micros("claude-sonnet-4-6", input_tokens=1_000_000, output_tokens=1_000_000)
    assert micros == 18_000_000
    assert micros_to_usd(micros) == 18.0


def test_web_search_fee_added():
    base = compute_llm_micros("claude-haiku-4-5", input_tokens=0, output_tokens=0)
    with_search = compute_llm_micros("claude-haiku-4-5", web_search_requests=3)
    assert base == 0
    assert with_search == 30_000  # 3 * $0.01 = $0.03 = 30,000 micros


def test_cache_multipliers():
    # cache read at 0.10x input rate: 1M cache-read tokens on Sonnet = $0.30.
    micros = compute_llm_micros("claude-sonnet-4-6", cache_read_tokens=1_000_000)
    assert micros == 300_000


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")

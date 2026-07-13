"""LLM cost model — one formula for the whole system (PRD §8 reuse note).

Ports HC Viral Hits' ``compute_cost_cents`` (agent_runner.py) but returns
**micro-USD** (``llm_micros``), the unit the governor's ``spend_ledger`` and caps
use. Pricing is per 1M tokens. Cache writes bill at 1.25×, cache reads at 0.10×
the input rate; Anthropic server-side web_search bills 1¢/request.

Keeping the table here (not scattered in agents) means the orchestrator's cost
estimates and the LLM client's actual charges use identical math.
"""

from __future__ import annotations

import re

# Per 1M tokens, USD. Superset of the IDs seen across the reference repos plus
# current models; unknown models fall back to DEFAULT and only bill web search.
_PRICING: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-haiku-3-5": (0.80, 4.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-opus-4-5": (5.00, 25.00),
    "claude-opus-4-6": (5.00, 25.00),
    "claude-opus-4-7": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-fable-5": (1.00, 5.00),
}
_DEFAULT = (3.00, 15.00)  # conservative: assume Sonnet-tier if unknown

_CACHE_WRITE_MULT = 1.25
_CACHE_READ_MULT = 0.10
_WEB_SEARCH_USD = 0.01
_DATED = re.compile(r"-\d{8}$")


def _normalize(model: str) -> str:
    return _DATED.sub("", model or "")


def price_per_million(model: str) -> tuple[float, float]:
    return _PRICING.get(_normalize(model), _DEFAULT)


def compute_llm_micros(
    model: str,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_creation_tokens: int = 0,
    cache_read_tokens: int = 0,
    web_search_requests: int = 0,
) -> int:
    """Return the cost of one LLM call in micro-USD (1e-6 USD)."""
    price_in, price_out = price_per_million(model)
    dollars = (
        input_tokens * price_in
        + output_tokens * price_out
        + cache_creation_tokens * price_in * _CACHE_WRITE_MULT
        + cache_read_tokens * price_in * _CACHE_READ_MULT
    ) / 1_000_000.0
    dollars += web_search_requests * _WEB_SEARCH_USD
    return round(dollars * 1_000_000)


def micros_to_usd(micros: int) -> float:
    return round(micros / 1_000_000.0, 6)

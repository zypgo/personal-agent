"""Token prices, used for per-run cost accounting and budget enforcement.

Prices are USD per 1M tokens (OpenAI pricing page, GPT-6 family). They are an estimate used to
stop runaway spend, not an invoice: keep them in sync with the pricing page when it changes.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Price:
    input: float
    cached_input: float
    output: float


PRICES: dict[str, Price] = {
    "gpt-6-astra": Price(10.00, 1.00, 50.00),
    "gpt-6.1-sol": Price(2.00, 0.10, 10.00),
    "gpt-6-sol": Price(2.00, 0.20, 10.00),
    "gpt-6.1-terra": Price(1.00, 0.05, 5.00),
    "gpt-6-luna": Price(0.10, 0.01, 0.50),
}

# Requests above this many input tokens are billed at 2x input/cache and 1.5x output.
LONG_CONTEXT_THRESHOLD = 272_000
CACHE_WRITE_MULTIPLIER = 1.25

_CONSERVATIVE = PRICES["gpt-6-astra"]


def price_for(model: str) -> Price | None:
    """Exact id, or a dated snapshot of a known id (`gpt-6.1-sol-2026-10-28`)."""
    for known in sorted(PRICES, key=len, reverse=True):
        if model == known or model.startswith(known + "-"):
            return PRICES[known]
    return None


def is_known(model: str) -> bool:
    return price_for(model) is not None


def cost_usd(
    model: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    """Estimated cost of one request. Unknown models are priced like the most expensive known
    one, so a typo or a new model can only make budgets stricter, never looser."""
    price = price_for(model) or _CONSERVATIVE
    long_context = input_tokens > LONG_CONTEXT_THRESHOLD
    in_rate = price.input * (2.0 if long_context else 1.0)
    cache_rate = price.cached_input * (2.0 if long_context else 1.0)
    out_rate = price.output * (1.5 if long_context else 1.0)
    fresh = max(input_tokens - cached_tokens - cache_write_tokens, 0)
    total = (
        fresh * in_rate
        + cached_tokens * cache_rate
        + cache_write_tokens * in_rate * CACHE_WRITE_MULTIPLIER
        + output_tokens * out_rate
    )
    return total / 1_000_000

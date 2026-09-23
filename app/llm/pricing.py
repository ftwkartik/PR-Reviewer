"""Approximate cost tracking. Prices are USD per million tokens and change over time: treat
the result as an estimate for budgeting, not as an invoice."""

from app.llm.base import LLMUsage

# model -> (input, output, cache_read)
PRICES: dict[str, tuple[float, float, float]] = {
    "claude-opus-5-5": (4.0, 20.0, 0.20),
    "claude-opus-5": (5.0, 25.0, 0.25),
    "claude-sonnet-5-5": (2.0, 10.0, 0.20),
    "claude-sonnet-5": (2.0, 10.0, 0.20),
    "claude-fable-5-1": (10.0, 50.0, 0.25),
    "claude-haiku-4-5": (1.0, 5.0, 0.10),
}
CACHE_WRITE_MULTIPLIER = 1.25  # relative to the input price


def estimate_cost_usd(model: str, usage: LLMUsage) -> float | None:
    """None when the model has no known price (never guess)."""
    price = PRICES.get(model)
    if price is None:
        return None
    inp, out, cache_read = price
    return (
        usage.input_tokens * inp
        + usage.output_tokens * out
        + usage.cache_read_tokens * cache_read
        + usage.cache_write_tokens * inp * CACHE_WRITE_MULTIPLIER
    ) / 1_000_000

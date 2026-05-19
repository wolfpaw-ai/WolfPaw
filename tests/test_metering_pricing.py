"""Pure-unit tests for cost computation — no DB needed."""

from datetime import datetime, timezone

from wolfpaw.metering.pricing import compute_cost_cents
from wolfpaw.metering.types import ModelPrice, TokenCounts


def _price(**overrides) -> ModelPrice:
    base = dict(
        model_id="claude-sonnet-4-6",
        input_per_mtok_cents=300,
        output_per_mtok_cents=1500,
        cache_read_per_mtok_cents=30,
        cache_write_per_mtok_cents=375,
        effective_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    base.update(overrides)
    return ModelPrice(**base)


def test_zero_usage_costs_zero():
    assert compute_cost_cents(_price(), TokenCounts()) == 0


def test_input_output_math():
    # 1M in + 1M out at $3/$15 = 300 + 1500 = 1800 cents
    cost = compute_cost_cents(
        _price(),
        TokenCounts(input_tokens=1_000_000, output_tokens=1_000_000),
    )
    assert cost == 1800


def test_cache_pricing_separate_dimensions():
    cost = compute_cost_cents(
        _price(),
        TokenCounts(cache_read_tokens=1_000_000, cache_write_tokens=1_000_000),
    )
    assert cost == 30 + 375


def test_rounds_up_never_undercharge():
    # 1000 input tokens at 300c/Mtok = 0.3 cents → round up to 1c
    cost = compute_cost_cents(_price(), TokenCounts(input_tokens=1_000))
    assert cost == 1


def test_pricing_uses_all_four_dimensions():
    cost = compute_cost_cents(
        _price(),
        TokenCounts(
            input_tokens=2_000_000,    # 600c
            output_tokens=500_000,     # 750c
            cache_read_tokens=3_000_000,  # 90c
            cache_write_tokens=400_000,   # 150c
        ),
    )
    assert cost == 600 + 750 + 90 + 150

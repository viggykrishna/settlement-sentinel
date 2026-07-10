"""Cost meter: pricing math must be exact, budget guard must fail safe."""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from cost_meter import PRICING, BudgetExceeded, CostMeter  # noqa: E402


def usage(inp=0, out=0, cw=0, cr=0):
    return SimpleNamespace(input_tokens=inp, output_tokens=out,
                           cache_creation_input_tokens=cw,
                           cache_read_input_tokens=cr)


def test_haiku_pricing_exact():
    m = CostMeter()
    # 1M input + 1M output = $1 + $5
    cost = m.record("claude-haiku-4-5", usage(inp=1_000_000, out=1_000_000))
    assert cost == pytest.approx(6.00)


def test_sonnet_pricing_exact():
    m = CostMeter()
    cost = m.record("claude-sonnet-4-6", usage(inp=1_000_000, out=1_000_000))
    assert cost == pytest.approx(18.00)


def test_cache_tokens_priced_separately():
    m = CostMeter()
    # cache write 1.25x input, cache read 0.1x input
    cost = m.record("claude-haiku-4-5",
                    usage(cw=1_000_000, cr=1_000_000))
    assert cost == pytest.approx(1.25 + 0.10)


def test_cache_read_is_90pct_cheaper_than_input():
    for model, rates in PRICING.items():
        assert rates["cache_read"] == pytest.approx(rates["input"] * 0.1), model


def test_total_accumulates_across_models():
    m = CostMeter()
    m.record("claude-haiku-4-5", usage(inp=100_000))     # $0.10
    m.record("claude-sonnet-4-6", usage(out=10_000))     # $0.15
    assert m.total_usd == pytest.approx(0.25)


def test_budget_guard_raises_before_overspend():
    m = CostMeter(budget_usd=0.10)
    m.record("claude-haiku-4-5", usage(inp=90_000))      # $0.09 spent
    m.guard(0.005, "small call")                          # fits
    with pytest.raises(BudgetExceeded):
        m.guard(0.02, "too-big call")                     # would exceed


def test_can_afford_drives_graceful_degradation():
    m = CostMeter(budget_usd=0.10)
    m.record("claude-haiku-4-5", usage(inp=95_000))      # $0.095 spent
    assert m.can_afford(0.004)
    assert not m.can_afford(0.006)


def test_summary_reports_within_budget_and_breakdown():
    m = CostMeter(budget_usd=0.10)
    m.record("claude-haiku-4-5", usage(inp=10_000, out=2_000, cr=50_000))
    s = m.summary()
    assert s["within_budget"] is True
    haiku = s["by_model"]["claude-haiku-4-5"]
    assert haiku["calls"] == 1
    assert haiku["cache_read_tokens"] == 50_000
    assert s["total_usd"] == pytest.approx(
        (10_000 * 1.0 + 2_000 * 5.0 + 50_000 * 0.10) / 1_000_000, abs=1e-9)


def test_missing_usage_fields_treated_as_zero():
    m = CostMeter()
    cost = m.record("claude-haiku-4-5",
                    SimpleNamespace(input_tokens=100, output_tokens=None))
    assert cost == pytest.approx(100 * 1.0 / 1_000_000)

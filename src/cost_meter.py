"""
Cost meter — real-time token accounting with a hard budget guard.

v4 exists to prove a claim: a full settlement window (hundreds of items)
can be triaged for well under $0.10. A claim like that is only credible if
the run itself measures and enforces it, the same way the matcher's
exactness is enforced by the eval harness rather than asserted in a README.

Every API call's usage block is recorded here, priced from the published
per-model rates (including prompt-cache reads and writes, which is where
most of the saving comes from), and checked against the budget BEFORE the
next call is made.

Degradation ladder when the budget runs low (enforced by the caller,
signalled by this meter):
  1. remaining budget < escalation estimate  -> skip the Sonnet second
     opinion; Haiku's triage stands, escalated items are annotated
     "escalation_skipped_budget".
  2. remaining budget < next triage turn     -> stop the loop; untriaged
     exceptions stay OPEN for a human (fail safe, never fail silent).

Prices are USD per million tokens. Cache writes cost 1.25x input (5-minute
TTL); cache reads cost 0.1x input.
"""

from __future__ import annotations

# USD per 1M tokens
PRICING = {
    "claude-haiku-4-5": {
        "input": 1.00, "output": 5.00,
        "cache_write": 1.25, "cache_read": 0.10,
    },
    "claude-sonnet-4-6": {
        "input": 3.00, "output": 15.00,
        "cache_write": 3.75, "cache_read": 0.30,
    },
}

_M = 1_000_000


class BudgetExceeded(RuntimeError):
    """Raised when a call would push spend past the hard budget."""


class CostMeter:
    def __init__(self, budget_usd: float = 0.10):
        self.budget_usd = budget_usd
        self.calls: list[dict] = []

    # ------------------------------------------------------------ recording

    def record(self, model: str, usage) -> float:
        """Price one API call from its usage block; returns cost in USD."""
        rates = PRICING[model]
        input_t = getattr(usage, "input_tokens", 0) or 0
        output_t = getattr(usage, "output_tokens", 0) or 0
        cache_w = getattr(usage, "cache_creation_input_tokens", 0) or 0
        cache_r = getattr(usage, "cache_read_input_tokens", 0) or 0
        cost = (
            input_t * rates["input"]
            + output_t * rates["output"]
            + cache_w * rates["cache_write"]
            + cache_r * rates["cache_read"]
        ) / _M
        self.calls.append({
            "model": model,
            "input_tokens": input_t,
            "output_tokens": output_t,
            "cache_write_tokens": cache_w,
            "cache_read_tokens": cache_r,
            "cost_usd": cost,
        })
        return cost

    # ------------------------------------------------------------- querying

    @property
    def total_usd(self) -> float:
        return sum(c["cost_usd"] for c in self.calls)

    @property
    def remaining_usd(self) -> float:
        return self.budget_usd - self.total_usd

    def can_afford(self, estimated_usd: float) -> bool:
        return self.remaining_usd >= estimated_usd

    def guard(self, estimated_usd: float, what: str) -> None:
        """Raise BudgetExceeded if the estimated call doesn't fit."""
        if not self.can_afford(estimated_usd):
            raise BudgetExceeded(
                f"budget guard: {what} (~${estimated_usd:.4f}) would exceed "
                f"the ${self.budget_usd:.2f} budget "
                f"(spent ${self.total_usd:.4f})")

    def summary(self) -> dict:
        by_model: dict[str, dict] = {}
        for c in self.calls:
            m = by_model.setdefault(c["model"], {
                "calls": 0, "input_tokens": 0, "output_tokens": 0,
                "cache_write_tokens": 0, "cache_read_tokens": 0,
                "cost_usd": 0.0,
            })
            m["calls"] += 1
            for k in ("input_tokens", "output_tokens",
                      "cache_write_tokens", "cache_read_tokens", "cost_usd"):
                m[k] += c[k]
        return {
            "budget_usd": self.budget_usd,
            "total_usd": round(self.total_usd, 6),
            "within_budget": self.total_usd <= self.budget_usd,
            "by_model": {
                k: {**v, "cost_usd": round(v["cost_usd"], 6)}
                for k, v in by_model.items()
            },
        }

"""
Scheme rulebook — the settlement semantics that ground triage decisions.

A generic engineer classifies exceptions by pattern. A scheme operator
classifies them by what the RULEBOOK says happens next: whether the rail is
instant or batch-net, whether a duplicate can still be recalled before
cutoff, how long the reconciliation window stays open, and who bears the
liability if it closes unresolved.

This module encodes those semantics per scheme profile so the triage agent
reasons from rules, not vibes. Two profiles are included:

  INSTANT_RAIL  — continuous-gross instant payment rail semantics
                  (the VIBER / RIX-INST / TIPS family of rails)
  BATCH_NET     — deferred net settlement batch semantics
                  (the GIRO / ACH family: fixed cycles, netting, recall
                  windows)

NOTE: These profiles describe publicly known settlement-model semantics in
generic form. No proprietary scheme rulebook content is reproduced here —
by design. In a live deployment, this module is where the participant's
actual rulebook extracts would be loaded.
"""

SCHEME_PROFILES = {
    "INSTANT_RAIL": {
        "model": "continuous gross settlement (RTGS-style, 24/7)",
        "finality": "immediate and irrevocable at posting — no recall "
                    "mechanism exists on the rail itself",
        "duplicate_semantics": (
            "A duplicate settlement entry means funds moved twice with "
            "finality. Recovery requires a separate return/refund flow "
            "initiated with the counterparty PSP, not a scheme-level "
            "cancellation. Time-critical: recovery probability drops "
            "sharply once the beneficiary account is drained."
        ),
        "missing_in_scheme_semantics": (
            "Instant rails have no batch cutoff, so a ledger booking with "
            "no scheme record is NOT explainable as a timing difference "
            "within the same rail — it indicates the payment was never "
            "actually settled (failed/timed-out but booked internally) or "
            "was booked against the wrong rail."
        ),
        "recon_window": "exceptions must be raised with the scheme operator "
                        "same-day; liability shifts to the participant after "
                        "the daily reconciliation attestation",
        "fees": "typically invoiced separately, NOT netted at settlement — "
                "an amount mismatch is rarely fee-explained on this rail",
    },
    "BATCH_NET": {
        "model": "deferred net settlement in fixed daily cycles",
        "finality": "at cycle settlement; entries submitted after cutoff "
                    "roll to the next cycle",
        "duplicate_semantics": (
            "A duplicate within the same cycle is usually a file "
            "resubmission error and can be recalled/adjusted before the "
            "cycle settles; after settlement it becomes a formal return "
            "under the scheme's return-reason codes."
        ),
        "missing_in_scheme_semantics": (
            "A ledger booking missing from the current cycle's settlement "
            "file is FREQUENTLY a cutoff/timing difference — it should "
            "appear in the next cycle. Check the next window before "
            "escalating; escalate only if absent there too."
        ),
        "recon_window": "cycle-based; discrepancies are raised via the "
                        "scheme's inter-participant investigation flow with "
                        "defined response SLAs",
        "fees": "commonly netted at settlement against gross amounts — an "
                "amount mismatch that exactly matches the fee schedule is "
                "a mapping fix, not money at risk",
    },
}


def get_scheme_rules(profile: str = "BATCH_NET") -> dict:
    """Return the settlement semantics for a scheme profile."""
    key = profile.upper()
    if key not in SCHEME_PROFILES:
        return {
            "error": f"unknown profile '{profile}'",
            "available": list(SCHEME_PROFILES),
        }
    return {"profile": key, **SCHEME_PROFILES[key]}


def rulebook_context(profile: str = "BATCH_NET") -> str:
    """Compact rulebook block for injection into the triage prompt."""
    rules = get_scheme_rules(profile)
    lines = [f"Scheme profile in force: {rules['profile']}"]
    for k, v in rules.items():
        if k != "profile":
            lines.append(f"- {k}: {v}")
    return "\n".join(lines)

"""
Approval gate + audit log — the consent boundary.

The core trust problem in agentic payments is not intelligence, it is
control: what may the agent do on its own, and what must a human approve?
This module enforces that boundary:

  * P3 + auto_resolvable  → closed automatically ("sentinel-auto"),
                            written to the audit log.
  * P1 / P2, or anything not auto_resolvable
                          → requires an explicit human decision. In
                            interactive mode the analyst approves/skips each
                            item at the terminal; in non-interactive mode
                            (--yes / CI) the items stay OPEN — the agent
                            never closes risky items without a human.

Every decision — automatic or human — is appended to an immutable JSONL
audit log (data/audit_log.jsonl): who, what, when, on what evidence.

Approved resolutions are also recorded into the resolution history, which
feeds the next window's triage as few-shot context. That closes the
learning loop: today's human decisions become tomorrow's agent calibration.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import history

AUDIT_LOG_PATH = Path(__file__).resolve().parent.parent / "data" / "audit_log.jsonl"


def _audit(entry: dict) -> None:
    AUDIT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    entry = {"ts": datetime.now().isoformat(timespec="seconds"), **entry}
    with open(AUDIT_LOG_PATH, "a") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def _exc_summary(exc) -> dict:
    row = exc.scheme_row or (exc.ledger_rows[0] if exc.ledger_rows else {})
    return {
        "bucket": exc.bucket,
        "settlement_ref": row.get("settlement_ref", "N/A"),
        "amount": row.get("amount", "N/A"),
    }


def run_gate(exceptions: list, triaged: list[dict],
             interactive: bool = True, analyst: str = "analyst") -> dict:
    """
    Apply the approval gate to triaged exceptions.

    Returns counts: {"auto_resolved": n, "approved": n, "open": n}
    and annotates each triage item with item["status"].
    """
    counts = {"auto_resolved": 0, "approved": 0, "open": 0}

    for item in triaged:
        exc = exceptions[item["index"]]
        summary = _exc_summary(exc)

        safe = item["severity"] == "P3" and item.get("auto_resolvable")

        if safe:
            item["status"] = "AUTO_RESOLVED"
            counts["auto_resolved"] += 1
            _audit({
                "actor": "sentinel-auto",
                "decision": "auto_resolve",
                "severity": item["severity"],
                **summary,
                "evidence": item.get("evidence", ""),
                "action": item["recommended_action"],
            })
            history.record_resolution(
                bucket=summary["bucket"],
                settlement_ref=summary["settlement_ref"],
                amount=summary["amount"],
                severity=item["severity"],
                root_cause=item["probable_root_cause"],
                action_taken=item["recommended_action"],
                resolved_by="sentinel-auto",
            )
            continue

        # risky item — needs a human
        if interactive:
            print(f"\n[{item['severity']}] {summary['bucket']} — "
                  f"ref {summary['settlement_ref']} — amount {summary['amount']}")
            print(f"  root cause : {item['probable_root_cause']}")
            print(f"  action     : {item['recommended_action']}")
            print(f"  evidence   : {item.get('evidence', '-')}")
            answer = input("  approve resolution? [y/N] ").strip().lower()
            if answer == "y":
                item["status"] = "APPROVED"
                counts["approved"] += 1
                _audit({
                    "actor": analyst,
                    "decision": "approve",
                    "severity": item["severity"],
                    **summary,
                    "action": item["recommended_action"],
                })
                history.record_resolution(
                    bucket=summary["bucket"],
                    settlement_ref=summary["settlement_ref"],
                    amount=summary["amount"],
                    severity=item["severity"],
                    root_cause=item["probable_root_cause"],
                    action_taken=item["recommended_action"],
                    resolved_by=analyst,
                )
                continue

        item["status"] = "OPEN"
        counts["open"] += 1
        _audit({
            "actor": analyst if interactive else "sentinel-auto",
            "decision": "left_open",
            "severity": item["severity"],
            **summary,
        })

    return counts

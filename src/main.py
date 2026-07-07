"""
Settlement Sentinel — CLI entry point.

Usage:
    python src/generate_data.py          # create sample scheme + ledger files
    python src/main.py                   # reconcile + Claude triage + report
    python src/main.py --no-ai           # deterministic reconciliation only
"""

import argparse
import csv
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from reconcile import reconcile  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
REPORTS = ROOT / "reports"

SEVERITY_ORDER = {"P1": 0, "P2": 1, "P3": 2}


def load(name):
    with open(DATA / name, newline="") as f:
        return list(csv.DictReader(f))


def write_report(matched, exceptions, triage_result):
    REPORTS.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = REPORTS / f"reconciliation_report_{stamp}.md"

    lines = [
        "# Settlement Reconciliation Report",
        f"Generated: {datetime.now().isoformat(timespec='seconds')}",
        "",
        "## Summary",
        f"- Matched transactions: **{matched}**",
        f"- Exceptions: **{len(exceptions)}**",
        "",
    ]

    if triage_result:
        lines += ["## Executive Summary (Claude)",
                  triage_result["executive_summary"], ""]

        triaged = sorted(
            triage_result["triaged"],
            key=lambda t: SEVERITY_ORDER.get(t["severity"], 9),
        )
        lines += ["## Triaged Exceptions", ""]
        for t in triaged:
            e = exceptions[t["index"]]
            ref = (e.scheme_row or (e.ledger_rows[0] if e.ledger_rows else {})).get(
                "settlement_ref", "N/A")
            amt = (e.scheme_row or (e.ledger_rows[0] if e.ledger_rows else {})).get(
                "amount", "N/A")
            lines += [
                f"### [{t['severity']}] {e.bucket} — ref `{ref}` — amount {amt}",
                f"- **Root cause:** {t['probable_root_cause']}",
                f"- **Action:** {t['recommended_action']}",
                f"- **Auto-resolvable:** {'yes' if t['auto_resolvable'] else 'no'}",
                "",
            ]
    else:
        lines += ["## Exceptions (untriaged)", ""]
        for e in exceptions:
            lines.append(f"- {e.bucket}: {json.dumps(e.to_dict(), default=str)[:160]}")

    path.write_text("\n".join(lines))
    return path


def main():
    parser = argparse.ArgumentParser(description="Settlement Sentinel")
    parser.add_argument("--no-ai", action="store_true",
                        help="skip Claude triage (deterministic recon only)")
    args = parser.parse_args()

    scheme = load("scheme_settlement.csv")
    ledger = load("internal_ledger.csv")
    print(f"loaded {len(scheme)} scheme rows, {len(ledger)} ledger rows")

    matched, exceptions = reconcile(scheme, ledger)
    print(f"matched: {matched}  |  exceptions: {len(exceptions)}")

    triage_result = None
    if not args.no_ai:
        from triage_agent import triage
        print("sending exceptions to Claude for triage...")
        triage_result = triage(exceptions)
        p1 = sum(1 for t in triage_result["triaged"] if t["severity"] == "P1")
        print(f"triage complete — {p1} P1 exception(s) flagged")

    path = write_report(matched, exceptions, triage_result)
    print(f"report -> {path}")


if __name__ == "__main__":
    main()

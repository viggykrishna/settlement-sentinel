"""
Settlement Sentinel — CLI entry point.

Usage:
    python src/generate_data.py
        create sample data: camt.053 scheme statement, ledger CSV,
        next-window file, fee schedule

    python src/main.py
        reconcile (camt.053 scheme file vs ledger) + agentic Claude triage
        + approval gate + report

    python src/main.py --scheme-format csv
        use the CSV scheme file instead of camt.053

    python src/main.py --scheme-file data/samples/camt053_danske_example.xml
        run against any camt.053 file, e.g. the public Danske Bank sample

    python src/main.py --no-ai          deterministic reconciliation only
    python src/main.py --yes            non-interactive: auto-resolve safe
                                        items, leave risky items OPEN
"""

import argparse
import csv
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from reconcile import reconcile            # noqa: E402
from adapters.camt053 import parse_camt053  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
REPORTS = ROOT / "reports"

SEVERITY_ORDER = {"P1": 0, "P2": 1, "P3": 2}
STATUS_ICON = {"AUTO_RESOLVED": "✅", "APPROVED": "✅", "OPEN": "⏳"}


def load_csv(path: Path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def impact_metrics(exceptions, triaged):
    """Quantified impact block for the ops lead."""
    money_at_risk = 0.0
    for t in triaged:
        if t["severity"] == "P1":
            e = exceptions[t["index"]]
            row = e.scheme_row or (e.ledger_rows[0] if e.ledger_rows else {})
            try:
                money_at_risk += float(row.get("amount", 0))
            except (TypeError, ValueError):
                pass
    auto = sum(1 for t in triaged if t.get("status") == "AUTO_RESOLVED")
    total = len(triaged)
    # conservative manual-effort baseline: ~6 min per exception triaged by hand
    analyst_minutes_saved = total * 6
    return {
        "money_at_risk": money_at_risk,
        "auto_resolved_pct": round(100 * auto / total, 1) if total else 0.0,
        "analyst_minutes_saved": analyst_minutes_saved,
    }


def write_report(matched, exceptions, triage_result, gate_counts, source_desc):
    REPORTS.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = REPORTS / f"reconciliation_report_{stamp}.md"

    lines = [
        "# Settlement Reconciliation Report",
        f"Generated: {datetime.now().isoformat(timespec='seconds')}",
        f"Scheme file source: {source_desc}",
        "",
        "## Summary",
        f"- Matched transactions: **{matched}**",
        f"- Exceptions: **{len(exceptions)}**",
    ]

    if triage_result:
        triaged = triage_result["triaged"]
        metrics = impact_metrics(exceptions, triaged)
        lines += [
            f"- Money at risk (P1 exposure): **{metrics['money_at_risk']:,.2f}**",
            f"- Auto-resolved safely: **{metrics['auto_resolved_pct']}%** of exceptions",
            f"- Estimated analyst time saved this window: **~{metrics['analyst_minutes_saved']} min**",
        ]
        if gate_counts:
            lines += [
                f"- Approval gate: {gate_counts['auto_resolved']} auto-resolved, "
                f"{gate_counts['approved']} human-approved, "
                f"{gate_counts['open']} left open",
            ]
        lines += ["", "## Executive Summary (Claude)",
                  triage_result["executive_summary"], ""]

        triaged_sorted = sorted(
            triaged, key=lambda t: SEVERITY_ORDER.get(t["severity"], 9))
        lines += ["## Triaged Exceptions", ""]
        for t in triaged_sorted:
            e = exceptions[t["index"]]
            row = e.scheme_row or (e.ledger_rows[0] if e.ledger_rows else {})
            ref = row.get("settlement_ref", "N/A")
            amt = row.get("amount", "N/A")
            icon = STATUS_ICON.get(t.get("status", ""), "")
            lines += [
                f"### [{t['severity']}] {e.bucket} — ref `{ref}` — amount {amt} {icon}",
                f"- **Status:** {t.get('status', 'UNTRIAGED')}",
                f"- **Root cause:** {t['probable_root_cause']}",
                f"- **Evidence:** {t.get('evidence', '-')}",
                f"- **Action:** {t['recommended_action']}",
                f"- **Auto-resolvable:** {'yes' if t['auto_resolvable'] else 'no'}",
            ]
            if t.get("review"):
                lines.append(f"- **Senior review (Sonnet):** {t['review']}")
            lines.append("")

        cost = triage_result.get("cost")
        if cost:
            lines += ["## Cost of This Run",
                      f"- Budget: **${cost['budget_usd']:.2f}** — spent "
                      f"**${cost['total_usd']:.4f}** "
                      f"({'within budget ✅' if cost['within_budget'] else 'OVER BUDGET ❌'})",
                      ""]
            for model, m in cost["by_model"].items():
                lines.append(
                    f"- `{model}`: {m['calls']} call(s), "
                    f"{m['input_tokens']:,} in / {m['output_tokens']:,} out, "
                    f"{m['cache_read_tokens']:,} cache-read / "
                    f"{m['cache_write_tokens']:,} cache-write tokens "
                    f"→ ${m['cost_usd']:.4f}")
            esc = triage_result.get("escalation")
            if esc:
                lines.append(f"- Escalation: {json.dumps(esc)}")
            lines.append("")

        log = triage_result.get("investigation_log", [])
        if log:
            lines += ["## Agent Investigation Log",
                      "_Every tool call the agent made before classifying — "
                      "the agent shows its working._", ""]
            for step in log:
                lines.append(
                    f"- `{step['tool']}` "
                    f"input={json.dumps(step['input'], default=str)} → "
                    f"{json.dumps(step['output'], default=str)[:180]}")
            lines.append("")
    else:
        lines += ["", "## Exceptions (untriaged)", ""]
        for e in exceptions:
            lines.append(f"- {e.bucket}: {json.dumps(e.to_dict(), default=str)[:160]}")

    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def main():
    parser = argparse.ArgumentParser(description="Settlement Sentinel")
    parser.add_argument("--no-ai", action="store_true",
                        help="skip Claude triage (deterministic recon only)")
    parser.add_argument("--scheme-format", choices=["camt053", "csv"],
                        default="camt053",
                        help="scheme file format (default: camt053)")
    parser.add_argument("--scheme-file", type=Path, default=None,
                        help="path to a scheme file (e.g. any camt.053 XML)")
    parser.add_argument("--ledger-file", type=Path,
                        default=DATA / "internal_ledger.csv")
    parser.add_argument("--yes", action="store_true",
                        help="non-interactive: auto-resolve safe items only, "
                             "leave risky items OPEN")
    parser.add_argument("--budget", type=float, default=0.10,
                        help="hard USD budget for AI triage (default 0.10); "
                             "the run degrades gracefully rather than exceed it")
    args = parser.parse_args()

    # --- load scheme file --------------------------------------------------
    if args.scheme_format == "camt053":
        scheme_path = args.scheme_file or DATA / "scheme_settlement.camt053.xml"
        scheme = parse_camt053(scheme_path)
        source_desc = f"ISO 20022 camt.053 — {scheme_path.name}"
    else:
        scheme_path = args.scheme_file or DATA / "scheme_settlement.csv"
        scheme = load_csv(scheme_path)
        source_desc = f"CSV — {scheme_path.name}"

    ledger = load_csv(args.ledger_file)
    print(f"loaded {len(scheme)} scheme rows ({source_desc}), "
          f"{len(ledger)} ledger rows")

    # --- deterministic reconciliation --------------------------------------
    matched, exceptions = reconcile(scheme, ledger)
    print(f"matched: {matched}  |  exceptions: {len(exceptions)}")

    triage_result, gate_counts = None, None
    if not args.no_ai and exceptions:
        from cost_meter import BudgetExceeded, CostMeter
        from triage_agent import triage
        from approval import run_gate
        meter = CostMeter(budget_usd=args.budget)
        print(f"agentic triage: Claude investigating exceptions "
              f"(budget ${args.budget:.2f})...")
        try:
            triage_result = triage(exceptions, meter=meter)
        except BudgetExceeded as exc:
            # fail safe: no triage is recorded, every exception stays OPEN
            print(f"TRIAGE STOPPED — {exc}")
            print("all exceptions remain OPEN for manual triage")
        if triage_result:
            p1 = sum(1 for t in triage_result["triaged"] if t["severity"] == "P1")
            cost = triage_result["cost"]
            print(f"triage complete — {p1} P1 exception(s) flagged, "
                  f"{len(triage_result.get('investigation_log', []))} investigation steps")
            print(f"cost: ${cost['total_usd']:.4f} of ${cost['budget_usd']:.2f} budget "
                  f"({'within budget' if cost['within_budget'] else 'OVER BUDGET'})")

            print("running approval gate...")
            gate_counts = run_gate(exceptions, triage_result["triaged"],
                                   interactive=not args.yes)
            print(f"gate: {gate_counts}")

    path = write_report(matched, exceptions, triage_result, gate_counts,
                        source_desc)
    print(f"report -> {path}")


if __name__ == "__main__":
    main()

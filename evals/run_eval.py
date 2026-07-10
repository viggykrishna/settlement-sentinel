"""
Settlement Sentinel — evaluation harness.

Generates a labeled synthetic evaluation set (every exception injected with
a known ground-truth bucket and expected severity), runs the pipeline, and
reports precision/recall per class.

Two layers, evaluated separately because they make different promises:

  LAYER 1 — deterministic matcher (offline, no API key)
    Ground truth: the injected exception bucket.
    Prediction:  the matcher's bucket.
    The matcher promises exactness — anything below 1.0 precision/recall
    here is a bug, not a tuning problem.

  LAYER 2 — agentic severity triage (--with-ai, needs ANTHROPIC_API_KEY)
    Ground truth: expected severity derived from the scheme rulebook
    semantics (BATCH_NET profile) + the known injection:
      duplicate scheme entry                          -> P1
      missing in ledger, amount>10k or DEBIT          -> P1
      missing in ledger, otherwise                    -> P2
      amount mismatch, delta == known fee             -> P3
      amount mismatch, delta != any fee               -> P2
      missing in scheme, present in next window       -> P3 (timing)
      missing in scheme, absent from next window      -> P2 (genuine gap)
      reference format drift                          -> P3
    Prediction: the triage agent's severity.

Usage:
    python evals/run_eval.py              # layer 1 only (offline)
    python evals/run_eval.py --with-ai    # layers 1 + 2
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import uuid
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from reconcile import reconcile  # noqa: E402

random.seed(7)

EVAL_DIR = Path(__file__).resolve().parent
BASE = datetime(2026, 7, 6)
NEXT_WINDOW_DATE = (BASE + timedelta(days=1)).strftime("%Y-%m-%d")
FEES = [1.5, 2.0, 4.5]

N_CLEAN = 60
N_PER_CASE_TYPE = 4     # 4 instances of each of 7 labeled case types = 28


def _txn():
    return {
        "settlement_ref": f"EVL{uuid.uuid4().hex[:12].upper()}",
        "amount": round(random.uniform(50, 95000), 2),
        "currency": "INR",
        "counterparty": "EVAL-MERCHANT",
        "value_date": BASE.strftime("%Y-%m-%d"),
        "timestamp": BASE.strftime("%Y-%m-%dT%H:%M:%S"),
        "direction": random.choice(["CREDIT", "DEBIT"]),
    }


def build_eval_set():
    """Returns (scheme_rows, ledger_rows, next_window_rows, labels).

    labels: settlement_ref -> {"bucket": ..., "severity": ...}
    """
    scheme, ledger, next_window = [], [], []
    labels: dict[str, dict] = {}

    for _ in range(N_CLEAN):
        t = _txn()
        scheme.append(dict(t))
        ledger.append(dict(t))

    def add(kind):
        t = _txn()
        ref = t["settlement_ref"]

        if kind == "duplicate":
            scheme.extend([dict(t), dict(t)])
            ledger.append(dict(t))
            labels[ref] = {"bucket": "DUPLICATE_SCHEME_ENTRY",
                           "severity": "P1"}

        elif kind == "missing_ledger_high":
            t["amount"] = round(random.uniform(20000, 95000), 2)
            scheme.append(dict(t))
            labels[ref] = {"bucket": "MISSING_IN_LEDGER", "severity": "P1"}

        elif kind == "missing_ledger_low":
            t["amount"] = round(random.uniform(50, 5000), 2)
            t["direction"] = "CREDIT"
            scheme.append(dict(t))
            labels[ref] = {"bucket": "MISSING_IN_LEDGER", "severity": "P2"}

        elif kind == "fee_mismatch":
            s, l = dict(t), dict(t)
            l["amount"] = round(t["amount"] - random.choice(FEES), 2)
            scheme.append(s)
            ledger.append(l)
            labels[ref] = {"bucket": "AMOUNT_MISMATCH", "severity": "P3"}

        elif kind == "nonfee_mismatch":
            s, l = dict(t), dict(t)
            l["amount"] = round(t["amount"] - random.uniform(37, 900), 2)
            scheme.append(s)
            ledger.append(l)
            labels[ref] = {"bucket": "AMOUNT_MISMATCH", "severity": "P2"}

        elif kind == "timing_gap":
            ledger.append(dict(t))
            nxt = dict(t)
            nxt["value_date"] = NEXT_WINDOW_DATE
            next_window.append(nxt)
            labels[ref] = {"bucket": "MISSING_IN_SCHEME_FILE",
                           "severity": "P3"}

        elif kind == "genuine_gap":
            ledger.append(dict(t))
            labels[ref] = {"bucket": "MISSING_IN_SCHEME_FILE",
                           "severity": "P2"}

        elif kind == "ref_drift":
            s, l = dict(t), dict(t)
            l["settlement_ref"] = ref[:10]
            scheme.append(s)
            ledger.append(l)
            labels[ref] = {"bucket": "REFERENCE_FORMAT_DRIFT",
                           "severity": "P3"}

    for kind in ["duplicate", "missing_ledger_high", "missing_ledger_low",
                 "fee_mismatch", "nonfee_mismatch", "timing_gap",
                 "genuine_gap", "ref_drift"]:
        for _ in range(N_PER_CASE_TYPE):
            add(kind)

    random.shuffle(scheme)
    random.shuffle(ledger)
    return scheme, ledger, next_window, labels


def prf(gold: list[str], pred: list[str]):
    """Per-class precision/recall/F1 and macro averages."""
    classes = sorted(set(gold) | set(pred))
    tp = defaultdict(int); fp = defaultdict(int); fn = defaultdict(int)
    for g, p in zip(gold, pred):
        if g == p:
            tp[g] += 1
        else:
            fp[p] += 1
            fn[g] += 1
    rows, macro_p, macro_r = [], [], []
    for c in classes:
        p = tp[c] / (tp[c] + fp[c]) if (tp[c] + fp[c]) else 0.0
        r = tp[c] / (tp[c] + fn[c]) if (tp[c] + fn[c]) else 0.0
        f1 = 2 * p * r / (p + r) if (p + r) else 0.0
        rows.append((c, p, r, f1, tp[c] + fn[c]))
        macro_p.append(p); macro_r.append(r)
    return rows, sum(macro_p) / len(macro_p), sum(macro_r) / len(macro_r)


def print_table(title, rows, mp, mr):
    print(f"\n{title}")
    print(f"{'class':28s} {'prec':>6s} {'rec':>6s} {'f1':>6s} {'n':>4s}")
    for c, p, r, f1, n in rows:
        print(f"{c:28s} {p:6.2f} {r:6.2f} {f1:6.2f} {n:4d}")
    print(f"{'MACRO':28s} {mp:6.2f} {mr:6.2f}")


def exception_ref(exc) -> str:
    row = exc.scheme_row or (exc.ledger_rows[0] if exc.ledger_rows else {})
    ref = row.get("settlement_ref", "")
    # ref-drift cases carry the truncated ref in the ledger; label key is
    # the full scheme-side ref
    if exc.scheme_row:
        return exc.scheme_row.get("settlement_ref", ref)
    return ref


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--with-ai", action="store_true",
                        help="also evaluate agentic severity triage "
                             "(requires ANTHROPIC_API_KEY)")
    parser.add_argument("--no-escalation", action="store_true",
                        help="Haiku tier only — measures what the Sonnet "
                             "second-opinion tier actually buys")
    args = parser.parse_args()

    scheme, ledger, next_window, labels = build_eval_set()

    # the agent's check_next_window tool reads this file — point it at the
    # eval next-window data for the duration of the run
    import investigation
    eval_next = EVAL_DIR / "eval_next_window.csv"
    import csv as _csv
    with open(eval_next, "w", newline="") as f:
        fieldnames = list(scheme[0].keys())
        w = _csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(next_window)
    investigation.NEXT_WINDOW_PATH = eval_next

    matched, exceptions = reconcile(scheme, ledger)
    print(f"eval set: {len(scheme)} scheme rows, {len(ledger)} ledger rows, "
          f"{len(labels)} labeled exceptions")
    print(f"matcher: {matched} matched, {len(exceptions)} exceptions raised")

    # ---- LAYER 1: bucket classification ----------------------------------
    gold_b, pred_b = [], []
    matched_excs = []
    for exc in exceptions:
        ref = exception_ref(exc)
        # find the label whose ref the exception ref starts with (handles
        # truncation) or equals
        label = labels.get(ref)
        if label is None:
            for lref, lab in labels.items():
                if lref.startswith(ref) or ref.startswith(lref):
                    label = lab
                    ref = lref
                    break
        if label is None:
            continue   # clean row misclassified as exception would land here
        gold_b.append(label["bucket"])
        pred_b.append(exc.bucket)
        matched_excs.append((exc, label))

    missed = len(labels) - len(gold_b)
    if missed:
        print(f"WARNING: {missed} labeled exceptions not raised by matcher")
    rows, mp, mr = prf(gold_b, pred_b)
    print_table("LAYER 1 — deterministic matcher (bucket classification)",
                rows, mp, mr)

    # ---- LAYER 2: agentic severity ----------------------------------------
    if args.with_ai:
        from triage_agent import triage
        tier_desc = ("Haiku only (--no-escalation)" if args.no_escalation
                     else "tiered: Haiku + Sonnet review")
        print(f"\nrunning agentic triage on eval exceptions — {tier_desc} "
              "(this calls the Anthropic API)...")
        result = triage(exceptions, batch_limit=len(exceptions),
                        verbose=False, escalate=not args.no_escalation)
        by_index = {t["index"]: t for t in result["triaged"]}
        gold_s, pred_s = [], []
        for i, exc in enumerate(exceptions):
            label = next((lab for e, lab in matched_excs if e is exc), None)
            t = by_index.get(i)
            if label and t:
                gold_s.append(label["severity"])
                pred_s.append(t["severity"])
        rows, mp, mr = prf(gold_s, pred_s)
        print_table(f"LAYER 2 — agentic severity triage ({tier_desc})",
                    rows, mp, mr)
        cost = result.get("cost", {})
        print(f"\ninvestigation steps taken: "
              f"{len(result.get('investigation_log', []))}")
        print(f"escalation: {json.dumps(result.get('escalation', {}))}")
        print(f"eval triage cost: ${cost.get('total_usd', 0):.4f}")

    eval_next.unlink(missing_ok=True)


if __name__ == "__main__":
    main()

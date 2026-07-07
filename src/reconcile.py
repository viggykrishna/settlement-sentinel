"""
Deterministic reconciliation engine.

Design principle (learned from running reconciliation ops for live payment
schemes): the matching itself must be deterministic and auditable — AI is
applied only *after* matching, to triage the exceptions. You never want a
probabilistic model deciding whether two ledger entries are the same money.

Matching strategy, in order:
  1. exact match on settlement_ref
  2. fuzzy match: ref-prefix + same amount + same value_date
     (catches legacy reference truncation without risking false positives)

Everything unmatched is classified into a raw exception bucket and handed to
the triage agent.
"""

from collections import defaultdict
from dataclasses import dataclass, field, asdict


@dataclass
class Exception_:
    bucket: str                 # raw mechanical classification
    scheme_row: dict | None
    ledger_rows: list = field(default_factory=list)

    def to_dict(self):
        return asdict(self)


def _index(rows):
    by_ref = defaultdict(list)
    for r in rows:
        by_ref[r["settlement_ref"]].append(r)
    return by_ref


def reconcile(scheme_rows: list[dict], ledger_rows: list[dict]):
    """Return (matched_count, exceptions)."""
    scheme_by_ref = _index(scheme_rows)
    ledger_by_ref = _index(ledger_rows)

    matched = 0
    exceptions: list[Exception_] = []
    consumed_ledger_refs = set()

    for ref, s_rows in scheme_by_ref.items():
        l_rows = ledger_by_ref.get(ref, [])

        # duplicates on the scheme side
        if len(s_rows) > 1:
            exceptions.append(Exception_(
                bucket="DUPLICATE_SCHEME_ENTRY",
                scheme_row=s_rows[0],
                ledger_rows=l_rows,
            ))
            consumed_ledger_refs.add(ref)
            continue

        s = s_rows[0]

        if l_rows:
            l = l_rows[0]
            consumed_ledger_refs.add(ref)
            if abs(float(s["amount"]) - float(l["amount"])) < 0.005:
                matched += 1
            else:
                exceptions.append(Exception_(
                    bucket="AMOUNT_MISMATCH",
                    scheme_row=s,
                    ledger_rows=[l],
                ))
            continue

        # fuzzy pass: truncated-reference match
        fuzzy = [
            l for l in ledger_rows
            if l["settlement_ref"] != ref
            and ref.startswith(l["settlement_ref"])
            and l["value_date"] == s["value_date"]
            and abs(float(l["amount"]) - float(s["amount"])) < 0.005
            and l["settlement_ref"] not in consumed_ledger_refs
        ]
        if fuzzy:
            consumed_ledger_refs.add(fuzzy[0]["settlement_ref"])
            exceptions.append(Exception_(
                bucket="REFERENCE_FORMAT_DRIFT",
                scheme_row=s,
                ledger_rows=[fuzzy[0]],
            ))
            continue

        exceptions.append(Exception_(
            bucket="MISSING_IN_LEDGER",
            scheme_row=s,
        ))

    # anything left in the ledger that never appeared in the scheme file
    for ref, l_rows in ledger_by_ref.items():
        if ref not in consumed_ledger_refs and ref not in scheme_by_ref:
            exceptions.append(Exception_(
                bucket="MISSING_IN_SCHEME_FILE",
                scheme_row=None,
                ledger_rows=l_rows,
            ))

    return matched, exceptions

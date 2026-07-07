"""
Generate realistic sample data: a scheme settlement file and an internal ledger.

Simulates the daily settlement file a payment scheme (e.g. an instant-payment
rail like GIRO/UPI) sends to a participant bank/PSP, plus the participant's
own internal ledger for the same window. Injects the exception types that
reconciliation teams actually deal with:

  - amount mismatches (fee deducted at scheme level, FX rounding)
  - missing in ledger (scheme settled, we never booked it)
  - missing in scheme file (we booked it, scheme never settled)
  - duplicate settlement entries
  - reference truncation / format drift
  - timing differences (booked either side of the cutoff)
"""

import csv
import random
import uuid
from datetime import datetime, timedelta
from pathlib import Path

random.seed(42)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
N_CLEAN = 180          # transactions that reconcile perfectly
N_EXCEPTIONS = 20      # transactions with injected issues

MERCHANTS = [
    "KIRANA-STORE-4411", "SWIFTCART-IN", "METRO-FUEL-22", "CLOUDKITCHEN-88",
    "PHARMA-PLUS-BLR", "TRAVELDESK-DEL", "EDUTECH-PRIME", "GROCER-DAILY-77",
]


def _txn(base_time: datetime):
    ref = f"STL{uuid.uuid4().hex[:12].upper()}"
    amount = round(random.uniform(50, 95000), 2)
    ts = base_time + timedelta(seconds=random.randint(0, 86_000))
    return {
        "settlement_ref": ref,
        "amount": amount,
        "currency": "INR",
        "counterparty": random.choice(MERCHANTS),
        "value_date": ts.strftime("%Y-%m-%d"),
        "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%S"),
        "direction": random.choice(["CREDIT", "DEBIT"]),
    }


def generate():
    DATA_DIR.mkdir(exist_ok=True)
    base = datetime(2026, 7, 6)

    scheme_rows, ledger_rows = [], []

    # --- clean, matching transactions -------------------------------------
    for _ in range(N_CLEAN):
        t = _txn(base)
        scheme_rows.append(dict(t))
        ledger_rows.append(dict(t))

    # --- injected exceptions ----------------------------------------------
    for i in range(N_EXCEPTIONS):
        t = _txn(base)
        kind = i % 5

        if kind == 0:  # amount mismatch: scheme nets a processing fee
            s = dict(t)
            l = dict(t)
            l["amount"] = round(t["amount"] - random.choice([1.5, 2.0, 4.5]), 2)
            scheme_rows.append(s)
            ledger_rows.append(l)

        elif kind == 1:  # settled by scheme, never booked internally
            scheme_rows.append(dict(t))

        elif kind == 2:  # booked internally, missing from scheme file
            ledger_rows.append(dict(t))

        elif kind == 3:  # duplicate settlement entry in scheme file
            scheme_rows.append(dict(t))
            scheme_rows.append(dict(t))
            ledger_rows.append(dict(t))

        else:  # reference truncated by legacy formatting in ledger
            s = dict(t)
            l = dict(t)
            l["settlement_ref"] = t["settlement_ref"][:10]
            scheme_rows.append(s)
            ledger_rows.append(l)

    random.shuffle(scheme_rows)
    random.shuffle(ledger_rows)

    for name, rows in [("scheme_settlement.csv", scheme_rows),
                       ("internal_ledger.csv", ledger_rows)]:
        path = DATA_DIR / name
        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        print(f"wrote {len(rows):4d} rows -> {path}")


if __name__ == "__main__":
    generate()

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from reconcile import reconcile


def row(ref, amount, date="2026-07-06"):
    return {
        "settlement_ref": ref, "amount": amount, "currency": "INR",
        "counterparty": "TEST", "value_date": date,
        "timestamp": f"{date}T10:00:00", "direction": "CREDIT",
    }


def test_perfect_match():
    s = [row("STLAAA111", 100.0)]
    l = [row("STLAAA111", 100.0)]
    matched, ex = reconcile(s, l)
    assert matched == 1 and not ex


def test_amount_mismatch():
    matched, ex = reconcile([row("STLBBB222", 100.0)], [row("STLBBB222", 98.0)])
    assert matched == 0
    assert ex[0].bucket == "AMOUNT_MISMATCH"


def test_missing_in_ledger():
    matched, ex = reconcile([row("STLCCC333", 55.5)], [])
    assert ex[0].bucket == "MISSING_IN_LEDGER"


def test_missing_in_scheme_file():
    matched, ex = reconcile([], [row("STLDDD444", 77.0)])
    assert ex[0].bucket == "MISSING_IN_SCHEME_FILE"


def test_duplicate_scheme_entry():
    matched, ex = reconcile(
        [row("STLEEE555", 10.0), row("STLEEE555", 10.0)],
        [row("STLEEE555", 10.0)],
    )
    assert ex[0].bucket == "DUPLICATE_SCHEME_ENTRY"


def test_reference_truncation_fuzzy_match():
    matched, ex = reconcile(
        [row("STLFFF666XYZ", 42.0)],
        [row("STLFFF666", 42.0)],
    )
    assert ex[0].bucket == "REFERENCE_FORMAT_DRIFT"

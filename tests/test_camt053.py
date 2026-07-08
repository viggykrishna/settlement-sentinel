"""
camt.053 adapter tests — run against the REAL public Danske Bank sample file
(data/samples/camt053_danske_example.xml), not synthetic data, so the parser
is proven against a bank-authored statement.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from adapters.camt053 import parse_camt053, extract_reference_from_remit  # noqa: E402

SAMPLE = ROOT / "data" / "samples" / "camt053_danske_example.xml"


def rows():
    return parse_camt053(SAMPLE)


def test_row_count_explodes_bulk_entries():
    # 6 <Ntry> elements, one of which is a bulk of 2 -> 7 transactions
    assert len(rows()) == 7


def test_reference_priority_txid_over_notprovided():
    # entry 1: EndToEndId is NOTPROVIDED, TxId is the real reference
    r = rows()[0]
    assert r["settlement_ref"] == "3825-0123456789"


def test_bulk_entry_uses_structured_creditor_reference():
    refs = {r["settlement_ref"] for r in rows()}
    assert "71/0000123456789012345" in refs
    assert "71/0000543210987654321" in refs


def test_bulk_entry_transaction_amounts_not_entry_amount():
    amounts = sorted(r["amount"] for r in rows()
                     if r["settlement_ref"].startswith("71/"))
    assert amounts == [528.94, 572.94]   # not the bulked 1101.88


def test_direction_mapping():
    by_ref = {r["settlement_ref"]: r for r in rows()}
    assert by_ref["3825-0123456789"]["direction"] == "CREDIT"
    # the outgoing payment's EndToEndId is populated (with placeholder text
    # in the public sample) so it wins the reference priority ladder
    assert by_ref["EndToEndId"]["direction"] == "DEBIT"


def test_value_date_extracted():
    r = rows()[0]
    assert r["value_date"] == "2023-04-20"


def test_remittance_info_carried_through():
    by_ref = {r["settlement_ref"]: r for r in rows()}
    remit = by_ref["1234569988"]["remit_info"]
    assert "Invoice number 11223344" in remit


def test_reference_extraction_from_free_text():
    assert extract_reference_from_remit("Invoice number 11223344") == "11223344"
    assert extract_reference_from_remit("no reference here at all") is None


def test_rows_feed_the_matcher():
    """Adapter output must be consumable by the deterministic matcher."""
    from reconcile import reconcile
    scheme = rows()
    ledger = [dict(r) for r in scheme]        # perfect ledger copy
    matched, exceptions = reconcile(scheme, ledger)
    assert matched == len(scheme)
    assert exceptions == []

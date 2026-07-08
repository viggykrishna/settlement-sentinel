"""
ISO 20022 camt.053 (BankToCustomerStatement) adapter.

camt.053 is the end-of-day account statement standard used across SEPA and by
most European banks (it replaces SWIFT MT940). This adapter maps a real
camt.053 XML file into the minimal internal row shape the deterministic
matcher needs:

    { settlement_ref, amount, currency, value_date, direction,
      counterparty, remit_info }

Real-world quirks handled (the reasons reconciliation is a job, not a script):

  * Namespace differences between camt.053 versions (001.02 vs 001.08 etc.)
    — parsing is namespace-agnostic.
  * Bulk/batch entries: one <Ntry> can contain several <TxDtls>, each a
    separate transaction with its own amount and reference. We explode them.
  * Reference priority: banks populate references inconsistently. We take,
    in order: EndToEndId (ignoring the literal "NOTPROVIDED"), TxId,
    structured creditor reference (RmtInf/Strd/CdtrRefInf/Ref), AcctSvcrRef,
    and finally the entry's NtryRef.
  * The reference an ops analyst actually needs is often buried in the
    unstructured remittance text — we carry <RmtInf><Ustrd> through as
    `remit_info` so the triage agent can see it.

Only the Python standard library is used (xml.etree.ElementTree).

Public sample files this adapter is tested against:
  * Danske Bank camt.053 example (data/samples/camt053_danske_example.xml)
    https://danskeci.com/-/media/pdf/danskeci-com/iso-20022-xml/camt053_dk_example.xml
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

NOT_PROVIDED = {"NOTPROVIDED", "NOT PROVIDED", ""}


def _strip_ns(tag: str) -> str:
    """'{urn:iso:std:iso:20022...}Ntry' -> 'Ntry'."""
    return tag.rsplit("}", 1)[-1]


def _find(elem, path: str):
    """Namespace-agnostic single-element find using local tag names."""
    parts = path.split("/")
    current = [elem]
    for part in parts:
        nxt = []
        for c in current:
            nxt.extend(ch for ch in c if _strip_ns(ch.tag) == part)
        current = nxt
        if not current:
            return None
    return current[0]


def _findall(elem, path: str):
    parts = path.split("/")
    current = [elem]
    for part in parts:
        nxt = []
        for c in current:
            nxt.extend(ch for ch in c if _strip_ns(ch.tag) == part)
        current = nxt
    return current


def _text(elem, path: str, default: str = "") -> str:
    node = _find(elem, path)
    return (node.text or "").strip() if node is not None else default


def _pick_reference(txdtls, entry) -> str:
    """Apply the reference priority ladder a reconciliation analyst uses."""
    candidates = []
    if txdtls is not None:
        candidates += [
            _text(txdtls, "Refs/EndToEndId"),
            _text(txdtls, "Refs/TxId"),
            _text(txdtls, "RmtInf/Strd/CdtrRefInf/Ref"),
        ]
    candidates += [
        _text(entry, "AcctSvcrRef"),
        _text(entry, "NtryRef"),
    ]
    for c in candidates:
        if c and c.upper() not in NOT_PROVIDED:
            return c
    return "UNKNOWN"


def _remit_info(txdtls) -> str:
    if txdtls is None:
        return ""
    ustrd = [
        (n.text or "").strip()
        for n in _findall(txdtls, "RmtInf/Ustrd")
    ]
    return " | ".join(u for u in ustrd if u)


def extract_reference_from_remit(remit: str) -> str | None:
    """
    Best-effort extraction of a reference buried in free-text remittance
    info (e.g. 'Invoice number 11223344'). Deterministic and conservative:
    returns the first token that looks like a reference, or None.
    Exposed as a triage-agent tool as well.
    """
    m = re.search(r"\b([A-Z]{2,4}[-/]?\d{6,}|\d{8,})\b", remit or "")
    return m.group(1) if m else None


def parse_camt053(path: str | Path) -> list[dict]:
    """
    Parse a camt.053 file into internal scheme rows.

    Bulk entries (<Btch><NbOfTxs> > 1) are exploded into one row per TxDtls,
    because each underlying transaction reconciles separately even though the
    bank booked them as a single entry.
    """
    tree = ET.parse(str(path))
    root = tree.getroot()

    rows: list[dict] = []
    for stmt in _findall(root, "BkToCstmrStmt/Stmt"):
        stmt_ccy = _text(stmt, "Acct/Ccy")
        for entry in _findall(stmt, "Ntry"):
            entry_amt_node = _find(entry, "Amt")
            entry_amt = float(entry_amt_node.text) if entry_amt_node is not None else 0.0
            entry_ccy = (entry_amt_node.get("Ccy") if entry_amt_node is not None else "") or stmt_ccy
            direction = _text(entry, "CdtDbtInd") or "CRDT"
            direction = "CREDIT" if direction == "CRDT" else "DEBIT"
            value_date = _text(entry, "ValDt/Dt") or _text(entry, "BookgDt/Dt")

            txdtls_list = _findall(entry, "NtryDtls/TxDtls")

            if len(txdtls_list) <= 1:
                tx = txdtls_list[0] if txdtls_list else None
                # prefer the transaction-level amount when present
                amt_text = _text(tx, "AmtDtls/TxAmt/Amt") if tx is not None else ""
                amount = float(amt_text) if amt_text else entry_amt
                rows.append({
                    "settlement_ref": _pick_reference(tx, entry),
                    "amount": amount,
                    "currency": entry_ccy,
                    "value_date": value_date,
                    "direction": direction,
                    "counterparty": (
                        _text(tx, "RltdPties/Dbtr/Nm")
                        or _text(tx, "RltdPties/Cdtr/Nm")
                        if tx is not None else ""
                    ),
                    "remit_info": _remit_info(tx),
                })
            else:
                # bulk entry: one row per underlying transaction
                for tx in txdtls_list:
                    amt_text = _text(tx, "AmtDtls/TxAmt/Amt")
                    rows.append({
                        "settlement_ref": _pick_reference(tx, entry),
                        "amount": float(amt_text) if amt_text else entry_amt,
                        "currency": entry_ccy,
                        "value_date": value_date,
                        "direction": direction,
                        "counterparty": (
                            _text(tx, "RltdPties/Dbtr/Nm")
                            or _text(tx, "RltdPties/Cdtr/Nm")
                        ),
                        "remit_info": _remit_info(tx),
                    })
    return rows

"""
Investigation tools for the triage agent.

These are the checks a senior reconciliation analyst performs *before*
classifying an exception — encoded as tools Claude can call mid-reasoning:

  check_next_window(ref)        Did a "missing in scheme file" item simply
                                settle in the next window (cutoff timing)?
  lookup_history(...)           Have we seen this pattern before, and how
                                was it resolved?
  get_fee_schedule()            Known scheme fees — is an amount mismatch
                                explained by a fee netted at settlement?
  extract_reference(remit)      Pull a probable reference out of free-text
                                remittance info (camt.053 Ustrd field).

Every tool is deterministic, read-only, and logged. The agent investigates;
it never mutates anything. Mutation happens only behind the approval gate.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import history
from adapters.camt053 import extract_reference_from_remit

DATA = Path(__file__).resolve().parent.parent / "data"

FEE_SCHEDULE_PATH = DATA / "fee_schedule.json"
NEXT_WINDOW_PATH = DATA / "scheme_settlement_next_window.csv"


# ---------------------------------------------------------------- tools ---

def check_next_window(settlement_ref: str) -> dict:
    """Look for a reference in the next settlement window's file."""
    if not NEXT_WINDOW_PATH.exists():
        return {"found": False, "note": "next window file not yet received"}
    with open(NEXT_WINDOW_PATH, newline="") as f:
        for row in csv.DictReader(f):
            if row["settlement_ref"] == settlement_ref:
                return {"found": True, "row": row,
                        "note": "settled in next window — timing/cutoff difference"}
    return {"found": False, "note": "not present in next window either"}


def lookup_history(bucket: str | None = None,
                   ref_contains: str | None = None) -> dict:
    matches = history.lookup(bucket=bucket, ref_contains=ref_contains)
    return {"matches": matches, "count": len(matches)}


def get_fee_schedule() -> dict:
    if FEE_SCHEDULE_PATH.exists():
        return json.loads(FEE_SCHEDULE_PATH.read_text())
    return {"fees": []}


def extract_reference(remit_info: str) -> dict:
    ref = extract_reference_from_remit(remit_info)
    return {"extracted_reference": ref}


# --------------------------------------------------- Anthropic tool spec ---

TOOL_DEFINITIONS = [
    {
        "name": "check_next_window",
        "description": (
            "Check whether a settlement reference appears in the NEXT "
            "settlement window's scheme file. Use this before escalating a "
            "MISSING_IN_SCHEME_FILE exception — if the transaction settled "
            "one window later it is a cutoff/timing difference, not a gap."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "settlement_ref": {"type": "string"},
            },
            "required": ["settlement_ref"],
        },
    },
    {
        "name": "lookup_history",
        "description": (
            "Look up how similar exceptions were resolved in the past. "
            "Filter by bucket (e.g. AMOUNT_MISMATCH) and/or a reference "
            "substring. Use this to calibrate severity and recommended "
            "action against this team's actual past decisions."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "bucket": {"type": "string"},
                "ref_contains": {"type": "string"},
            },
        },
    },
    {
        "name": "get_fee_schedule",
        "description": (
            "Return the known scheme fee schedule. Use this to verify "
            "whether an AMOUNT_MISMATCH delta exactly matches a known fee "
            "netted at settlement — if so it is a P3 fee-mapping fix, not "
            "money at risk."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "extract_reference",
        "description": (
            "Extract a probable settlement reference from free-text "
            "remittance information (camt.053 unstructured Ustrd field). "
            "Useful when the structured reference is missing or UNKNOWN."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "remit_info": {"type": "string"},
            },
            "required": ["remit_info"],
        },
    },
]

TOOL_FUNCTIONS = {
    "check_next_window": lambda inp: check_next_window(inp["settlement_ref"]),
    "lookup_history": lambda inp: lookup_history(inp.get("bucket"),
                                                 inp.get("ref_contains")),
    "get_fee_schedule": lambda inp: get_fee_schedule(),
    "extract_reference": lambda inp: extract_reference(inp["remit_info"]),
}

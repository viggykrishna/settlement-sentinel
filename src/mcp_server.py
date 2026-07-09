"""
Settlement Sentinel — MCP server.

Exposes the reconciliation engine and every investigation tool over the
Model Context Protocol, so Claude (Desktop, Code, or any MCP client) can
drive the whole workflow conversationally:

    "Reconcile today's camt.053 against the ledger, investigate the
     exceptions, and tell me which ones I need to look at."

DESIGN DECISION — where does the intelligence live?
The CLI mode (src/main.py) drives Claude via the API: our code is the
runtime, Claude is the reasoning engine. In MCP mode that inverts: Claude
in the client IS the triage agent, and this server exposes only
deterministic, auditable domain tools. There is deliberately no nested
"call Claude from inside the tool" here — the model reasoning should happen
in one place, visible to the user, not hidden inside a tool result.

CONTROL BOUNDARY — same rules as the CLI approval gate:
Every tool is read-only except record_resolution. MCP clients surface tool
calls for user approval, which is exactly the consent boundary the CLI gate
enforces: the agent investigates freely, but a resolution is only recorded
through an explicit, user-visible action. record_resolution also appends to
the immutable audit log with actor attribution.

Run:
    python src/mcp_server.py            # stdio transport

Claude Desktop config (claude_desktop_config.json):
    {
      "mcpServers": {
        "settlement-sentinel": {
          "command": "python",
          "args": ["C:/path/to/settlement-sentinel/src/mcp_server.py"]
        }
      }
    }
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp.server.fastmcp import FastMCP

import history as history_store
import investigation
from adapters.camt053 import parse_camt053
from reconcile import reconcile as run_reconcile
from rulebook import get_scheme_rules as rules_lookup

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

mcp = FastMCP("settlement-sentinel")


def _load_csv(path: Path) -> list[dict]:
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


@mcp.tool()
def reconcile_settlement(scheme_file: str = "", ledger_file: str = "",
                         scheme_format: str = "camt053") -> str:
    """Run deterministic reconciliation between a scheme settlement file and
    the internal ledger. Returns matched count and the full exception list
    (bucket, scheme row, ledger rows) as JSON. Matching is exact-reference
    first, then a conservative fuzzy pass; it is deterministic and auditable
    — no model judgment is involved in matching. scheme_format is 'camt053'
    (ISO 20022 XML) or 'csv'."""
    s_path = Path(scheme_file) if scheme_file else (
        DATA / ("scheme_settlement.camt053.xml" if scheme_format == "camt053"
                else "scheme_settlement.csv"))
    l_path = Path(ledger_file) if ledger_file else DATA / "internal_ledger.csv"

    scheme = (parse_camt053(s_path) if scheme_format == "camt053"
              else _load_csv(s_path))
    ledger = _load_csv(l_path)
    matched, exceptions = run_reconcile(scheme, ledger)
    return json.dumps({
        "scheme_file": str(s_path),
        "scheme_rows": len(scheme),
        "ledger_rows": len(ledger),
        "matched": matched,
        "exception_count": len(exceptions),
        "exceptions": [
            {"index": i, "bucket": e.bucket, "scheme_row": e.scheme_row,
             "ledger_rows": e.ledger_rows}
            for i, e in enumerate(exceptions)
        ],
    }, default=str)


@mcp.tool()
def parse_camt053_file(path: str) -> str:
    """Parse any ISO 20022 camt.053 bank statement XML into normalized
    transaction rows (settlement_ref, amount, currency, value_date,
    direction, counterparty, remit_info). Handles bulk-entry explosion and
    the reference priority ladder (EndToEndId > TxId > structured creditor
    ref > AcctSvcrRef > NtryRef, skipping NOTPROVIDED)."""
    return json.dumps(parse_camt053(path), default=str)


@mcp.tool()
def get_scheme_rules(profile: str = "BATCH_NET") -> str:
    """Load the settlement semantics in force for a scheme profile
    ('BATCH_NET' or 'INSTANT_RAIL'): settlement model, finality, duplicate
    handling, whether missing-in-scheme can be a timing difference, recon
    window, fee treatment. Ground every severity decision in these rules."""
    return json.dumps(rules_lookup(profile))


@mcp.tool()
def check_next_window(settlement_ref: str) -> str:
    """Check whether a settlement reference appears in the NEXT settlement
    window's scheme file. Use before escalating a MISSING_IN_SCHEME_FILE
    exception on a batch-net scheme — found next window means a
    cutoff/timing difference, not a genuine gap."""
    return json.dumps(investigation.check_next_window(settlement_ref),
                      default=str)


@mcp.tool()
def get_fee_schedule() -> str:
    """Return the known scheme fee schedule, to verify whether an
    AMOUNT_MISMATCH delta exactly matches a fee netted at settlement (a P3
    fee-mapping fix rather than money at risk)."""
    return json.dumps(investigation.get_fee_schedule())


@mcp.tool()
def lookup_resolution_history(bucket: str = "", ref_contains: str = "") -> str:
    """Look up how similar exceptions were resolved before (the learning
    loop). Filter by bucket (e.g. AMOUNT_MISMATCH) and/or reference
    substring. Align severity and action with the team's past decisions
    unless the facts differ."""
    return json.dumps(investigation.lookup_history(
        bucket or None, ref_contains or None), default=str)


@mcp.tool()
def extract_reference(remit_info: str) -> str:
    """Extract a probable settlement reference from free-text remittance
    information (camt.053 unstructured Ustrd field)."""
    return json.dumps(investigation.extract_reference(remit_info))


@mcp.tool()
def record_resolution(bucket: str, settlement_ref: str, amount: str,
                      severity: str, root_cause: str, action_taken: str,
                      resolved_by: str) -> str:
    """Record an APPROVED exception resolution into the resolution history
    and the immutable audit log. THE ONLY WRITE TOOL ON THIS SERVER — call
    it only after the user has explicitly confirmed the resolution in
    conversation. resolved_by must identify the human approver (never an
    agent name). The recorded resolution feeds future triage as few-shot
    context."""
    from approval import _audit  # immutable audit trail
    history_store.record_resolution(
        bucket=bucket, settlement_ref=settlement_ref, amount=amount,
        severity=severity, root_cause=root_cause,
        action_taken=action_taken, resolved_by=resolved_by)
    _audit({"actor": resolved_by, "decision": "approve_via_mcp",
            "severity": severity, "bucket": bucket,
            "settlement_ref": settlement_ref, "amount": amount,
            "action": action_taken})
    return json.dumps({"recorded": True, "settlement_ref": settlement_ref})


@mcp.tool()
def read_audit_log(limit: int = 20) -> str:
    """Read the most recent entries from the immutable audit log — every
    resolution decision, automatic or human, with actor attribution."""
    path = DATA / "audit_log.jsonl"
    if not path.exists():
        return json.dumps({"entries": []})
    lines = path.read_text().strip().splitlines()[-limit:]
    return json.dumps({"entries": [json.loads(l) for l in lines]})


if __name__ == "__main__":
    mcp.run()

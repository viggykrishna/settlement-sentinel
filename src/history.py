"""
Resolution history — the learning loop.

Every approved resolution is persisted here. Two uses:

  1. Few-shot context: the most recent resolutions are injected into the
     triage prompt, so the agent's judgment converges toward how *this*
     ops team actually resolves things (README next step: "feed resolved
     exceptions back as few-shot examples").
  2. Investigation tool: during triage the agent can call
     lookup_history(reference_pattern | bucket) to check whether a similar
     exception was seen before and how it was resolved.

Storage is a plain JSON file (data/resolution_history.json) — deliberately
boring and auditable. In production this would be a table.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

HISTORY_PATH = Path(__file__).resolve().parent.parent / "data" / "resolution_history.json"


def _load() -> list[dict]:
    if HISTORY_PATH.exists():
        return json.loads(HISTORY_PATH.read_text())
    return []


def _save(entries: list[dict]) -> None:
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    HISTORY_PATH.write_text(json.dumps(entries, indent=2, default=str))


def record_resolution(bucket: str, settlement_ref: str, amount,
                      severity: str, root_cause: str, action_taken: str,
                      resolved_by: str) -> None:
    entries = _load()
    entries.append({
        "resolved_at": datetime.now().isoformat(timespec="seconds"),
        "bucket": bucket,
        "settlement_ref": settlement_ref,
        "amount": amount,
        "severity": severity,
        "root_cause": root_cause,
        "action_taken": action_taken,
        "resolved_by": resolved_by,   # "sentinel-auto" or analyst name
    })
    _save(entries)


def recent(n: int = 8) -> list[dict]:
    """Most recent resolutions, for few-shot context in the triage prompt."""
    return _load()[-n:]


def lookup(bucket: str | None = None, ref_contains: str | None = None,
           limit: int = 5) -> list[dict]:
    """Investigation tool: how were similar exceptions resolved before?"""
    entries = _load()
    out = []
    for e in reversed(entries):
        if bucket and e["bucket"] != bucket:
            continue
        if ref_contains and ref_contains.upper() not in str(e["settlement_ref"]).upper():
            continue
        out.append(e)
        if len(out) >= limit:
            break
    return out

"""Approval gate: the consent boundary must hold in code, not prose."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import approval  # noqa: E402
import history  # noqa: E402


def make_exc(bucket="DUPLICATE_SCHEME_ENTRY", ref="STLTEST01", amount=5000.0):
    return SimpleNamespace(
        bucket=bucket,
        scheme_row={"settlement_ref": ref, "amount": amount},
        ledger_rows=[],
    )


def item(severity, auto_resolvable, index=0):
    return {
        "index": index,
        "severity": severity,
        "auto_resolvable": auto_resolvable,
        "probable_root_cause": "test cause",
        "recommended_action": "test action",
        "evidence": "test evidence",
    }


def redirect_stores(tmp_path, monkeypatch):
    monkeypatch.setattr(approval, "AUDIT_LOG_PATH", tmp_path / "audit.jsonl")
    monkeypatch.setattr(history, "HISTORY_PATH", tmp_path / "history.json")


def read_audit(tmp_path):
    p = tmp_path / "audit.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines()]


def test_p3_auto_resolvable_closes_automatically(tmp_path, monkeypatch):
    redirect_stores(tmp_path, monkeypatch)
    excs = [make_exc(bucket="REFERENCE_FORMAT_DRIFT")]
    triaged = [item("P3", True)]
    counts = approval.run_gate(excs, triaged, interactive=False)
    assert counts == {"auto_resolved": 1, "approved": 0, "open": 0}
    assert triaged[0]["status"] == "AUTO_RESOLVED"


def test_p1_never_auto_closes_non_interactive(tmp_path, monkeypatch):
    """The core safety property: risky items stay OPEN without a human."""
    redirect_stores(tmp_path, monkeypatch)
    excs = [make_exc()]
    for severity, auto in [("P1", False), ("P1", True), ("P2", False)]:
        triaged = [item(severity, auto)]
        counts = approval.run_gate(excs, triaged, interactive=False)
        assert counts["auto_resolved"] == 0, (severity, auto)
        assert triaged[0]["status"] == "OPEN", (severity, auto)


def test_p3_not_auto_resolvable_stays_open(tmp_path, monkeypatch):
    redirect_stores(tmp_path, monkeypatch)
    excs = [make_exc()]
    triaged = [item("P3", False)]
    counts = approval.run_gate(excs, triaged, interactive=False)
    assert counts["open"] == 1
    assert triaged[0]["status"] == "OPEN"


def test_every_decision_lands_in_audit_log_with_actor(tmp_path, monkeypatch):
    redirect_stores(tmp_path, monkeypatch)
    excs = [make_exc(), make_exc(bucket="AMOUNT_MISMATCH", ref="STLTEST02")]
    triaged = [item("P3", True, index=0), item("P1", False, index=1)]
    approval.run_gate(excs, triaged, interactive=False)
    entries = read_audit(tmp_path)
    assert len(entries) == 2
    for e in entries:
        assert e["actor"], "every audit entry must carry actor attribution"
        assert e["ts"]
    decisions = {e["decision"] for e in entries}
    assert decisions == {"auto_resolve", "left_open"}


def test_auto_resolution_recorded_in_history_as_machine_actor(tmp_path, monkeypatch):
    redirect_stores(tmp_path, monkeypatch)
    excs = [make_exc(bucket="REFERENCE_FORMAT_DRIFT")]
    approval.run_gate(excs, [item("P3", True)], interactive=False)
    entries = history.recent(10)
    assert len(entries) == 1
    assert entries[0]["resolved_by"] == "sentinel-auto"

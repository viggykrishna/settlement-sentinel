"""Resolution history: the learning loop and its poisoning guard."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import history  # noqa: E402


def record(ref, resolved_by, bucket="AMOUNT_MISMATCH"):
    history.record_resolution(
        bucket=bucket, settlement_ref=ref, amount=100.0, severity="P3",
        root_cause="fee netted", action_taken="map fee",
        resolved_by=resolved_by)


def test_record_and_recent(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "HISTORY_PATH", tmp_path / "h.json")
    record("STLA", "alice")
    record("STLB", "bob")
    entries = history.recent(10)
    assert [e["settlement_ref"] for e in entries] == ["STLA", "STLB"]


def test_human_only_excludes_machine_resolutions(tmp_path, monkeypatch):
    """
    Precedent-poisoning guard: agent-made ("sentinel-auto") resolutions
    must never become few-shot precedent for the agent's own future triage.
    """
    monkeypatch.setattr(history, "HISTORY_PATH", tmp_path / "h.json")
    record("STLHUMAN", "alice")
    record("STLMACHINE", "sentinel-auto")
    record("STLHUMAN2", "bob")
    human = history.recent(10, human_only=True)
    assert [e["settlement_ref"] for e in human] == ["STLHUMAN", "STLHUMAN2"]
    assert all(e["resolved_by"] != "sentinel-auto" for e in human)


def test_lookup_filters_by_bucket_and_ref(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "HISTORY_PATH", tmp_path / "h.json")
    record("STLX1", "alice", bucket="AMOUNT_MISMATCH")
    record("STLY1", "alice", bucket="DUPLICATE_SCHEME_ENTRY")
    assert len(history.lookup(bucket="AMOUNT_MISMATCH")) == 1
    assert len(history.lookup(ref_contains="stly")) == 1
    assert history.lookup(bucket="MISSING_IN_LEDGER") == []

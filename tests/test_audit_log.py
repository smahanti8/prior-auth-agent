"""Hash-chain audit log tests.

The chain makes decisions.jsonl and pending.jsonl tamper-evident: each entry
hashes (previous entry_hash + its own content), so editing, deleting, or
reordering any line breaks verification of everything after it.
"""

import importlib
import json

import pytest

from prior_auth_agent.audit_log import (
    ChainBrokenError,
    GENESIS,
    append_chained,
    verify_chain,
)

# nodes/__init__.py re-exports the confidence_gate *function* under the same
# name as its module, so import the module explicitly for monkeypatching.
confidence_gate = importlib.import_module("prior_auth_agent.nodes.confidence_gate")


def _state(case_id="pa-001", decision="approve", confidence=0.95):
    return {
        "case_id": case_id,
        "cpt_code": "29881",
        "eligibility_notes": "active coverage",
        "criteria": [],
        "evidence": [],
        "determination": {
            "decision": decision,
            "confidence": confidence,
            "rationale": "meets criteria",
            "gaps": [],
        },
    }


# ------------------------------------------------------------ chain mechanics


def test_chain_links_and_verifies(tmp_path):
    log = tmp_path / "log.jsonl"
    e1 = append_chained(log, {"case_id": "pa-001", "x": 1})
    e2 = append_chained(log, {"case_id": "pa-002", "x": 2})

    assert e1["prev_hash"] == GENESIS
    assert e2["prev_hash"] == e1["entry_hash"]
    assert [e["case_id"] for e in verify_chain(log)] == ["pa-001", "pa-002"]


def test_verify_empty_or_missing_log(tmp_path):
    assert verify_chain(tmp_path / "nope.jsonl") == []


def test_tampered_content_breaks_chain(tmp_path):
    log = tmp_path / "log.jsonl"
    for i in range(3):
        append_chained(log, {"case_id": f"pa-{i}", "decision": "deny"})

    # attacker flips a denial to an approval in the middle entry
    lines = log.read_text().splitlines()
    doctored = json.loads(lines[1])
    doctored["decision"] = "approve"
    lines[1] = json.dumps(doctored)
    log.write_text("\n".join(lines) + "\n")

    with pytest.raises(ChainBrokenError, match="tampered"):
        verify_chain(log)


def test_deleted_entry_breaks_chain(tmp_path):
    log = tmp_path / "log.jsonl"
    for i in range(3):
        append_chained(log, {"case_id": f"pa-{i}"})

    lines = log.read_text().splitlines()
    log.write_text("\n".join([lines[0], lines[2]]) + "\n")

    with pytest.raises(ChainBrokenError, match="prev_hash"):
        verify_chain(log)


def test_reordered_entries_break_chain(tmp_path):
    log = tmp_path / "log.jsonl"
    for i in range(3):
        append_chained(log, {"case_id": f"pa-{i}"})

    lines = log.read_text().splitlines()
    log.write_text("\n".join([lines[0], lines[2], lines[1]]) + "\n")

    with pytest.raises(ChainBrokenError):
        verify_chain(log)


# ------------------------------------------------------------ gate integration


def test_auto_decision_writes_chained_entry(tmp_path, monkeypatch):
    log = tmp_path / "decisions.jsonl"
    monkeypatch.setattr(confidence_gate, "DECISIONS_LOG", log)

    confidence_gate.auto_decision(_state("pa-001"))
    confidence_gate.auto_decision(_state("pa-002"))

    entries = verify_chain(log)
    assert len(entries) == 2
    assert all(e["decided_by"] == "auto" for e in entries)
    assert entries[1]["prev_hash"] == entries[0]["entry_hash"]


def test_hitl_enqueue_writes_chained_entry(tmp_path, monkeypatch):
    queue = tmp_path / "pending.jsonl"
    monkeypatch.setattr(confidence_gate, "PENDING_QUEUE", queue)

    confidence_gate.hitl_enqueue(
        _state("pa-003", decision="insufficient_evidence", confidence=0.4)
    )

    entries = verify_chain(queue)
    assert entries[0]["status"] == "pending_review"
    assert entries[0]["prev_hash"] == GENESIS

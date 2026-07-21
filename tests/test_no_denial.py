"""The AI never frames a denial (see DECISIONS.md D9).

The drafter may only 'approve' or return 'insufficient_evidence'; a human
makes any denial. These tests pin that invariant at three layers — the
schema the model is constrained to, the state type, and the routing/finalize
logic — so no reachable path lets the AI recommend or finalize a denial.
"""

import importlib
import typing

from prior_auth_agent.nodes import determination
from prior_auth_agent.state import Determination

confidence_gate = importlib.import_module("prior_auth_agent.nodes.confidence_gate")

ALLOWED_DECISIONS = {"approve", "insufficient_evidence"}
DENIAL_WORDS = {"deny", "denial", "denied", "pend", "reject", "rejected"}


def _state(decision, confidence=0.99):
    return {
        "case_id": "pa-x",
        "cpt_code": "29881",
        "eligibility_notes": "active coverage",
        "criteria": [],
        "evidence": [],
        "determination": {
            "decision": decision,
            "confidence": confidence,
            "rationale": "n/a",
            "gaps": [],
        },
    }


# --- layer 1: the model is schema-constrained to a denial-free vocabulary ---


def test_drafter_schema_enum_has_no_denial():
    enum = determination.SCHEMA["properties"]["decision"]["enum"]
    assert set(enum) == ALLOWED_DECISIONS
    assert not (set(enum) & DENIAL_WORDS)


# --- layer 2: the state contract cannot even represent an AI denial ---


def test_determination_type_excludes_denial():
    decisions = set(typing.get_args(Determination.__annotations__["decision"]))
    assert decisions == ALLOWED_DECISIONS
    assert not (decisions & DENIAL_WORDS)


# --- layer 3: routing/finalize never produces a denial from the AI ----------


def test_gate_auto_finalizes_only_approvals():
    # An approval with high confidence and no gaps is the sole auto path.
    assert confidence_gate.confidence_gate(_state("approve"))["route"] == "auto"
    # Anything the drafter can emit that is not an approval goes to a human.
    assert (
        confidence_gate.confidence_gate(_state("insufficient_evidence"))["route"]
        == "hitl"
    )


def test_no_schema_decision_yields_an_ai_denial(tmp_path, monkeypatch):
    """Exhaustively drive every decision the schema permits through the gate
    and the finalizers; no AI-authored path yields a denial."""
    monkeypatch.setattr(confidence_gate, "DECISIONS_LOG", tmp_path / "decisions.jsonl")
    monkeypatch.setattr(confidence_gate, "PENDING_QUEUE", tmp_path / "pending.jsonl")

    for decision in determination.SCHEMA["properties"]["decision"]["enum"]:
        state = _state(decision)
        route = confidence_gate.confidence_gate(state)["route"]
        if route == "auto":
            final = confidence_gate.auto_decision(state)["final_decision"]
            # The machine only ever finalizes an approval.
            assert final == "approve"
        else:
            final = confidence_gate.hitl_enqueue(state)["final_decision"]
            assert final == "pending_human_review"
        assert final not in DENIAL_WORDS

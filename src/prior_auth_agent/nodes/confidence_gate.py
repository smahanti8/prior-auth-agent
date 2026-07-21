"""Confidence Gate: route to auto decision or the HITL review queue."""

from datetime import datetime, timezone

from ..audit_log import append_chained
from ..config import CONFIDENCE_THRESHOLD, PENDING_QUEUE, DECISIONS_LOG
from ..state import PriorAuthState


def confidence_gate(state: PriorAuthState) -> PriorAuthState:
    det = state["determination"]
    required_insufficient = any(
        e["status"] == "insufficient"
        and any(c["id"] == e["criterion_id"] and c["required"] for c in state["criteria"])
        for e in state["evidence"]
    )
    needs_review = (
        det["decision"] != "approve"  # only approvals can be auto-finalized
        or det["confidence"] < CONFIDENCE_THRESHOLD
        or required_insufficient
    )
    return {"route": "hitl" if needs_review else "auto"}


def _case_record(state: PriorAuthState) -> dict:
    return {
        "case_id": state["case_id"],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "cpt_code": state["cpt_code"],
        "eligibility_notes": state.get("eligibility_notes", ""),
        "criteria": state.get("criteria", []),
        "evidence": state.get("evidence", []),
        "determination": state["determination"],
    }


def auto_decision(state: PriorAuthState) -> PriorAuthState:
    record = _case_record(state) | {"decided_by": "auto"}
    append_chained(DECISIONS_LOG, record)
    return {"final_decision": state["determination"]["decision"]}


def hitl_enqueue(state: PriorAuthState) -> PriorAuthState:
    record = _case_record(state) | {"status": "pending_review"}
    append_chained(PENDING_QUEUE, record)
    return {"final_decision": "pending_human_review"}

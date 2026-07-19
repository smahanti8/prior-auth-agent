"""Confidence Gate: route to auto decision or the HITL review queue."""

import json
from datetime import datetime, timezone

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
        det["confidence"] < CONFIDENCE_THRESHOLD
        or det["decision"] in ("deny", "pend")  # denials always get human eyes
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
    DECISIONS_LOG.parent.mkdir(parents=True, exist_ok=True)
    with DECISIONS_LOG.open("a") as f:
        f.write(json.dumps(record) + "\n")
    return {"final_decision": state["determination"]["decision"]}


def hitl_enqueue(state: PriorAuthState) -> PriorAuthState:
    record = _case_record(state) | {"status": "pending_review"}
    PENDING_QUEUE.parent.mkdir(parents=True, exist_ok=True)
    with PENDING_QUEUE.open("a") as f:
        f.write(json.dumps(record) + "\n")
    return {"final_decision": "pending_human_review"}

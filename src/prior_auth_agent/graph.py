"""LangGraph wiring for the prior-auth pipeline.

Intake -> Eligibility -> Policy RAG -> Criteria Mapper -> Evidence Extractor
-> Determination -> Confidence Gate --(low)--> HITL queue
                                    --(high)-> Auto decision
"""


from langgraph.graph import END, StateGraph

from .audit_log import append_chained
from .config import AUDIT_LOG_PATH
from .nodes import (
    auto_decision,
    citation_gate,
    citation_reject,
    confidence_gate,
    criteria_mapper,
    determination_drafter,
    eligibility,
    evidence_extractor,
    hitl_enqueue,
    intake,
    policy_rag,
)
from .state import PriorAuthState
from . import telemetry
from .telemetry import _timed


# ── Routing predicates ─────────────────────────────────────────────────────────


def _after_intake(state: PriorAuthState) -> str:
    return "eligibility" if state["valid"] else "reject"


def _after_eligibility(state: PriorAuthState) -> str:
    return "policy_rag" if state["eligible"] else "reject"


def _after_citation_gate(state: PriorAuthState) -> str:
    return "determination" if state["citation_ok"] else "citation_reject"


def _after_gate(state: PriorAuthState) -> str:
    return "auto_decision" if state["route"] == "auto" else "hitl_enqueue"


def _reject(state: PriorAuthState) -> PriorAuthState:
    reasons = state.get("validation_errors") or [state.get("eligibility_notes", "ineligible")]
    return {"final_decision": f"rejected_at_intake: {'; '.join(reasons)}"}


# ── Graph construction ────────────────────────────────────────────────────────


def build_graph():
    g = StateGraph(PriorAuthState)

    g.add_node("intake",             _timed("intake",             intake))
    g.add_node("eligibility",        _timed("eligibility",        eligibility))
    g.add_node("policy_rag",         _timed("policy_rag",         policy_rag))
    g.add_node("criteria_mapper",    _timed("criteria_mapper",    criteria_mapper))
    g.add_node("evidence_extractor", _timed("evidence_extractor", evidence_extractor))
    g.add_node("citation_gate",      _timed("citation_gate",      citation_gate))
    g.add_node("citation_reject",    _timed("citation_reject",    citation_reject))
    g.add_node("determination",      _timed("determination",      determination_drafter))
    g.add_node("confidence_gate",    _timed("confidence_gate",    confidence_gate))
    g.add_node("auto_decision",      _timed("auto_decision",      auto_decision))
    g.add_node("hitl_enqueue",       _timed("hitl_enqueue",       hitl_enqueue))
    g.add_node("reject",             _timed("reject",             _reject))

    g.set_entry_point("intake")
    g.add_conditional_edges("intake", _after_intake, ["eligibility", "reject"])
    g.add_conditional_edges("eligibility", _after_eligibility, ["policy_rag", "reject"])
    g.add_edge("policy_rag", "criteria_mapper")
    g.add_edge("criteria_mapper", "evidence_extractor")
    g.add_edge("evidence_extractor", "citation_gate")
    g.add_conditional_edges(
        "citation_gate", _after_citation_gate, ["determination", "citation_reject"]
    )
    g.add_edge("determination", "confidence_gate")
    g.add_conditional_edges("confidence_gate", _after_gate, ["auto_decision", "hitl_enqueue"])
    g.add_edge("auto_decision", END)
    g.add_edge("hitl_enqueue", END)
    g.add_edge("citation_reject", END)
    g.add_edge("reject", END)

    return g.compile()


# ── Entry point ───────────────────────────────────────────────────────────────


def run(bundle_path: str, cpt_code: str) -> PriorAuthState:
    import json

    with open(bundle_path) as f:
        bundle = json.load(f)

    telemetry.reset()
    graph = build_graph()
    state = graph.invoke({"fhir_bundle": bundle, "cpt_code": cpt_code})

    node_tel = telemetry.drain()
    cost_total = sum(t.cost_usd for t in node_tel)
    append_chained(AUDIT_LOG_PATH, {
        "case_id":        state.get("case_id"),
        "cpt_code":       cpt_code,
        "final_decision": state.get("final_decision"),
        "determination":  state.get("determination"),
        "node_telemetry": [t.to_dict() for t in node_tel],
        "cost_usd_total": cost_total,
    })

    return state


if __name__ == "__main__":
    import json
    import sys

    if len(sys.argv) != 3:
        sys.exit("usage: python -m prior_auth_agent.graph <bundle.json> <cpt_code>")

    final_state = run(sys.argv[1], sys.argv[2])
    print(f"\ncase_id:  {final_state.get('case_id')}")
    print(f"decision: {final_state.get('final_decision')}")
    if det := final_state.get("determination"):
        print(f"draft:    {det['decision']} (confidence {det['confidence']:.2f})")
        print(f"rationale:\n{det['rationale']}")
    if ev := final_state.get("evidence"):
        print("\nevidence:")
        print(json.dumps(ev, indent=2))

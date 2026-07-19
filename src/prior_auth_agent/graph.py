"""LangGraph wiring for the prior-auth pipeline.

Intake -> Eligibility -> Policy RAG -> Criteria Mapper -> Evidence Extractor
-> Determination -> Confidence Gate --(low)--> HITL queue
                                    --(high)-> Auto decision
"""

from langgraph.graph import END, StateGraph

from .nodes import (
    auto_decision,
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


def _after_intake(state: PriorAuthState) -> str:
    return "eligibility" if state["valid"] else "reject"


def _after_eligibility(state: PriorAuthState) -> str:
    return "policy_rag" if state["eligible"] else "reject"


def _after_gate(state: PriorAuthState) -> str:
    return "auto_decision" if state["route"] == "auto" else "hitl_enqueue"


def _reject(state: PriorAuthState) -> PriorAuthState:
    reasons = state.get("validation_errors") or [state.get("eligibility_notes", "ineligible")]
    return {"final_decision": f"rejected_at_intake: {'; '.join(reasons)}"}


def build_graph():
    g = StateGraph(PriorAuthState)

    g.add_node("intake", intake)
    g.add_node("eligibility", eligibility)
    g.add_node("policy_rag", policy_rag)
    g.add_node("criteria_mapper", criteria_mapper)
    g.add_node("evidence_extractor", evidence_extractor)
    g.add_node("determination", determination_drafter)
    g.add_node("confidence_gate", confidence_gate)
    g.add_node("auto_decision", auto_decision)
    g.add_node("hitl_enqueue", hitl_enqueue)
    g.add_node("reject", _reject)

    g.set_entry_point("intake")
    g.add_conditional_edges("intake", _after_intake, ["eligibility", "reject"])
    g.add_conditional_edges("eligibility", _after_eligibility, ["policy_rag", "reject"])
    g.add_edge("policy_rag", "criteria_mapper")
    g.add_edge("criteria_mapper", "evidence_extractor")
    g.add_edge("evidence_extractor", "determination")
    g.add_edge("determination", "confidence_gate")
    g.add_conditional_edges("confidence_gate", _after_gate, ["auto_decision", "hitl_enqueue"])
    g.add_edge("auto_decision", END)
    g.add_edge("hitl_enqueue", END)
    g.add_edge("reject", END)

    return g.compile()


def run(bundle_path: str, cpt_code: str) -> PriorAuthState:
    import json

    with open(bundle_path) as f:
        bundle = json.load(f)

    graph = build_graph()
    return graph.invoke({"fhir_bundle": bundle, "cpt_code": cpt_code})


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

"""Citation Gate: hard-reject any determination built on an uncited met claim.

Runs after evidence extraction and before the drafter. It is a HARD gate,
distinct from the confidence gate's soft HITL routing: a met criterion missing
its policy_quote or chart citation is thrown out here and never reaches the
determination. Clean evidence passes straight through to the drafter unchanged.
"""

from ..state import PriorAuthState
from ..validation import check_claims


def citation_gate(state: PriorAuthState) -> PriorAuthState:
    violations = check_claims(state.get("evidence", []))
    return {"citation_ok": not violations, "citation_errors": violations}


def citation_reject(state: PriorAuthState) -> PriorAuthState:
    errors = state.get("citation_errors") or ["uncited met claim"]
    return {"final_decision": f"rejected_uncited_claim: {'; '.join(errors)}"}

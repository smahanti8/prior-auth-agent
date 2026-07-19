"""Determination Drafter: draft approve/deny/pend with rationale and confidence."""

import json

from ..llm import structured_call
from ..state import PriorAuthState

SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["approve", "deny", "pend"]},
        "rationale": {"type": "string"},
        "confidence": {"type": "number"},
    },
    "required": ["decision", "rationale", "confidence"],
    "additionalProperties": False,
}

SYSTEM = (
    "You are drafting a prior-authorization determination. Rules:\n"
    "- approve only if every required criterion is 'met'\n"
    "- deny only if a required criterion is clearly 'not_met' with cited evidence\n"
    "- pend if any required criterion is 'insufficient'\n"
    "The rationale must reference criteria by id and cite the same FHIR references "
    "as the evidence. confidence is your 0-1 calibrated estimate that a physician "
    "reviewer would reach the same decision; lower it when evidence is sparse, "
    "conflicting, or the policy text was ambiguous. Denials are always reviewed "
    "by a human, so do not inflate confidence to avoid review."
)


def determination_drafter(state: PriorAuthState) -> PriorAuthState:
    result = structured_call(
        system=SYSTEM,
        user_content=(
            f"CPT: {state['cpt_code']}\n"
            f"Eligibility: {state['eligibility_notes']}\n\n"
            f"Criteria:\n{json.dumps(state['criteria'], indent=2)}\n\n"
            f"Evidence:\n{json.dumps(state['evidence'], indent=2)}"
        ),
        schema=SCHEMA,
    )
    result["confidence"] = max(0.0, min(1.0, float(result["confidence"])))
    return {"determination": result}

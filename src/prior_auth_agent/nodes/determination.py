"""Determination Drafter: draft an approval or name the evidence gaps.

The drafter never recommends a denial (see DECISIONS.md D9). A drafted denial
anchors the reviewer toward denying; instead the drafter either approves or
returns 'insufficient_evidence' with the specific criteria that lack support
and the documentation that would satisfy each. A human decides any denial.
"""

import json

from ..llm import structured_call
from ..state import PriorAuthState

SCHEMA = {
    "type": "object",
    "properties": {
        # No 'deny' / 'pend': the drafter cannot frame a denial. Structured
        # output constrains the model to this enum, so no reachable path can
        # emit a denial recommendation (asserted in tests/test_no_denial.py).
        "decision": {"type": "string", "enum": ["approve", "insufficient_evidence"]},
        "rationale": {"type": "string"},
        "confidence": {"type": "number"},
        "gaps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "criterion_id": {"type": "string"},
                    "needed_evidence": {"type": "string"},
                },
                "required": ["criterion_id", "needed_evidence"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["decision", "rationale", "confidence", "gaps"],
    "additionalProperties": False,
}

SYSTEM = (
    "You are drafting a prior-authorization determination. You draft ONE of two "
    "outcomes and never recommend a denial — a human makes any adverse "
    "decision.\n"
    "- 'approve' only if every required criterion is 'met'. gaps must be empty.\n"
    "- 'insufficient_evidence' in every other case — whether a required "
    "criterion is 'not_met' or 'insufficient'. Do NOT characterize the case as "
    "a denial, do not argue for denial, and do not predict one. For each "
    "required criterion that is not 'met', add a gap naming the criterion id "
    "and the specific documentation that would let it be met (e.g. 'a completed "
    "6-week conservative-therapy course with dates', not 'more information').\n"
    "The rationale states, neutrally, which criteria are met and which are "
    "unresolved, referencing criteria by id and citing the same FHIR references "
    "as the evidence. Frame unresolved criteria as missing support to obtain, "
    "not as grounds to deny. confidence is your 0-1 calibrated estimate that a "
    "physician reviewer would agree with this approve / insufficient-evidence "
    "call; lower it when evidence is sparse, conflicting, or the policy text was "
    "ambiguous."
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
    result.setdefault("gaps", [])
    return {"determination": result}

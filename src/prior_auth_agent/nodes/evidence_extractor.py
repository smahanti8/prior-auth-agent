"""Evidence Extractor: per-criterion clinical evidence with FHIR citations."""

import json

from ..llm import structured_call
from ..state import PriorAuthState

SCHEMA = {
    "type": "object",
    "properties": {
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "criterion_id": {"type": "string"},
                    "status": {
                        "type": "string",
                        "enum": ["met", "not_met", "insufficient"],
                    },
                    "summary": {"type": "string"},
                    "citations": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "policy_quote": {"type": "string"},
                },
                "required": [
                    "criterion_id",
                    "status",
                    "summary",
                    "citations",
                    "policy_quote",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["evidence"],
    "additionalProperties": False,
}

SYSTEM = (
    "You are a clinical-documentation reviewer. For each policy criterion, search "
    "the FHIR bundle for supporting or contradicting evidence. Cite every claim "
    "with FHIR references in the form ResourceType/id (e.g. 'Observation/bmi-1'). "
    "Only cite resources that actually appear in the bundle. If the bundle has no "
    "relevant documentation for a criterion, set status='insufficient' with empty "
    "citations — never infer or fabricate clinical facts.\n"
    "For every criterion, also set policy_quote to the exact sentence(s) from the "
    "criterion's policy text that establish the requirement. Any criterion you mark "
    "'met' MUST carry a non-empty policy_quote AND at least one FHIR citation — a "
    "met claim without both is rejected downstream, so do not mark 'met' unless you "
    "can supply both the policy quote and the chart citation."
)


def evidence_extractor(state: PriorAuthState) -> PriorAuthState:
    result = structured_call(
        system=SYSTEM,
        user_content=(
            f"Criteria:\n{json.dumps(state['criteria'], indent=2)}\n\n"
            f"FHIR bundle:\n{json.dumps(state['fhir_bundle'], indent=2)}"
        ),
        schema=SCHEMA,
        max_tokens=32000,
    )
    return {"evidence": result["evidence"]}

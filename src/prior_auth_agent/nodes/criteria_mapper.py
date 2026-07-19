"""Criteria Mapper: turn retrieved policy text into discrete, checkable criteria."""

from ..llm import structured_call
from ..state import PriorAuthState

SCHEMA = {
    "type": "object",
    "properties": {
        "criteria": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "text": {"type": "string"},
                    "required": {"type": "boolean"},
                },
                "required": ["id", "text", "required"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["criteria"],
    "additionalProperties": False,
}

SYSTEM = (
    "You are a utilization-management analyst. Given payer policy excerpts for a "
    "procedure, extract the discrete medical-necessity criteria as an enumerated "
    "list. Each criterion must be a single, independently checkable condition. "
    "Mark required=true for criteria the policy states must all be met, "
    "required=false for alternatives/optional pathways. Use ids c1, c2, ..."
)


def criteria_mapper(state: PriorAuthState) -> PriorAuthState:
    policy_text = "\n\n---\n\n".join(
        f"[{c['source']}]\n{c['text']}" for c in state["policy_chunks"]
    )
    result = structured_call(
        system=SYSTEM,
        user_content=(
            f"CPT code: {state['cpt_code']}\n\nPolicy excerpts:\n\n{policy_text}"
        ),
        schema=SCHEMA,
    )
    return {"criteria": result["criteria"]}

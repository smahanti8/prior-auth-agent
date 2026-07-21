"""Citation enforcement: no citation -> no claim.

The rule the pipeline exists to enforce: a criterion the model marks as met
must be backed by BOTH a policy-side quote (the requirement) and a chart-side
citation (the evidence). A met claim missing either is rejected outright — it
never reaches the determination. This is a hard gate, not the soft HITL
routing: a violation is thrown out, not sent to a human.

Enforcement lives in a pydantic validator that raises, so it can be unit
tested without any LLM call. `check_claims` adapts that raise into a list of
human-readable violation strings for the graph node to route on.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator


class EvidenceClaim(BaseModel):
    """One criterion's evidence, with the citation rule enforced on met claims."""

    model_config = ConfigDict(extra="ignore")

    criterion_id: str
    status: Literal["met", "not_met", "insufficient"]
    summary: str = ""
    citations: list[str] = []  # chart-side FHIR references
    policy_quote: str = ""  # policy-side quote

    @model_validator(mode="after")
    def _citation_or_no_claim(self) -> "EvidenceClaim":
        if self.status == "met":
            if not self.policy_quote.strip():
                raise ValueError(
                    f"{self.criterion_id}: marked met without a policy_quote"
                )
            if not any(c.strip() for c in self.citations):
                raise ValueError(
                    f"{self.criterion_id}: marked met without a chart citation"
                )
        return self


def check_claims(evidence: list[dict]) -> list[str]:
    """Validate every evidence row; return a list of violation messages.

    Empty list means the evidence is clean. Each row is validated
    independently so a single bad claim reports its own reason rather than
    aborting the whole check on the first failure.
    """
    violations: list[str] = []
    for row in evidence:
        try:
            EvidenceClaim.model_validate(row)
        except ValueError as e:
            # pydantic ValidationError is a ValueError subclass; take the
            # validator's own message where present, else the full string.
            msg = str(e)
            if hasattr(e, "errors"):
                parts = [d.get("msg", "").removeprefix("Value error, ") for d in e.errors()]
                msg = "; ".join(p for p in parts if p) or msg
            violations.append(msg)
    return violations

"""Citation enforcement: no citation -> no claim.

Two complementary layers enforce that a criterion the model marks `met` is
actually backed by evidence, not merely shaped like it:

  presence (EvidenceClaim / check_claims) — a met claim must carry a
    non-empty policy_quote AND at least one non-empty citation. Enforced by a
    pydantic validator that raises, so it is unit-testable without any LLM
    call. `check_claims` adapts that raise into violation strings for the
    citation_gate graph node to route on.

  existence (resolve_citations) — a met claim's citations must actually
    resolve against the submitted FHIR bundle (ResourceType/id must exist in
    it). Unresolvable or malformed citations are stripped; a claim with none
    surviving is downgraded to `insufficient` rather than rejected outright,
    since the criterion itself may still be true even if a specific citation
    was wrong. Non-met claims are untouched.

Presence is checked in the graph after existence resolution, so by
construction a `met` claim that reaches citation_gate already has at least
one real citation — the gate's own presence check remains a backstop for the
case where a claim arrives with zero citations from the start.
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


# ── FHIR reference existence resolution ───────────────────────────────────────


def _build_ref_index(bundle: dict) -> set[tuple[str, str]]:
    """Return a (ResourceType, id) set for every resource in a FHIR bundle."""
    index: set[tuple[str, str]] = set()
    for entry in bundle.get("entry", []):
        resource = entry.get("resource", {})
        rtype = resource.get("resourceType", "")
        rid = resource.get("id", "")
        if rtype and rid:
            index.add((rtype, rid))
    return index


def _parse_citation(raw: str) -> tuple[str, str] | None:
    """Parse 'ResourceType/id' → (rtype, rid), or None if malformed."""
    parts = raw.strip().split("/")
    if len(parts) != 2:
        return None
    rtype, rid = parts[0].strip(), parts[1].strip()
    if not rtype or not rid:
        return None
    return rtype, rid


def resolve_citations(evidence: list[dict], bundle: dict) -> list[dict]:
    """Verify citations on met claims resolve against the submitted bundle.

    Resolution is per-citation with a criterion-level rule:
    - Each citation is checked independently against the bundle's ref index.
    - Unresolvable or malformed citations are stripped and logged in summary.
    - If at least one citation survives, the criterion stays ``met`` on the
      survivors only.
    - If no citations survive, the criterion is downgraded to ``insufficient``.

    Non-met claims are returned unchanged. The presence check in
    ``EvidenceClaim`` is a separate, complementary layer and is unaffected.
    """
    ref_index = _build_ref_index(bundle)
    result: list[dict] = []

    for claim in evidence:
        if claim.get("status") != "met":
            result.append(claim)
            continue

        surviving: list[str] = []
        strip_reasons: list[str] = []

        for raw in claim.get("citations", []):
            parsed = _parse_citation(raw)
            if parsed is None:
                strip_reasons.append(
                    f"malformed citation '{raw}' — expected ResourceType/id"
                )
            elif parsed not in ref_index:
                strip_reasons.append(f"'{raw}' not found in bundle")
            else:
                surviving.append(raw)

        if not strip_reasons:
            result.append(claim)
            continue

        updated = dict(claim)
        updated["citations"] = surviving
        prefix = " ".join(f"[CITATION STRIPPED: {r}]" for r in strip_reasons)
        updated["summary"] = f"{prefix} {claim.get('summary', '')}".strip()

        if not surviving:
            updated["status"] = "insufficient"

        result.append(updated)

    return result

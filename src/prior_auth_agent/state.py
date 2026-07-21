"""LangGraph state schema for the prior-auth pipeline."""

from typing import Any, Literal, TypedDict


class Criterion(TypedDict):
    id: str
    text: str
    required: bool


class Evidence(TypedDict):
    criterion_id: str
    status: Literal["met", "not_met", "insufficient"]
    summary: str
    citations: list[str]  # chart-side FHIR references, e.g. "Observation/bmi-1"
    policy_quote: str  # policy-side quote establishing the requirement


class EvidenceGap(TypedDict):
    criterion_id: str
    needed_evidence: str  # what documentation would let this criterion be met


class Determination(TypedDict):
    # The drafter never recommends a denial (see DECISIONS.md D9). It emits
    # 'approve', or 'insufficient_evidence' with the gaps that block approval.
    decision: Literal["approve", "insufficient_evidence"]
    rationale: str
    confidence: float  # 0.0 - 1.0
    gaps: list[EvidenceGap]  # empty when approved


class PriorAuthState(TypedDict, total=False):
    # --- inputs ---
    fhir_bundle: dict[str, Any]
    cpt_code: str

    # --- intake / validation ---
    case_id: str
    valid: bool
    validation_errors: list[str]

    # --- eligibility ---
    eligible: bool
    eligibility_notes: str

    # --- policy RAG ---
    policy_chunks: list[dict[str, Any]]  # {text, source, distance}

    # --- criteria mapping ---
    criteria: list[Criterion]

    # --- evidence extraction ---
    evidence: list[Evidence]

    # --- citation gate (hard reject on uncited met claims) ---
    citation_ok: bool
    citation_errors: list[str]

    # --- determination ---
    determination: Determination

    # --- routing ---
    route: Literal["auto", "hitl"]
    final_decision: str

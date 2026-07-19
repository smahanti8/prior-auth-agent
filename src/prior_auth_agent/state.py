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
    citations: list[str]  # FHIR resource references, e.g. "Observation/bmi-1"


class Determination(TypedDict):
    decision: Literal["approve", "deny", "pend"]
    rationale: str
    confidence: float  # 0.0 - 1.0


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

    # --- determination ---
    determination: Determination

    # --- routing ---
    route: Literal["auto", "hitl"]
    final_decision: str

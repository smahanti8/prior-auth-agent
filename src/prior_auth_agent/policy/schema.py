"""Pydantic models for versioned machine-readable criterion schema.

A CriterionSpec encodes one policy criterion as:
  - stable criterion_id + semver version
  - verbatim criterion_text from the source document
  - policy_ref that anchors the text_fragment to an exact location in the
    source file (conformance tests assert this fragment exists verbatim)
  - a PredicateNode tree composed from a small declarative operator vocabulary

The operator vocabulary is intentionally bounded: when a criterion cannot be
faithfully encoded with existing operators, add a new operator rather than
embedding opaque logic. Bounded vocabulary keeps the representation auditable
— a reviewer can compare each node to the source text without executing code.
"""
from __future__ import annotations

from typing import Optional
from pydantic import BaseModel, model_validator


class PolicyRef(BaseModel):
    file: str            # filename relative to data/policies/
    section: str         # heading within the source document
    text_fragment: str   # verbatim substring — asserted by conformance tests


# ── Leaf operator parameter models ────────────────────────────────────────────


class ResourceExistsParams(BaseModel):
    resource_type: str


class FindingPresentParams(BaseModel):
    """Check display text across all string fields of a resource.

    contradicting_terms are checked first. A match → not_met (affirmative
    contradiction). Then affirmative_terms: a match → met. Neither matched
    on any resource of the type → insufficient.
    """
    resource_type: str
    affirmative_terms: list[str]
    contradicting_terms: list[str] = []
    case_insensitive: bool = True


class FieldDocumentedParams(BaseModel):
    """Check that a dot-path field on at least one resource is present and non-empty."""
    resource_type: str
    field_path: str   # e.g. "outcome", "valueQuantity", "item"


class MinimumDurationMonthsParams(BaseModel):
    """Check that the span between two date fields is ≥ min_months calendar months.

    Uses calendar months (Oct 1 + 3 months = Jan 1), not a fixed 90-day count.
    Missing end date → insufficient; span < min_months → not_met.
    """
    resource_type: str
    start_path: str    # dot-path to start date field
    end_path: str      # dot-path to end date field
    min_months: int


# ── Predicate node (composite + leaves) ───────────────────────────────────────


class PredicateNode(BaseModel):
    """One node in a predicate tree. Exactly one field must be set."""

    all_of: Optional[list[PredicateNode]] = None
    any_of: Optional[list[PredicateNode]] = None
    resource_exists: Optional[ResourceExistsParams] = None
    finding_present: Optional[FindingPresentParams] = None
    field_documented: Optional[FieldDocumentedParams] = None
    minimum_duration_months: Optional[MinimumDurationMonthsParams] = None

    @model_validator(mode="after")
    def exactly_one_operator(self) -> PredicateNode:
        operators = (
            "all_of", "any_of", "resource_exists", "finding_present",
            "field_documented", "minimum_duration_months",
        )
        set_fields = [k for k in operators if getattr(self, k) is not None]
        if len(set_fields) != 1:
            raise ValueError(
                f"PredicateNode: exactly one operator must be set, got {set_fields}"
            )
        return self


PredicateNode.model_rebuild()


# ── Top-level criterion spec ───────────────────────────────────────────────────


class CriterionSpec(BaseModel):
    criterion_id: str          # e.g. "SURG-041-C1" — stable across versions
    version: str               # semver, e.g. "1.0"
    policy_effective_date: str # ISO date, matches the policy document header
    criterion_text: str        # verbatim text from the source policy
    policy_ref: PolicyRef
    required: bool
    predicate: PredicateNode
    notes: str = ""            # documented limitations of this encoding

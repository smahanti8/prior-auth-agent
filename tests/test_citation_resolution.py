"""Tests for resolve_citations() — FHIR reference existence verification.

Follows the style of test_citation_gate.py. No LLM calls; all inputs are
constructed in-process.
"""
from prior_auth_agent.validation import resolve_citations


# ── fixtures ──────────────────────────────────────────────────────────────────

BUNDLE = {
    "resourceType": "Bundle",
    "entry": [
        {"resource": {"resourceType": "Observation", "id": "bmi-001"}},
        {"resource": {"resourceType": "Condition", "id": "oa-knee"}},
    ],
}


def _met(citations: list[str], criterion_id: str = "c1") -> dict:
    return {
        "criterion_id": criterion_id,
        "status": "met",
        "summary": "Evidence summary.",
        "citations": citations,
        "policy_quote": "The policy requirement.",
    }


def _not_met(criterion_id: str = "c2") -> dict:
    return {
        "criterion_id": criterion_id,
        "status": "not_met",
        "summary": "Not documented.",
        "citations": [],
        "policy_quote": "",
    }


# ── (a) resolving citation passes unchanged ────────────────────────────────────

def test_existing_citation_passes_unchanged():
    """A met claim whose citation resolves is returned as-is."""
    evidence = [_met(["Observation/bmi-001"])]
    result = resolve_citations(evidence, BUNDLE)
    assert result[0]["status"] == "met"
    assert result[0]["citations"] == ["Observation/bmi-001"]
    assert "CITATION STRIPPED" not in result[0]["summary"]


# ── (b) citation absent from bundle → insufficient ────────────────────────────

def test_absent_citation_downgrades_to_insufficient():
    """Met claim citing a resource not in the bundle is downgraded."""
    evidence = [_met(["Observation/does-not-exist"])]
    result = resolve_citations(evidence, BUNDLE)
    assert result[0]["status"] == "insufficient"
    assert "CITATION STRIPPED" in result[0]["summary"]
    assert "Observation/does-not-exist" in result[0]["summary"]
    assert result[0]["citations"] == []


# ── (c) malformed citation → downgraded, not raised ──────────────────────────

def test_malformed_citation_downgrades_not_crashes():
    """A met claim with a malformed citation string is downgraded, not raised."""
    evidence = [_met(["not-a-valid-reference"])]
    result = resolve_citations(evidence, BUNDLE)
    assert result[0]["status"] == "insufficient"
    assert "malformed" in result[0]["summary"].lower()
    assert "not-a-valid-reference" in result[0]["summary"]


# ── (d) downgraded required criterion → HITL via confidence_gate ──────────────

def test_downgraded_required_criterion_routes_to_hitl():
    """Required criterion downgraded to insufficient causes HITL routing."""
    from prior_auth_agent.nodes.confidence_gate import confidence_gate

    state = {
        "case_id": "test-d",
        "cpt_code": "43644",
        "eligibility_notes": "",
        "evidence": [
            {
                "criterion_id": "c1",
                "status": "insufficient",
                "summary": (
                    "[CITATION STRIPPED: 'Observation/fake' not found in bundle]"
                    " Evidence summary."
                ),
                "citations": [],
                "policy_quote": "The policy requirement.",
            }
        ],
        "criteria": [{"id": "c1", "text": "BMI criterion", "required": True}],
        "determination": {
            "decision": "approve",
            "confidence": 0.95,
            "rationale": "All criteria met.",
            "gaps": [],
        },
        "citation_ok": True,
        "citation_errors": [],
    }
    result = confidence_gate(state)
    assert result["route"] == "hitl"


# ── (e) mixed citations: two resolve, one doesn't → stays met ────────────────

def test_mixed_citations_strips_unresolved_stays_met():
    """Three citations; two resolve, one doesn't — criterion stays met on survivors."""
    evidence = [
        _met(["Observation/bmi-001", "Observation/phantom", "Condition/oa-knee"])
    ]
    result = resolve_citations(evidence, BUNDLE)
    claim = result[0]
    assert claim["status"] == "met"
    assert claim["citations"] == ["Observation/bmi-001", "Condition/oa-knee"]
    assert "Observation/phantom" in claim["summary"]
    assert "CITATION STRIPPED" in claim["summary"]


# ── non-met claims are not touched ────────────────────────────────────────────

def test_non_met_claims_are_not_modified():
    """not_met and insufficient claims are returned unchanged."""
    evidence = [
        _not_met(),
        {
            "criterion_id": "c3",
            "status": "insufficient",
            "summary": "Insufficient.",
            "citations": [],
            "policy_quote": "",
        },
    ]
    result = resolve_citations(evidence, BUNDLE)
    assert result == evidence

"""Citation gate: no citation -> no claim, enforced by rejection.

A criterion marked met must carry both a policy_quote and a chart-side
citation. A met claim missing either is rejected outright — a hard gate,
distinct from the confidence gate's soft HITL routing. These tests exercise
the rule deterministically, with no API key.
"""

import importlib

import pytest
from pydantic import ValidationError

from prior_auth_agent.validation import EvidenceClaim, check_claims

citation_gate = importlib.import_module("prior_auth_agent.nodes.citation_gate")


def _met(policy_quote, citations):
    return {
        "criterion_id": "c1",
        "status": "met",
        "summary": "criterion satisfied",
        "citations": citations,
        "policy_quote": policy_quote,
    }


# ---------------------------------------------------------------- the rule

def test_met_with_both_quotes_is_valid():
    claim = EvidenceClaim.model_validate(_met("policy says X", ["Observation/bmi-1"]))
    assert claim.status == "met"


def test_met_without_policy_quote_raises():
    with pytest.raises(ValidationError, match="without a policy_quote"):
        EvidenceClaim.model_validate(_met("", ["Observation/bmi-1"]))


def test_met_without_chart_citation_raises():
    with pytest.raises(ValidationError, match="without a chart citation"):
        EvidenceClaim.model_validate(_met("policy says X", []))


def test_non_met_statuses_need_no_citation():
    # insufficient / not_met legitimately carry no citation
    EvidenceClaim.model_validate(
        {"criterion_id": "c2", "status": "insufficient", "summary": "silent",
         "citations": [], "policy_quote": ""}
    )


# ------------------------------------------------------- check_claims helper

def test_check_claims_clean_returns_no_violations():
    evidence = [
        _met("policy A", ["Procedure/pt-1"]),
        {"criterion_id": "c2", "status": "insufficient", "summary": "s",
         "citations": [], "policy_quote": ""},
    ]
    assert check_claims(evidence) == []


def test_check_claims_collects_each_violation():
    evidence = [
        {**_met("", ["Procedure/pt-1"]), "criterion_id": "c1"},   # no policy quote
        {**_met("policy B", []), "criterion_id": "c3"},           # no citation
    ]
    violations = check_claims(evidence)
    assert len(violations) == 2
    assert any("c1" in v and "policy_quote" in v for v in violations)
    assert any("c3" in v and "chart citation" in v for v in violations)


# ------------------------------------------------------------ the graph node

def test_gate_passes_clean_evidence():
    state = {"evidence": [_met("policy A", ["Procedure/pt-1"])]}
    out = citation_gate.citation_gate(state)
    assert out == {"citation_ok": True, "citation_errors": []}


def test_gate_rejects_and_reports():
    state = {"evidence": [_met("", ["Procedure/pt-1"])]}
    out = citation_gate.citation_gate(state)
    assert out["citation_ok"] is False
    assert out["citation_errors"]

    rejected = citation_gate.citation_reject({**state, **out})
    assert rejected["final_decision"].startswith("rejected_uncited_claim:")

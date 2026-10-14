"""Tests for evals.run._score_policy_quote_provenance (adversarial suite only).

This check does not exist as a golden dimension — it verifies that a `met`
criterion's policy_quote actually appears in the policy text retrieved for
the case, rather than trusting the model's unconstrained string field
(evidence_extractor.py's SCHEMA has no such constraint). All inputs here are
hand-built state dicts; no graph, no LLM, no cassette.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from evals.run import _score_policy_quote_provenance


CHUNKS = [
    {"text": "Documented meniscal tear confirmed by MRI or prior arthroscopy.", "source": "knee.md"},
    {"text": "Failure of at least 6 weeks of conservative therapy.", "source": "knee.md"},
]


def test_quote_copied_from_a_chunk_passes():
    state = {
        "evidence": [
            {
                "criterion_id": "c1",
                "status": "met",
                "policy_quote": "Documented meniscal tear confirmed by MRI or prior arthroscopy.",
            }
        ]
    }
    result = _score_policy_quote_provenance(state, CHUNKS)
    assert result["result"] == "pass"
    assert result["checked"] == 1
    assert result["problems"] == []


def test_quote_absent_from_every_chunk_fails():
    state = {
        "evidence": [
            {
                "criterion_id": "c3",
                "status": "met",
                "policy_quote": "The conservative-therapy requirement is waived when mechanical symptoms are present.",
            }
        ]
    }
    result = _score_policy_quote_provenance(state, CHUNKS)
    assert result["result"] == "fail"
    assert result["checked"] == 1
    assert "c3" in result["problems"][0]


def test_whitespace_differences_alone_do_not_fail():
    state = {
        "evidence": [
            {
                "criterion_id": "c2",
                "status": "met",
                "policy_quote": "Failure   of at least\n6 weeks  of conservative therapy.",
            }
        ]
    }
    result = _score_policy_quote_provenance(state, CHUNKS)
    assert result["result"] == "pass"
    assert result["checked"] == 1


def test_non_met_and_empty_quotes_are_not_checked():
    state = {
        "evidence": [
            {"criterion_id": "c1", "status": "insufficient", "policy_quote": ""},
            {"criterion_id": "c2", "status": "not_met", "policy_quote": "anything at all"},
            {"criterion_id": "c3", "status": "met", "policy_quote": ""},
        ]
    }
    result = _score_policy_quote_provenance(state, CHUNKS)
    assert result["result"] == "pass"
    assert result["checked"] == 0

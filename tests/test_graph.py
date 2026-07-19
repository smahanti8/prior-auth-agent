"""Smoke tests for the non-LLM parts of the pipeline (no API key required)."""

import json
from pathlib import Path

from prior_auth_agent.nodes.intake import intake
from prior_auth_agent.nodes.eligibility import eligibility
from prior_auth_agent.graph import build_graph

SAMPLE = Path(__file__).resolve().parents[1] / "data" / "samples" / "sample_bundle.json"


def load_sample():
    return json.loads(SAMPLE.read_text())


def test_intake_accepts_valid_case():
    out = intake({"fhir_bundle": load_sample(), "cpt_code": "29881"})
    assert out["valid"], out["validation_errors"]
    assert out["case_id"].startswith("pa-")


def test_intake_rejects_bad_cpt():
    out = intake({"fhir_bundle": load_sample(), "cpt_code": "abc"})
    assert not out["valid"]
    assert any("CPT" in e for e in out["validation_errors"])


def test_intake_rejects_empty_bundle():
    out = intake({"fhir_bundle": {}, "cpt_code": "29881"})
    assert not out["valid"]


def test_eligibility_reads_coverage():
    out = eligibility({"fhir_bundle": load_sample()})
    assert out["eligible"]


def test_eligibility_missing_coverage():
    out = eligibility({"fhir_bundle": {"entry": []}})
    assert not out["eligible"]


def test_graph_compiles():
    graph = build_graph()
    assert graph is not None


def test_graph_short_circuits_invalid_input():
    graph = build_graph()
    result = graph.invoke({"fhir_bundle": {}, "cpt_code": "bad"})
    assert result["final_decision"].startswith("rejected_at_intake")

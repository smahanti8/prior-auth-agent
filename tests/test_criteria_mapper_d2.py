"""D-2 criteria mapper: encoded path, un-encoded fallback, divergence, predicate hints.

All tests run without an API key — structured_call is patched to return
controlled LLM output. Predicate evaluation uses the live encoded YAML specs
so tests also verify the schema + evaluator integration.
"""

import sys
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from prior_auth_agent.nodes.criteria_mapper import (
    _build_predicate_hint,
    _compute_divergence,
    _has_divergence,
    bundle_to_fragments,
    criteria_mapper,
)
from prior_auth_agent.policy.evaluator import EvalResult
from prior_auth_agent.policy.registry import load_by_cpt

CRITERIA_DIR = REPO_ROOT / "data" / "policy" / "criteria"
TODAY = "2026-07-29"


# ── helpers ────────────────────────────────────────────────────────────────────


def _state(cpt_code: str, bundle_entries=None) -> dict:
    return {
        "cpt_code":     cpt_code,
        "policy_chunks": [{"source": "test_policy.md", "text": "some policy text"}],
        "fhir_bundle":  {"entry": bundle_entries or []},
    }


def _all_mapped_llm_result() -> dict:
    return {
        "criteria": [
            {"encoded_id": "SURG-041-C1", "text": "LLM C1 text", "required": True},
            {"encoded_id": "SURG-041-C2", "text": "LLM C2 text", "required": True},
            {"encoded_id": "SURG-041-C3", "text": "LLM C3 text", "required": True},
        ]
    }


# ── bundle_to_fragments ────────────────────────────────────────────────────────


def test_bundle_to_fragments_groups_by_resource_type():
    bundle = {
        "entry": [
            {"resource": {"resourceType": "DiagnosticReport", "id": "dr-1"}},
            {"resource": {"resourceType": "Procedure",        "id": "proc-1"}},
            {"resource": {"resourceType": "DiagnosticReport", "id": "dr-2"}},
        ]
    }
    frags = bundle_to_fragments(bundle)
    assert len(frags["DiagnosticReport"]) == 2
    assert len(frags["Procedure"]) == 1


def test_bundle_to_fragments_empty_bundle():
    assert bundle_to_fragments({"entry": []}) == {}


def test_bundle_to_fragments_ignores_entry_without_resource_type():
    bundle = {"entry": [{"resource": {"id": "x"}}]}  # no resourceType
    assert bundle_to_fragments(bundle) == {}


# ── predicate hints ─────────────────────────────────────────────────────────────


def test_predicate_hint_met_contains_path_and_reason():
    result = EvalResult(status="met", reason="found it", matched_paths=["DiagnosticReport/dr-1"])
    hint = _build_predicate_hint(None, result)
    assert "MET" in hint
    assert "DiagnosticReport/dr-1" in hint


def test_predicate_hint_met_no_paths_uses_bundle():
    result = EvalResult(status="met", reason="found it", matched_paths=[])
    hint = _build_predicate_hint(None, result)
    assert "MET" in hint
    assert "bundle" in hint


def test_predicate_hint_not_met_instructs_do_not_mark_met():
    result = EvalResult(status="not_met", reason="partial-thickness present")
    hint = _build_predicate_hint(None, result)
    assert "NOT MET" in hint
    assert "Do not mark" in hint


def test_predicate_hint_insufficient_names_status():
    result = EvalResult(status="insufficient", reason="no DiagnosticReport")
    hint = _build_predicate_hint(None, result)
    assert "INSUFFICIENT" in hint


# ── divergence computation ──────────────────────────────────────────────────────


def test_divergence_no_divergence_when_all_matched():
    llm = [
        {"encoded_id": "SURG-041-C1", "text": "...", "required": True},
        {"encoded_id": "SURG-041-C2", "text": "...", "required": True},
        {"encoded_id": "SURG-041-C3", "text": "...", "required": True},
    ]
    d = _compute_divergence(llm, ["SURG-041-C1", "SURG-041-C2", "SURG-041-C3"])
    assert d == {"missing": [], "unmatched": [], "duplicate_mappings": []}
    assert not _has_divergence(d)


def test_divergence_missing_id():
    llm = [
        {"encoded_id": "SURG-041-C1", "text": "...", "required": True},
        {"encoded_id": "SURG-041-C3", "text": "...", "required": True},
        # C2 absent from LLM mapping
    ]
    d = _compute_divergence(llm, ["SURG-041-C1", "SURG-041-C2", "SURG-041-C3"])
    assert "SURG-041-C2" in d["missing"]
    assert _has_divergence(d)


def test_divergence_unmatched_null_id():
    llm = [
        {"encoded_id": "SURG-041-C1", "text": "full-thickness tear", "required": True},
        {"encoded_id": None,           "text": "extra criterion",      "required": False},
    ]
    d = _compute_divergence(llm, ["SURG-041-C1"])
    assert len(d["unmatched"]) == 1
    assert d["unmatched"][0]["text"] == "extra criterion"
    assert _has_divergence(d)


def test_divergence_unmatched_invalid_id():
    """An encoded_id not in valid_ids is treated as unmatched (LLM hallucination)."""
    llm = [{"encoded_id": "SURG-041-C99", "text": "hallucinated", "required": True}]
    d = _compute_divergence(llm, ["SURG-041-C1"])
    assert len(d["unmatched"]) == 1
    assert "SURG-041-C1" in d["missing"]
    assert _has_divergence(d)


def test_divergence_duplicate_mapping():
    llm = [
        {"encoded_id": "SURG-041-C1", "text": "full-thickness tear",        "required": True},
        {"encoded_id": "SURG-041-C1", "text": "MRI confirms full-thickness", "required": True},
    ]
    d = _compute_divergence(llm, ["SURG-041-C1"])
    assert len(d["duplicate_mappings"]) == 1
    assert d["duplicate_mappings"][0]["encoded_id"] == "SURG-041-C1"
    assert len(d["duplicate_mappings"][0]["texts"]) == 2
    assert _has_divergence(d)


# ── un-encoded path ─────────────────────────────────────────────────────────────


def test_unencoded_path_returns_llm_criteria():
    with patch("prior_auth_agent.nodes.criteria_mapper.structured_call") as mock:
        mock.return_value = {
            "criteria": [{"id": "c1", "text": "some criterion", "required": True}]
        }
        result = criteria_mapper(_state("99999"))
    assert result["criteria"][0]["id"] == "c1"
    assert result["criteria_versions"] == []
    assert result["criteria_divergence"] is None


def test_unencoded_path_no_encoded_fields_on_criteria():
    with patch("prior_auth_agent.nodes.criteria_mapper.structured_call") as mock:
        mock.return_value = {
            "criteria": [{"id": "c1", "text": "some criterion", "required": True}]
        }
        result = criteria_mapper(_state("99999"))
    assert "encoded_version" not in result["criteria"][0]
    assert "predicate_hint"  not in result["criteria"][0]


def test_unencoded_path_schema_has_id_not_encoded_id():
    """Un-encoded path must use the legacy schema (no encoded_id field)."""
    with patch("prior_auth_agent.nodes.criteria_mapper.structured_call") as mock:
        mock.return_value = {"criteria": []}
        criteria_mapper(_state("99999"))
    schema = mock.call_args.kwargs.get("schema") or mock.call_args.args[2]
    item_props = schema["properties"]["criteria"]["items"]["properties"]
    assert "id" in item_props
    assert "encoded_id" not in item_props


# ── encoded path ───────────────────────────────────────────────────────────────


def test_encoded_path_criteria_text_from_spec_not_llm():
    """Criteria text comes from the encoded spec, not the LLM's paraphrase."""
    with patch("prior_auth_agent.nodes.criteria_mapper.structured_call") as mock:
        mock.return_value = _all_mapped_llm_result()
        result = criteria_mapper(_state("29827"))
    for c in result["criteria"]:
        assert "LLM" not in c["text"]


def test_encoded_path_all_criteria_have_encoded_version():
    with patch("prior_auth_agent.nodes.criteria_mapper.structured_call") as mock:
        mock.return_value = _all_mapped_llm_result()
        result = criteria_mapper(_state("29827"))
    assert all(c.get("encoded_version") for c in result["criteria"])


def test_encoded_path_all_criteria_have_predicate_hint():
    with patch("prior_auth_agent.nodes.criteria_mapper.structured_call") as mock:
        mock.return_value = _all_mapped_llm_result()
        result = criteria_mapper(_state("29827"))
    assert all(len(c.get("predicate_hint", "")) > 0 for c in result["criteria"])


def test_encoded_path_criteria_versions_contains_all_ids():
    with patch("prior_auth_agent.nodes.criteria_mapper.structured_call") as mock:
        mock.return_value = _all_mapped_llm_result()
        result = criteria_mapper(_state("29827"))
    version_ids = {v["criterion_id"] for v in result["criteria_versions"]}
    assert version_ids == {"SURG-041-C1", "SURG-041-C2", "SURG-041-C3"}


def test_encoded_path_no_divergence_is_none():
    with patch("prior_auth_agent.nodes.criteria_mapper.structured_call") as mock:
        mock.return_value = _all_mapped_llm_result()
        result = criteria_mapper(_state("29827"))
    assert result["criteria_divergence"] is None


def test_encoded_path_divergence_records_missing():
    with patch("prior_auth_agent.nodes.criteria_mapper.structured_call") as mock:
        mock.return_value = {
            "criteria": [
                {"encoded_id": "SURG-041-C1", "text": "...", "required": True},
                # C2 and C3 absent from LLM mapping
                {"encoded_id": None, "text": "extra", "required": False},
            ]
        }
        result = criteria_mapper(_state("29827"))
    div = result["criteria_divergence"]
    assert div is not None
    assert "SURG-041-C2" in div["missing"]
    assert "SURG-041-C3" in div["missing"]
    assert len(div["unmatched"]) == 1


def test_encoded_path_schema_has_encoded_id_not_id():
    """Encoded path must use the mapping schema (encoded_id field, not id)."""
    with patch("prior_auth_agent.nodes.criteria_mapper.structured_call") as mock:
        mock.return_value = _all_mapped_llm_result()
        criteria_mapper(_state("29827"))
    schema = mock.call_args.kwargs.get("schema") or mock.call_args.args[2]
    item_props = schema["properties"]["criteria"]["items"]["properties"]
    assert "encoded_id" in item_props
    assert "id" not in item_props


def test_encoded_path_system_prompt_contains_all_criterion_ids():
    with patch("prior_auth_agent.nodes.criteria_mapper.structured_call") as mock:
        mock.return_value = _all_mapped_llm_result()
        criteria_mapper(_state("29827"))
    system = mock.call_args.kwargs.get("system") or mock.call_args.args[0]
    assert "SURG-041-C1" in system
    assert "SURG-041-C2" in system
    assert "SURG-041-C3" in system


def test_encoded_path_predicate_hint_met_when_bundle_has_full_thickness_report():
    """With a full-thickness DiagnosticReport in the bundle, C1 hint says MET."""
    bundle_entries = [
        {
            "resource": {
                "resourceType": "DiagnosticReport",
                "id": "dr-mri-1",
                "conclusion": "Full-thickness tear of the supraspinatus tendon.",
            }
        }
    ]
    with patch("prior_auth_agent.nodes.criteria_mapper.structured_call") as mock:
        mock.return_value = _all_mapped_llm_result()
        result = criteria_mapper(_state("29827", bundle_entries=bundle_entries))
    c1 = next(c for c in result["criteria"] if c["id"] == "SURG-041-C1")
    assert "MET" in c1["predicate_hint"]


def test_encoded_path_predicate_hint_not_met_when_partial_thickness_only():
    """With only a partial-thickness finding, C1 hint says NOT MET."""
    bundle_entries = [
        {
            "resource": {
                "resourceType": "DiagnosticReport",
                "id": "dr-mri-2",
                "conclusion": "Partial-thickness tear of the supraspinatus tendon.",
            }
        }
    ]
    with patch("prior_auth_agent.nodes.criteria_mapper.structured_call") as mock:
        mock.return_value = _all_mapped_llm_result()
        result = criteria_mapper(_state("29827", bundle_entries=bundle_entries))
    c1 = next(c for c in result["criteria"] if c["id"] == "SURG-041-C1")
    assert "NOT MET" in c1["predicate_hint"]


def test_encoded_path_predicate_hint_insufficient_when_no_bundle():
    """With an empty bundle, all hints should be INSUFFICIENT."""
    with patch("prior_auth_agent.nodes.criteria_mapper.structured_call") as mock:
        mock.return_value = _all_mapped_llm_result()
        result = criteria_mapper(_state("29827"))
    for c in result["criteria"]:
        assert "INSUFFICIENT" in c["predicate_hint"]


# ── registry ────────────────────────────────────────────────────────────────────


def test_load_by_cpt_returns_three_criteria_for_29827():
    specs = load_by_cpt("29827", TODAY, CRITERIA_DIR)
    assert len(specs) == 3
    assert {s.criterion_id for s in specs} == {"SURG-041-C1", "SURG-041-C2", "SURG-041-C3"}


def test_load_by_cpt_returns_empty_for_unknown_cpt():
    assert load_by_cpt("99999", TODAY, CRITERIA_DIR) == []


def test_load_by_cpt_respects_as_of_date():
    """Criteria with effective_date 2026-01-01 are not returned for as_of_date 2025-01-01."""
    specs = load_by_cpt("29827", "2025-01-01", CRITERIA_DIR)
    assert specs == []


def test_load_by_cpt_results_sorted_by_criterion_id():
    specs = load_by_cpt("29827", TODAY, CRITERIA_DIR)
    ids = [s.criterion_id for s in specs]
    assert ids == sorted(ids)

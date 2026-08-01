"""Deterministic tests for the golden eval fixture schema and bundles.

All tests run without an LLM or API key. They verify:
  - All 5 YAML case specs load and pass Pydantic validation
  - The generator produces bundles with the exact resource IDs declared in specs
  - Case 5 (malformed) fails intake with 'Patient' in the error message
  - Cases 1-4 (valid bundles) all pass intake without errors
  - Every expected_citation for met criteria resolves in its generated bundle
    (confirms the ground truth and the bundle are internally consistent)
"""

import json
import sys
from pathlib import Path

import pytest

# Add repo root so evals.* is importable without installing it as a package.
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from evals.golden.schema import CaseSpec
from evals.golden.generator import generate_bundle, CASES_DIR, BUNDLES_DIR
from prior_auth_agent.nodes.intake import intake
from prior_auth_agent.validation import _build_ref_index


# ── helpers ────────────────────────────────────────────────────────────────────

def load_spec(case_id_prefix: str) -> CaseSpec:
    matches = sorted(CASES_DIR.glob(f"{case_id_prefix}*.yaml"))
    assert matches, f"no YAML found for prefix '{case_id_prefix}'"
    return CaseSpec.load(matches[0])


def load_bundle(case_id_prefix: str) -> dict:
    matches = sorted(BUNDLES_DIR.glob(f"{case_id_prefix}*.json"))
    assert matches, f"no bundle JSON found for prefix '{case_id_prefix}'"
    return json.loads(matches[0].read_text(encoding="utf-8"))


ALL_PREFIXES = ["case_001", "case_002", "case_003", "case_004", "case_005"]


# ── schema loading ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("prefix", ALL_PREFIXES)
def test_spec_loads_without_error(prefix):
    """Every YAML case spec must parse cleanly into a CaseSpec model."""
    spec = load_spec(prefix)
    assert spec.id.startswith("case_")
    assert spec.cpt


def test_all_five_specs_present():
    specs = CaseSpec.load_all(CASES_DIR)
    assert len(specs) == 5, f"expected 5 case specs, found {len(specs)}"


# ── generator correctness ──────────────────────────────────────────────────────

@pytest.mark.parametrize("prefix", ALL_PREFIXES)
def test_generated_bundle_has_expected_resource_ids(prefix):
    """Generator must produce entries whose (resourceType, id) match the spec."""
    spec = load_spec(prefix)
    bundle = generate_bundle(spec)
    ref_index = _build_ref_index(bundle)

    res = spec.bundle_resources
    for cond in res.conditions:
        assert ("Condition", cond.id) in ref_index, f"missing Condition/{cond.id}"
    for obs in res.observations:
        assert ("Observation", obs.id) in ref_index, f"missing Observation/{obs.id}"
    for img in res.imaging_studies:
        assert ("ImagingStudy", img.id) in ref_index, f"missing ImagingStudy/{img.id}"
    for proc in res.procedures:
        assert ("Procedure", proc.id) in ref_index, f"missing Procedure/{proc.id}"
    for dr in res.diagnostic_reports:
        assert ("DiagnosticReport", dr.id) in ref_index, f"missing DiagnosticReport/{dr.id}"

    patient_present = any(rt == "Patient" for rt, _ in ref_index)
    if res.omit_patient:
        assert not patient_present, "expected NO Patient but one was generated"
    elif res.patient is not None:
        assert patient_present, "expected a Patient but none was generated"


# ── intake gate behaviour ──────────────────────────────────────────────────────

@pytest.mark.parametrize("prefix", ["case_001", "case_002", "case_003", "case_004"])
def test_valid_bundles_pass_intake(prefix):
    """Bundles for cases 1-4 must satisfy all four intake() checks."""
    spec = load_spec(prefix)
    bundle = load_bundle(prefix)
    out = intake({"fhir_bundle": bundle, "cpt_code": spec.cpt})
    assert out["valid"], f"{prefix} failed intake: {out['validation_errors']}"
    assert out["validation_errors"] == []


def test_case_005_fails_intake_with_patient_error():
    """Case 5 must fail intake and the error must mention 'Patient'."""
    spec = load_spec("case_005")
    bundle = load_bundle("case_005")
    out = intake({"fhir_bundle": bundle, "cpt_code": spec.cpt})
    assert not out["valid"]
    assert any(
        spec.ground_truth.outcome.validation_error_contains in err
        for err in out["validation_errors"]
    ), f"expected 'Patient' in errors; got: {out['validation_errors']}"


# ── ground truth / bundle consistency ─────────────────────────────────────────

@pytest.mark.parametrize("prefix", ["case_001", "case_002", "case_003", "case_004"])
def test_met_expected_citations_resolve_in_bundle(prefix):
    """For every 'met' criterion in ground_truth, each expected_citation must
    resolve against the generated bundle. This confirms the spec is internally
    consistent: citations the human declared as expected actually exist."""
    spec = load_spec(prefix)
    bundle = generate_bundle(spec)
    ref_index = _build_ref_index(bundle)

    for criterion in spec.ground_truth.criteria:
        if criterion.expected_status != "met":
            continue
        for citation in criterion.expected_citations:
            parts = citation.split("/")
            assert len(parts) == 2, f"malformed expected citation '{citation}'"
            rtype, rid = parts
            assert (rtype, rid) in ref_index, (
                f"{prefix} c{criterion.id}: expected citation '{citation}' "
                f"is not in the generated bundle — fix the spec or the resource id"
            )


@pytest.mark.parametrize("prefix", ["case_002", "case_003"])
def test_insufficient_criteria_have_empty_expected_citations(prefix):
    """Criteria expected to be insufficient must declare no expected citations."""
    spec = load_spec(prefix)
    for criterion in spec.ground_truth.criteria:
        if criterion.expected_status == "insufficient":
            assert criterion.expected_citations == [], (
                f"{prefix} {criterion.id}: insufficient criterion must have "
                f"empty expected_citations, got {criterion.expected_citations}"
            )

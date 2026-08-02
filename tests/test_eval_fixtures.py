"""Deterministic tests for the golden eval fixture schema and bundles.

All tests run without an LLM or API key. They verify:
  - All 15 YAML case specs load and pass Pydantic validation
  - The generator produces bundles with the exact resource IDs declared in specs
  - Case 005 (malformed) fails intake with 'Patient' in the error message
  - Case 009 (lapsed coverage) fails eligibility with 'cancelled' in the notes
  - Cases 001-004 and 006-015 (excluding 005, 009) pass intake without errors
  - Active-coverage cases have a Coverage resource with status 'active'
  - The lapsed-coverage case has Coverage.status == 'cancelled'
  - Every expected_citation for met criteria resolves in its generated bundle
  - Every expected_citation for not_met criteria resolves in its generated bundle
  - Insufficient criteria have empty expected_citations
"""

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from evals.golden.schema import CaseSpec
from evals.golden.generator import generate_bundle, CASES_DIR, BUNDLES_DIR
from prior_auth_agent.nodes.intake import intake
from prior_auth_agent.nodes.eligibility import eligibility
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


ALL_PREFIXES = [
    "case_001", "case_002", "case_003", "case_004", "case_005",
    "case_006", "case_007", "case_008", "case_009", "case_010",
    "case_011", "case_012", "case_013", "case_014", "case_015",
]

# Cases expected to pass intake (all except 005 which intentionally omits Patient)
INTAKE_PASSING = [p for p in ALL_PREFIXES if p != "case_005"]

# Cases with active Coverage (all except 005 which has no Coverage, and 009 which is cancelled)
ACTIVE_COVERAGE = [
    p for p in ALL_PREFIXES if p not in ("case_005", "case_009")
]

# Determination-stage cases (reached the LLM — not intake or eligibility rejections)
DETERMINATION_PREFIXES = [
    p for p in ALL_PREFIXES if p not in ("case_005", "case_009")
]


# ── schema loading ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("prefix", ALL_PREFIXES)
def test_spec_loads_without_error(prefix):
    """Every YAML case spec must parse cleanly into a CaseSpec model."""
    spec = load_spec(prefix)
    assert spec.id.startswith("case_")
    assert spec.cpt


def test_all_fifteen_specs_present():
    specs = CaseSpec.load_all(CASES_DIR)
    assert len(specs) == 15, f"expected 15 case specs, found {len(specs)}"


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

    if res.coverage is not None:
        assert ("Coverage", res.coverage.id) in ref_index, (
            f"missing Coverage/{res.coverage.id}"
        )


# ── coverage resource correctness ─────────────────────────────────────────────

@pytest.mark.parametrize("prefix", ACTIVE_COVERAGE)
def test_active_coverage_cases_have_active_coverage(prefix):
    """Bundles for active-coverage cases must include a Coverage with status 'active'."""
    bundle = load_bundle(prefix)
    coverage_entries = [
        e["resource"] for e in bundle["entry"]
        if e["resource"]["resourceType"] == "Coverage"
    ]
    assert len(coverage_entries) == 1, (
        f"{prefix}: expected exactly 1 Coverage resource, found {len(coverage_entries)}"
    )
    assert coverage_entries[0]["status"] == "active", (
        f"{prefix}: Coverage.status expected 'active', got {coverage_entries[0]['status']!r}"
    )


def test_case_009_has_cancelled_coverage():
    """Case 009 must have Coverage.status == 'cancelled' to trigger eligibility rejection."""
    bundle = load_bundle("case_009")
    coverage_entries = [
        e["resource"] for e in bundle["entry"]
        if e["resource"]["resourceType"] == "Coverage"
    ]
    assert len(coverage_entries) == 1
    assert coverage_entries[0]["status"] == "cancelled"


def test_case_011_has_only_patient_and_coverage():
    """Case 011 (empty chart) must contain only Patient and Coverage — no clinical resources."""
    bundle = load_bundle("case_011")
    types = {e["resource"]["resourceType"] for e in bundle["entry"]}
    assert types == {"Patient", "Coverage"}, (
        f"case_011 expected only Patient+Coverage, got: {types}"
    )


# ── intake gate behaviour ──────────────────────────────────────────────────────

@pytest.mark.parametrize("prefix", INTAKE_PASSING)
def test_valid_bundles_pass_intake(prefix):
    """Bundles for cases that aren't malformed must satisfy all intake() checks."""
    spec = load_spec(prefix)
    bundle = load_bundle(prefix)
    out = intake({"fhir_bundle": bundle, "cpt_code": spec.cpt})
    assert out["valid"], f"{prefix} failed intake: {out['validation_errors']}"
    assert out["validation_errors"] == []


def test_case_005_fails_intake_with_patient_error():
    """Case 005 must fail intake and the error must mention 'Patient'."""
    spec = load_spec("case_005")
    bundle = load_bundle("case_005")
    out = intake({"fhir_bundle": bundle, "cpt_code": spec.cpt})
    assert not out["valid"]
    assert any(
        spec.ground_truth.outcome.validation_error_contains in err
        for err in out["validation_errors"]
    ), f"expected 'Patient' in errors; got: {out['validation_errors']}"


# ── eligibility gate behaviour ─────────────────────────────────────────────────

def test_case_009_fails_eligibility_with_cancelled():
    """Case 009 must fail eligibility and notes must mention 'cancelled'."""
    spec = load_spec("case_009")
    bundle = load_bundle("case_009")
    # Must first pass intake
    intake_out = intake({"fhir_bundle": bundle, "cpt_code": spec.cpt})
    assert intake_out["valid"], "case_009 failed intake unexpectedly"
    # Now check eligibility
    elig_out = eligibility({**intake_out, "fhir_bundle": bundle})
    assert not elig_out["eligible"]
    assert spec.ground_truth.outcome.validation_error_contains in elig_out.get(
        "eligibility_notes", ""
    ), (
        f"expected 'cancelled' in eligibility_notes; "
        f"got: {elig_out.get('eligibility_notes')!r}"
    )


# ── ground truth / bundle consistency ─────────────────────────────────────────

@pytest.mark.parametrize("prefix", DETERMINATION_PREFIXES)
def test_met_expected_citations_resolve_in_bundle(prefix):
    """For every 'met' criterion in ground_truth, each expected_citation must
    resolve against the generated bundle."""
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
                f"{prefix} {criterion.id}: expected citation '{citation}' "
                f"is not in the generated bundle — fix the spec or the resource id"
            )


@pytest.mark.parametrize("prefix", DETERMINATION_PREFIXES)
def test_not_met_expected_citations_resolve_in_bundle(prefix):
    """For every 'not_met' criterion in ground_truth, each expected_citation must
    resolve against the generated bundle (the resource documenting the contradiction
    must exist)."""
    spec = load_spec(prefix)
    bundle = generate_bundle(spec)
    ref_index = _build_ref_index(bundle)

    for criterion in spec.ground_truth.criteria:
        if criterion.expected_status != "not_met":
            continue
        for citation in criterion.expected_citations:
            parts = citation.split("/")
            assert len(parts) == 2, f"malformed expected citation '{citation}'"
            rtype, rid = parts
            assert (rtype, rid) in ref_index, (
                f"{prefix} {criterion.id}: not_met expected citation '{citation}' "
                f"is not in the bundle — the contradicting resource must exist"
            )


@pytest.mark.parametrize("prefix", ["case_002", "case_003", "case_011"])
def test_insufficient_criteria_have_empty_expected_citations(prefix):
    """Criteria expected to be insufficient must declare no expected citations."""
    spec = load_spec(prefix)
    for criterion in spec.ground_truth.criteria:
        if criterion.expected_status == "insufficient":
            assert criterion.expected_citations == [], (
                f"{prefix} {criterion.id}: insufficient criterion must have "
                f"empty expected_citations, got {criterion.expected_citations}"
            )


def test_case_007_ghost_citation_absent_from_bundle():
    """The ghost reference in case_007 must NOT be in the bundle — that's the
    whole point of the strip-and-log test."""
    bundle = load_bundle("case_007")
    ref_index = _build_ref_index(bundle)
    assert ("Observation", "steroid-injection-007") not in ref_index, (
        "Observation/steroid-injection-007 must NOT be a bundle entry for "
        "the strip-and-log test to work"
    )

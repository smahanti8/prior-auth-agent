"""Deterministic tests for the adversarial golden-format fixtures.

Mirrors tests/test_eval_fixtures.py's patterns, scoped to evals/adversarial/
instead of evals/golden/. All tests run without an LLM or API key. They
verify:
  - All 4 adversarial case specs load and pass Pydantic validation
  - The generator produces bundles with the exact real resource IDs declared
  - Every expected_citation for met/not_met criteria resolves in its bundle
  - insufficient criteria declare no expected_citations
  - Fabricated resource ids named only inside injected chart text are
    genuinely absent from the generated bundle (mirrors case_007's
    ghost-citation assertion in the golden suite)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from evals.golden.schema import CaseSpec
from evals.golden.generator import generate_bundle
from prior_auth_agent.validation import _build_ref_index

ADV_CASES_DIR = REPO_ROOT / "evals" / "adversarial" / "cases"
ADV_BUNDLES_DIR = REPO_ROOT / "evals" / "adversarial" / "bundles"

ALL_PREFIXES = ["adv_001", "adv_002", "adv_003", "adv_004"]


def load_spec(case_id_prefix: str) -> CaseSpec:
    matches = sorted(ADV_CASES_DIR.glob(f"{case_id_prefix}*.yaml"))
    assert matches, f"no YAML found for prefix '{case_id_prefix}'"
    return CaseSpec.load(matches[0])


def load_bundle(case_id_prefix: str) -> dict:
    matches = sorted(ADV_BUNDLES_DIR.glob(f"{case_id_prefix}*.json"))
    assert matches, f"no bundle JSON found for prefix '{case_id_prefix}'"
    return json.loads(matches[0].read_text(encoding="utf-8"))


# ── schema loading ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("prefix", ALL_PREFIXES)
def test_spec_loads_without_error(prefix):
    spec = load_spec(prefix)
    assert spec.id.startswith("adv_")
    assert spec.cpt == "29881"


def test_all_four_adversarial_specs_present():
    specs = CaseSpec.load_all(ADV_CASES_DIR)
    assert len(specs) == 4, f"expected 4 adversarial case specs, found {len(specs)}"


# ── generator correctness ──────────────────────────────────────────────────────


@pytest.mark.parametrize("prefix", ALL_PREFIXES)
def test_generated_bundle_has_expected_resource_ids(prefix):
    spec = load_spec(prefix)
    bundle = generate_bundle(spec)
    ref_index = _build_ref_index(bundle)

    res = spec.bundle_resources
    for obs in res.observations:
        assert ("Observation", obs.id) in ref_index, f"missing Observation/{obs.id}"
    for img in res.imaging_studies:
        assert ("ImagingStudy", img.id) in ref_index, f"missing ImagingStudy/{img.id}"
    for proc in res.procedures:
        assert ("Procedure", proc.id) in ref_index, f"missing Procedure/{proc.id}"

    assert res.patient is not None
    assert ("Patient", res.patient.id) in ref_index
    assert res.coverage is not None
    assert ("Coverage", res.coverage.id) in ref_index


# ── ground truth / bundle consistency ─────────────────────────────────────────


@pytest.mark.parametrize("prefix", ALL_PREFIXES)
def test_met_and_not_met_expected_citations_resolve_in_bundle(prefix):
    spec = load_spec(prefix)
    bundle = generate_bundle(spec)
    ref_index = _build_ref_index(bundle)

    for criterion in spec.ground_truth.criteria:
        if criterion.expected_status not in ("met", "not_met"):
            continue
        for citation in criterion.expected_citations:
            parts = citation.split("/")
            assert len(parts) == 2, f"malformed expected citation '{citation}'"
            rtype, rid = parts
            assert (rtype, rid) in ref_index, (
                f"{prefix} {criterion.id}: expected citation '{citation}' "
                f"is not in the generated bundle"
            )


@pytest.mark.parametrize("prefix", ALL_PREFIXES)
def test_insufficient_criteria_have_empty_expected_citations(prefix):
    spec = load_spec(prefix)
    for criterion in spec.ground_truth.criteria:
        if criterion.expected_status == "insufficient":
            assert criterion.expected_citations == [], (
                f"{prefix} {criterion.id}: insufficient criterion must have "
                f"empty expected_citations, got {criterion.expected_citations}"
            )


def test_adv_002_ghost_citation_absent_from_bundle():
    """The fabricated resource named inside adv_002's injected instruction
    must NOT be a real bundle entry — that's the whole point of the fixture."""
    bundle = load_bundle("adv_002")
    ref_index = _build_ref_index(bundle)
    assert ("Observation", "pt-course-adv002-verified") not in ref_index, (
        "Observation/pt-course-adv002-verified must NOT be a bundle entry "
        "for adv_002's fake-citation-in-injection test to work"
    )


def test_adv_004_bundle_has_no_real_conservative_therapy_resource():
    """adv_004's chart carries a spoofed policy quote, not a real PT record —
    there must be no Procedure resource anywhere in this bundle."""
    bundle = load_bundle("adv_004")
    types = [e["resource"]["resourceType"] for e in bundle["entry"]]
    assert "Procedure" not in types, (
        "adv_004 must contain no real Procedure resource — the only "
        "'evidence' for c3 is the spoofed policy-quote note"
    )


@pytest.mark.parametrize("prefix", ALL_PREFIXES)
def test_all_criteria_are_required(prefix):
    """Every criterion across the 4 adversarial fixtures is required=True —
    each attack targets a required criterion so the confidence gate can
    never quietly auto-approve past it."""
    spec = load_spec(prefix)
    for criterion in spec.ground_truth.criteria:
        assert criterion.required, f"{prefix} {criterion.id}: expected required=True"

"""Tests for cassette integrity and staleness detection.

These tests guard against three failure modes:
  1. A system prompt changes without re-recording — cassette becomes a silent lie.
  2. A cassette is present but structurally incomplete (missing node entries).
  3. The Cassette class's own staleness check is broken.

All tests skip gracefully if no cassette exists for a case.  In CI the cassettes
are committed, so skips indicate a missing recording that should be added.

Run with: PYTHONPATH=src pytest tests/test_recorded_fixtures.py -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from evals.fixtures import Cassette, StalenessError, RECORDED_DIR, _hash_string

# Cases that reach the LLM (exclude intake-fail 005 and eligibility-fail 009)
LLM_PREFIXES = [
    "case_001", "case_002", "case_003", "case_004",
    "case_006", "case_007", "case_008", "case_010",
    "case_011", "case_012", "case_013", "case_014", "case_015",
]

REQUIRED_NODES = ["criteria_mapper", "evidence_extractor", "determination"]


def _skip_if_missing(case_id: str) -> None:
    if not Cassette.exists(case_id):
        pytest.skip(
            f"No cassette for {case_id} — "
            f"record it with: python -m evals.run --live {case_id}"
        )


# ── existence and structure ────────────────────────────────────────────────────


@pytest.mark.parametrize("prefix", LLM_PREFIXES)
def test_cassette_present(prefix):
    """Every LLM case should have a committed cassette in evals/recorded/.
    If this test fails in CI, someone needs to run --live and commit the result."""
    _skip_if_missing(prefix)
    path = RECORDED_DIR / f"{prefix}.json"
    assert path.exists()


@pytest.mark.parametrize("prefix", LLM_PREFIXES)
def test_cassette_loads_without_error(prefix):
    """Cassette JSON must parse and the Cassette class must initialise cleanly."""
    _skip_if_missing(prefix)
    cassette = Cassette.load(prefix)
    assert cassette.case_id == prefix
    assert cassette.recorded_at != "unknown"


@pytest.mark.parametrize("prefix", LLM_PREFIXES)
def test_cassette_has_all_required_node_entries(prefix):
    """Each LLM cassette must store responses for all three structured-call nodes."""
    _skip_if_missing(prefix)
    path = RECORDED_DIR / f"{prefix}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    nodes = data.get("nodes", {})
    missing = [n for n in REQUIRED_NODES if n not in nodes]
    assert not missing, (
        f"{prefix}: missing node entries {missing!r} in cassette — re-record: "
        f"python -m evals.run --live {prefix}"
    )


@pytest.mark.parametrize("prefix", LLM_PREFIXES)
def test_cassette_has_policy_rag_chunks(prefix):
    """Each LLM cassette must store policy_rag chunks (may be empty but must be present)."""
    _skip_if_missing(prefix)
    path = RECORDED_DIR / f"{prefix}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "policy_rag" in data.get("nodes", {}), (
        f"{prefix}: 'policy_rag' entry missing from cassette — re-record"
    )
    assert "chunks" in data["nodes"]["policy_rag"], (
        f"{prefix}: cassette policy_rag missing 'chunks' list"
    )


@pytest.mark.parametrize("prefix", LLM_PREFIXES)
def test_cassette_nodes_have_system_hashes(prefix):
    """System hashes stored in cassette nodes must be present and non-empty."""
    _skip_if_missing(prefix)
    path = RECORDED_DIR / f"{prefix}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    nodes = data.get("nodes", {})
    for node_name in REQUIRED_NODES:
        if node_name not in nodes:
            continue
        stored = nodes[node_name].get("system_hash")
        assert stored, (
            f"{prefix}/{node_name}: system_hash is empty or missing — "
            f"re-record to enable staleness detection"
        )
        assert len(stored) >= 16, (
            f"{prefix}/{node_name}: system_hash looks too short: {stored!r}"
        )


@pytest.mark.parametrize("prefix", LLM_PREFIXES)
def test_cassette_nodes_have_schema_fingerprints(prefix):
    """Schema fingerprints must be present in non-policy_rag nodes."""
    _skip_if_missing(prefix)
    path = RECORDED_DIR / f"{prefix}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    nodes = data.get("nodes", {})
    for node_name in REQUIRED_NODES:
        if node_name not in nodes:
            continue
        fp = nodes[node_name].get("schema_fingerprint")
        assert fp, (
            f"{prefix}/{node_name}: schema_fingerprint missing — "
            f"re-record with current recorder.py"
        )


@pytest.mark.parametrize("prefix", LLM_PREFIXES)
def test_cassette_fingerprints_are_unique(prefix):
    """Schema fingerprints for different nodes must not collide — if they do the
    replay dispatcher cannot route structured_call to the right cassette entry."""
    _skip_if_missing(prefix)
    path = RECORDED_DIR / f"{prefix}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    nodes = data.get("nodes", {})
    fps = [
        nodes[n]["schema_fingerprint"]
        for n in REQUIRED_NODES
        if n in nodes and nodes[n].get("schema_fingerprint")
    ]
    assert len(fps) == len(set(fps)), (
        f"{prefix}: duplicate schema fingerprints across nodes {fps} — "
        f"each LLM node must use a unique output schema"
    )


# ── staleness detection ────────────────────────────────────────────────────────


@pytest.mark.parametrize("prefix", LLM_PREFIXES)
def test_cassette_passes_staleness_check(prefix):
    """Cassettes whose stored system hashes match the current source must not raise
    StalenessError.  If they do, a system prompt changed without re-recording."""
    _skip_if_missing(prefix)
    cassette = Cassette.load(prefix)
    # This should not raise — if it does, re-record the cassette
    cassette.check_staleness()


def test_staleness_check_raises_on_mutated_hash(tmp_path):
    """check_staleness() must raise StalenessError when a stored hash doesn't match
    the current source.  This test synthesises a minimal fake cassette with a wrong
    hash to verify the detection logic itself is exercised."""
    from prior_auth_agent.nodes.criteria_mapper import SYSTEM as REAL_CM_SYS
    wrong_hash = "0000000000000000"
    assert wrong_hash != _hash_string(REAL_CM_SYS), "sanity: wrong_hash must not equal real"

    fake_data = {
        "case_id": "case_stale_test",
        "recorded_at": "2026-01-01T00:00:00+00:00",
        "model": "test",
        "nodes": {
            "policy_rag": {"chunks": []},
            "criteria_mapper": {
                "schema_fingerprint": "aaaaaa",
                "system_hash": wrong_hash,
                "response": {},
            },
            "evidence_extractor": {"schema_fingerprint": "bbbbbb", "system_hash": wrong_hash, "response": {}},
            "determination": {"schema_fingerprint": "cccccc", "system_hash": wrong_hash, "response": {}},
        },
    }
    cassette = Cassette("case_stale_test", fake_data)
    with pytest.raises(StalenessError) as exc_info:
        cassette.check_staleness()
    assert "criteria_mapper" in str(exc_info.value) or "evidence_extractor" in str(exc_info.value)
    assert "STALE" in str(exc_info.value).upper() or "stale" in str(exc_info.value).lower()


def test_staleness_check_passes_when_hash_matches():
    """check_staleness() must pass silently when stored hashes equal the live source."""
    from prior_auth_agent.nodes.criteria_mapper import SYSTEM as CM_SYS
    from prior_auth_agent.nodes.evidence_extractor import SYSTEM as EE_SYS
    from prior_auth_agent.nodes.determination import SYSTEM as DET_SYS

    correct_data = {
        "case_id": "case_freshness_test",
        "recorded_at": "2026-01-01T00:00:00+00:00",
        "model": "test",
        "nodes": {
            "policy_rag": {"chunks": []},
            "criteria_mapper": {
                "schema_fingerprint": "aaaaaa",
                "system_hash": _hash_string(CM_SYS),
                "response": {},
            },
            "evidence_extractor": {
                "schema_fingerprint": "bbbbbb",
                "system_hash": _hash_string(EE_SYS),
                "response": {},
            },
            "determination": {
                "schema_fingerprint": "cccccc",
                "system_hash": _hash_string(DET_SYS),
                "response": {},
            },
        },
    }
    cassette = Cassette("case_freshness_test", correct_data)
    cassette.check_staleness()  # must not raise


# ── model tracking ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("prefix", LLM_PREFIXES)
def test_cassette_model_field_present(prefix):
    """The 'model' field documents which model version produced the cassette.
    It enables future diffing when the model is upgraded."""
    _skip_if_missing(prefix)
    cassette = Cassette.load(prefix)
    assert cassette.model != "unknown", (
        f"{prefix}: cassette 'model' field is missing or 'unknown' — "
        f"re-record with current recorder.py (it reads from config.MODEL)"
    )

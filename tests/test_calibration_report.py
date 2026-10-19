"""Tests for the standalone calibration report (evals/calibration.py).

The Brier computation itself lives in prior_auth_agent.audit_packet
(_calibration_metrics) and is exercised by tests/test_audit_packet.py; these
tests only cover the gate logic and reporting shape that evals/calibration.py
adds on top of it.
"""

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT))

from evals.calibration import (
    DEFAULT_BIN_COUNT,
    MIN_PER_BIN,
    build_report,
    min_n_for_bins,
    write_artifact,
)


# A tiny, hand-computed fixture: 3 determination-stage pairs.
# Brier = mean((confidence - outcome)^2)
#   case_a: (0.9 - 1)**2  = 0.01     (correct)
#   case_b: (0.6 - 0.0)**2 = 0.36    (incorrect)
#   case_c: (0.7 - 1)**2  = 0.09     (correct)
# mean = (0.01 + 0.36 + 0.09) / 3 = 0.153333...
TINY_EVAL_DATA = {
    "cases": [
        {
            "case_id": "case_a",
            "scores": {"determination": {"result": "pass", "confidence": 0.9}},
        },
        {
            "case_id": "case_b",
            "scores": {"determination": {"result": "fail", "confidence": 0.6}},
        },
        {
            "case_id": "case_c",
            "scores": {"determination": {"result": "pass", "confidence": 0.7}},
        },
        {
            "case_id": "case_skip",
            "scores": {"determination": {"result": "skip", "confidence": None}},
        },
    ]
}


def test_min_n_for_bins_default():
    assert min_n_for_bins(DEFAULT_BIN_COUNT) == DEFAULT_BIN_COUNT * MIN_PER_BIN
    assert min_n_for_bins(10) == 50


def test_build_report_brier_matches_hand_computation():
    report = build_report(TINY_EVAL_DATA)
    expected = ((0.9 - 1) ** 2 + (0.6 - 0.0) ** 2 + (0.7 - 1) ** 2) / 3
    assert report["n"] == 3
    assert report["brier_score"] == pytest.approx(expected, abs=1e-6)


def test_build_report_gates_ece_when_n_too_small():
    report = build_report(TINY_EVAL_DATA, bin_count=10)
    assert report["ece"] is None
    assert report["bin_count"] == 10
    assert report["min_n_for_bins"] == 50
    assert report["ece_note"] == (
        "ECE not reported: N too small; minimum N for 10 bins is 50."
    )


def test_build_report_no_data():
    report = build_report(None)
    assert report["n"] == 0
    assert report["brier_score"] is None
    assert report["ece"] is None
    assert "ECE not reported" in report["ece_note"]


def test_write_artifact_round_trips(tmp_path, monkeypatch):
    import evals.calibration as calibration_mod

    monkeypatch.setattr(calibration_mod, "RESULTS_DIR", tmp_path)
    report = build_report(TINY_EVAL_DATA)
    out = write_artifact(report)

    assert out.exists()
    assert out.parent == tmp_path
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["n"] == 3
    assert payload["brier_score"] == pytest.approx(report["brier_score"])
    assert payload["ece"] is None
    assert "generated_at" in payload

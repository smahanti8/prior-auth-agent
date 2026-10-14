"""Regression test for a real bug found during adversarial-suite recording,
2026-09-27: save_cassette() ignored its caller's recorded_dir entirely and
always wrote to the golden evals/recorded/ directory, even when run.py's
--suite adversarial correctly resolved a different (adversarial) directory
for everything else. Live-recording four adversarial cassettes wrote them
into the golden cassette directory instead of evals/adversarial/recorded/.
"""
from __future__ import annotations

from evals.recorder import CallCapture, save_cassette


def test_save_cassette_writes_to_the_given_recorded_dir_not_the_golden_default(tmp_path):
    capture = CallCapture()
    scratch_dir = tmp_path / "not_the_golden_dir"

    path = save_cassette("case_regression_test", capture, [], recorded_dir=scratch_dir)

    assert path.parent == scratch_dir
    assert path.exists()
    assert path.name == "case_regression_test.json"

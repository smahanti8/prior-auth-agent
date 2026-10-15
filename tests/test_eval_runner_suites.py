"""Tests for evals.run's --suite selection.

Covers: the golden suite stays the default with unchanged directories; the
adversarial suite is discovered from an isolated (monkeypatched) directory
pair and never mixes with the golden cases/recordings; an adversarial case
with no cassette is reported as NOT EVALUATED (never SKIP, never scored as
passing); --require-recorded turns that into a non-zero exit; and
--baseline-check/--update-baseline refuse to run under the adversarial
suite. All cases here are synthetic and deterministic-stage or intentionally
uncassetted, so no LLM call or API key is ever needed.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from evals import run as run_module
from evals.golden.generator import CASES_DIR as GOLDEN_CASES_DIR
from evals.fixtures import RECORDED_DIR as GOLDEN_RECORDED_DIR


# ── _resolve_suite ──────────────────────────────────────────────────────────────


def test_default_suite_is_golden_with_unchanged_directories():
    suite, cases_dir, recorded_dir, rest = run_module._resolve_suite(["case_001"])
    assert suite == "golden"
    assert cases_dir == GOLDEN_CASES_DIR
    assert recorded_dir == GOLDEN_RECORDED_DIR
    assert rest == ["case_001"]


def test_suite_flag_resolves_adversarial_directories():
    suite, cases_dir, recorded_dir, rest = run_module._resolve_suite(
        ["--suite", "adversarial", "--live"]
    )
    assert suite == "adversarial"
    assert cases_dir == run_module.ADVERSARIAL_CASES_DIR
    assert recorded_dir == run_module.ADVERSARIAL_RECORDED_DIR
    assert rest == ["--live"]
    # the value token must be stripped out, not left to be mistaken for a
    # case-id prefix filter
    assert "adversarial" not in rest


def test_unknown_suite_raises():
    with pytest.raises(SystemExit):
        run_module._resolve_suite(["--suite", "nonexistent"])


# ── main(): adversarial discovery + NOT EVALUATED + require-recorded ──────────


_SYNTHETIC_CASE_YAML = """\
id: synth_001
title: "Synthetic determination-stage case with no recording"
cpt: "99999"
date: "2026-01-01"
bundle_resources:
  patient:
    id: patient-synth001
    age: 40
    sex: M
  coverage:
    id: coverage-synth001
    status: active
    payer_display: "Acme Health Plan"
ground_truth:
  criteria: []
  outcome:
    stage: determination
    decision: insufficient_evidence
    route: hitl
    rationale: "Synthetic fixture for runner-suite tests only."
"""


@pytest.fixture
def adversarial_dirs(tmp_path, monkeypatch):
    cases_dir = tmp_path / "cases"
    recorded_dir = tmp_path / "recorded"
    cases_dir.mkdir()
    recorded_dir.mkdir()
    (cases_dir / "synth_001.yaml").write_text(_SYNTHETIC_CASE_YAML, encoding="utf-8")
    monkeypatch.setitem(
        run_module.SUITES,
        "adversarial",
        {"cases_dir": cases_dir, "recorded_dir": recorded_dir},
    )
    return cases_dir, recorded_dir


def test_adversarial_case_without_cassette_is_not_evaluated(adversarial_dirs, capsys):
    exit_code = run_module.main(["--suite", "adversarial"])
    out = capsys.readouterr().out
    assert "NOT EVALUATED" in out
    assert "SKIP" not in out  # adversarial must never print golden's SKIP wording
    assert "EVALUATED 0 of 1" in out
    assert exit_code == 0  # default stays green


def test_require_recorded_fails_when_nothing_recorded(adversarial_dirs, capsys):
    exit_code = run_module.main(["--suite", "adversarial", "--require-recorded"])
    assert exit_code == 1


def test_baseline_check_refuses_under_adversarial_suite(adversarial_dirs, capsys):
    exit_code = run_module.main(["--suite", "adversarial", "--baseline-check"])
    out = capsys.readouterr().out
    assert "not supported for --suite" in out
    assert exit_code == 1


def test_update_baseline_refuses_under_adversarial_suite(adversarial_dirs, capsys):
    exit_code = run_module.main(["--suite", "adversarial", "--update-baseline"])
    out = capsys.readouterr().out
    assert "not supported for --suite" in out
    assert exit_code == 1


def test_adversarial_case_that_fails_exits_nonzero_without_require_recorded(
    adversarial_dirs, capsys
):
    # Regression: the adversarial exit code only looked at NOT EVALUATED, so a
    # case that was recorded but failed (stale cassette, replay error, or a
    # real invariant break) still exited 0 and a CI step could not catch it.
    # Found while adding the --require-recorded CI step (2026-10-14).
    _, recorded_dir = adversarial_dirs
    (recorded_dir / "synth_001.json").write_text(
        '{"case_id": "synth_001", "nodes": {}}', encoding="utf-8"
    )
    exit_code = run_module.main(["--suite", "adversarial"])
    out = capsys.readouterr().out
    assert "EVALUATED 1 of 1" in out
    assert exit_code == 1

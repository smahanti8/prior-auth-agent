"""Tests for evals/tier_analysis.py's pure reporting logic.

Scope: _recommendation() and print_tier_table() are deterministic given
already-computed baseline/tier result dicts — no live API calls, no graph
execution, no mocking of core pipeline components. run_tier() and main()
are excluded: they require a real ANTHROPIC_API_KEY and live model calls
(tiering changes model outputs, so cassette replay can't stand in), which
is why this module had zero test coverage before now.

Includes a direct regression test for a real bug found in this session: the
cost-comparison row hardcoded a leading "-" before the savings percentage,
so a node whose cost happened to increase relative to baseline (pure
live-run noise on an untiered node) rendered as "--6%" instead of "+6%".
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from evals.tier_analysis import _recommendation, print_tier_table

# The actual bug signature: "(--6%)" — a percentage cell with a doubled sign.
# Plain "--" alone isn't safe to assert against: the markdown table separator
# row ("|---------|") legitimately contains it.
_DOUBLE_SIGN = re.compile(r"\(--\d")


# ── fixtures ──────────────────────────────────────────────────────────────────


def _cost_report(node_costs: dict[str, float]) -> dict:
    return {
        "nodes": [
            {
                "node_name": name, "node_type": "llm", "model": "m",
                "n": 13, "avg_latency_ms": 0.0,
                "avg_input_tokens": 100, "avg_output_tokens": 50,
                "avg_cost_usd": cost,
            }
            for name, cost in node_costs.items()
        ],
        "total_cost_usd": sum(node_costs.values()),
        "total_latency_ms": 0.0,
        "projections": {},
    }


def _tier_result(label, overrides, pass_counts, scoreable, node_costs):
    return {
        "tier_label": label,
        "node_overrides": overrides,
        "case_count": 13,
        "pass_counts": pass_counts,
        "scoreable": scoreable,
        "cost_report": _cost_report(node_costs),
    }


BASELINE = _tier_result(
    "Baseline", {},
    pass_counts={"determination": 10, "routing": 11, "criterion_evidence": 10, "citation_validity": 13},
    scoreable={"determination": 13, "routing": 13, "criterion_evidence": 12, "citation_validity": 13},
    node_costs={"criteria_mapper": 0.0782, "evidence_extractor": 0.1001, "determination": 0.0706},
)


# ── _recommendation() ────────────────────────────────────────────────────────


def test_recommendation_no_accuracy_change():
    text = _recommendation(
        node="criteria_mapper", baseline_cost_1m=78_200, tier_cost_1m=2_300,
        tier_model="claude-haiku-4-5-20251001",
        baseline_s3=10, tier_s3=10, scoreable=12,
    )
    assert "**can** drop to" in text
    assert "no measured accuracy loss" in text
    assert "S3: 10/12 → 10/12" in text
    assert "$75,900/month" in text


def test_recommendation_accuracy_improves():
    text = _recommendation(
        node="determination", baseline_cost_1m=70_600, tier_cost_1m=2_200,
        tier_model="claude-haiku-4-5-20251001",
        baseline_s3=10, tier_s3=11, scoreable=12,
    )
    assert "**improves**" in text
    assert "+1 S3 cases" in text
    assert "S3: 10/12 → 11/12" in text


def test_recommendation_accuracy_regresses():
    text = _recommendation(
        node="evidence_extractor", baseline_cost_1m=100_100, tier_cost_1m=3_300,
        tier_model="claude-haiku-4-5-20251001",
        baseline_s3=10, tier_s3=8, scoreable=12,
    )
    assert "**improves**" not in text
    assert "**can**" not in text
    assert "2-point drop in criterion evidence accuracy" in text
    assert "S3: 10/12 → 8/12" in text


# ── print_tier_table cost row — regression test for the "--6%" bug ────────────


def test_cost_row_never_prints_double_negative_sign(capsys):
    """A node's cost can go UP relative to baseline in a tier run where that
    node wasn't even the one tiered — pure live-run noise on the untouched
    node. Before the fix, the hardcoded leading "-" made this render as
    "--6%" (two minus signs). Must never happen."""
    tier = _tier_result(
        "Haiku — evidence_extractor",
        {"evidence_extractor": "claude-haiku-4-5-20251001"},
        pass_counts={"determination": 9, "routing": 9, "criterion_evidence": 8, "citation_validity": 13},
        scoreable={"determination": 13, "routing": 13, "criterion_evidence": 12, "citation_validity": 13},
        # determination wasn't tiered here but its measured cost came in
        # slightly higher than baseline on this particular live run.
        node_costs={"criteria_mapper": 0.0812, "evidence_extractor": 0.0033, "determination": 0.0745},
    )
    print_tier_table(BASELINE, [tier])
    out = capsys.readouterr().out
    assert not _DOUBLE_SIGN.search(out)
    assert "$74,500 (+6%)" in out


def test_cost_row_prints_correct_savings_sign(capsys):
    tier = _tier_result(
        "Haiku — determination",
        {"determination": "claude-haiku-4-5-20251001"},
        pass_counts={"determination": 11, "routing": 11, "criterion_evidence": 11, "citation_validity": 13},
        scoreable={"determination": 13, "routing": 13, "criterion_evidence": 12, "citation_validity": 13},
        node_costs={"criteria_mapper": 0.0782, "evidence_extractor": 0.1001, "determination": 0.0022},
    )
    print_tier_table(BASELINE, [tier])
    out = capsys.readouterr().out
    assert "$2,200 (-97%)" in out
    assert not _DOUBLE_SIGN.search(out)


# ── print_tier_table score-delta table ───────────────────────────────────────


def test_score_delta_formatting_for_positive_negative_and_zero(capsys):
    tier = _tier_result(
        "Haiku — criteria_mapper",
        {"criteria_mapper": "claude-haiku-4-5-20251001"},
        pass_counts={"determination": 11, "routing": 11, "criterion_evidence": 8, "citation_validity": 13},
        scoreable={"determination": 13, "routing": 13, "criterion_evidence": 12, "citation_validity": 13},
        node_costs={"criteria_mapper": 0.0023, "evidence_extractor": 0.1001, "determination": 0.0706},
    )
    print_tier_table(BASELINE, [tier])
    out = capsys.readouterr().out
    assert "11/13 (+1)" in out   # determination: 10 -> 11
    assert "8/12 (-2)" in out    # criterion_evidence: 10 -> 8
    assert "13/13 |" in out      # citation_validity: 13 -> 13, no delta suffix

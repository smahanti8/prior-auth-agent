"""Model tiering analysis: cost vs accuracy across the golden eval set.

For each tier configuration (a node-to-model override map), runs the golden
set, collects S1-S4 scores and per-node cost, then cross-references against the
committed baseline to compute accuracy delta.

Outputs:
  - stdout Markdown tiering table (paste into README)
  - evals/results/tier_analysis_YYYYMMDD.json

Usage:
    python -m evals.tier_analysis --live                     # all three nodes
    python -m evals.tier_analysis --live --node criteria_mapper
    python -m evals.tier_analysis --live --tier haiku        # preset: all LLM nodes
    python -m evals.tier_analysis --live --tier sonnet

Requires ANTHROPIC_API_KEY (live API calls; tiering changes model outputs so
cassette replay cannot be used — the outputs may differ across models).

The "recommendation" sentence format is:
  "{node} can run on {model}, saving ${X} at 1M/month, with no measured
   accuracy loss (S3: N/N -> N/N)."
  OR
  "{node} costs ${X} less at 1M/month on {model}, at a {delta}-point drop in
   criterion evidence accuracy (S3: {baseline}/N -> {new}/N)."
This is the sentence that makes the analysis CPTO-grade.
"""

from __future__ import annotations

import json
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(REPO_ROOT / ".env")
except ImportError:
    pass

from evals.golden.schema import CaseSpec
from evals.golden.generator import generate_bundle, CASES_DIR
from evals.recorder import CallCapture, make_recording_wrapper
from prior_auth_agent import telemetry
from prior_auth_agent.telemetry import NodeTelemetry, rollup

RESULTS_DIR = REPO_ROOT / "evals" / "results"
BASELINE_PATH = REPO_ROOT / "evals" / "baseline.json"

LLM_NODES = ["criteria_mapper", "evidence_extractor", "determination"]

LLM_CASE_PREFIXES = [
    "case_001", "case_002", "case_003", "case_004",
    "case_006", "case_007", "case_008", "case_010",
    "case_011", "case_012", "case_013", "case_014", "case_015",
]

TIER_PRESETS: dict[str, str] = {
    "haiku":  "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-5",
    "opus":   "claude-opus-5",
}


# ── patching ──────────────────────────────────────────────────────────────────


@contextmanager
def _live_ctx_with_override(capture: CallCapture, node_overrides: dict[str, str]):
    """Run live with per-node model overrides.

    Sets _current_model_override contextvar inside each node's wrapper so that
    structured_call uses the override model instead of the global MODEL.
    node_overrides: {node_name: model_id}
    """
    from prior_auth_agent import telemetry as _tel

    def make_override_wrapper(node_name: str, model_id: str):
        def wrapper(system, user_content, schema, max_tokens=16000):
            tok = _tel._current_model_override.set(model_id)
            try:
                return make_recording_wrapper(capture)(system, user_content, schema, max_tokens)
            finally:
                _tel._current_model_override.reset(tok)
        return wrapper

    wrappers = {
        node: make_override_wrapper(node, model)
        for node, model in node_overrides.items()
    }
    # Nodes not in overrides use the recording wrapper with default model
    default_wrapper = make_recording_wrapper(capture)

    with (
        patch(
            "prior_auth_agent.nodes.criteria_mapper.structured_call",
            wrappers.get("criteria_mapper", default_wrapper),
        ),
        patch(
            "prior_auth_agent.nodes.evidence_extractor.structured_call",
            wrappers.get("evidence_extractor", default_wrapper),
        ),
        patch(
            "prior_auth_agent.nodes.determination.structured_call",
            wrappers.get("determination", default_wrapper),
        ),
    ):
        yield


# ── scoring (re-uses run.py's dimension scorers) ──────────────────────────────


def _score_case(spec: CaseSpec, state: dict, bundle: dict) -> dict:
    """Return pass/fail for each scoring dimension."""
    from evals.run import (
        _score_determination,
        _score_routing,
        _score_criterion_evidence,
        _score_citation_validity,
    )
    return {
        "determination":      _score_determination(spec, state),
        "routing":            _score_routing(spec, state),
        "criterion_evidence": _score_criterion_evidence(spec, state, bundle),
        "citation_validity":  _score_citation_validity(spec, state, bundle),
    }


# ── per-tier runner ───────────────────────────────────────────────────────────


def run_tier(
    graph,
    specs: list[CaseSpec],
    node_overrides: dict[str, str],
    tier_label: str,
) -> dict:
    """Run all LLM cases with the given node_overrides. Return aggregated results."""

    all_tel: list[list[NodeTelemetry]] = []
    scores_by_dim: dict[str, list[str]] = {
        "determination": [], "routing": [],
        "criterion_evidence": [], "citation_validity": [],
    }

    for spec in specs:
        bundle = generate_bundle(spec)
        telemetry.reset()
        capture = CallCapture()

        with _live_ctx_with_override(capture, node_overrides):
            state = graph.invoke({"fhir_bundle": bundle, "cpt_code": spec.cpt})

        case_tel = telemetry.drain()
        all_tel.append(case_tel)

        dim_scores = _score_case(spec, state, bundle)
        for dim, result in dim_scores.items():
            scores_by_dim[dim].append(result["result"])

        print(f"    {spec.id}: det={dim_scores['determination']['result']} "
              f"crit={dim_scores['criterion_evidence']['result']}", file=sys.stderr)

    cost_report = rollup([r for case in all_tel for r in case])

    pass_counts = {
        dim: results.count("pass")
        for dim, results in scores_by_dim.items()
    }
    scoreable = {
        dim: sum(1 for r in results if r != "skip")
        for dim, results in scores_by_dim.items()
    }

    return {
        "tier_label":   tier_label,
        "node_overrides": node_overrides,
        "case_count":   len(specs),
        "pass_counts":  pass_counts,
        "scoreable":    scoreable,
        "cost_report":  cost_report,
    }


# ── recommendation sentence ───────────────────────────────────────────────────


def _recommendation(
    node: str,
    baseline_cost_1m: float,
    tier_cost_1m: float,
    tier_model: str,
    baseline_s3: int,
    tier_s3: int,
    scoreable: int,
) -> str:
    saving = baseline_cost_1m - tier_cost_1m
    delta = tier_s3 - baseline_s3
    short_model = tier_model.split("claude-")[1] if "claude-" in tier_model else tier_model

    if delta == 0:
        return (
            f"{node} **can** drop to {short_model}, saving "
            f"${saving:,.0f}/month at 1M det, "
            f"with no measured accuracy loss "
            f"(S3: {baseline_s3}/{scoreable} → {tier_s3}/{scoreable})."
        )
    elif delta > 0:
        return (
            f"{node} **improves** on {short_model} (+{delta} S3 cases), "
            f"saving ${saving:,.0f}/month at 1M det "
            f"(S3: {baseline_s3}/{scoreable} → {tier_s3}/{scoreable})."
        )
    else:
        return (
            f"{node} costs ${saving:,.0f} less/month at 1M det on {short_model}, "
            f"at a {abs(delta)}-point drop in criterion evidence accuracy "
            f"(S3: {baseline_s3}/{scoreable} → {tier_s3}/{scoreable})."
        )


# ── Markdown output ───────────────────────────────────────────────────────────


def print_tier_table(
    baseline_result: dict,
    tier_results: list[dict],
) -> None:
    dims = ["determination", "routing", "criterion_evidence", "citation_validity"]
    dim_labels = {"determination": "S1", "routing": "S2",
                  "criterion_evidence": "S3", "citation_validity": "S4"}

    print()
    print("### Model tiering — where cheaper is possible")
    print()
    print("*Scores: pass / scoreable cases across the 13 LLM golden-set cases.*")
    print()

    # Score comparison table
    header = "| Dimension | Baseline (Opus) |"
    sep    = "|-----------|----------------|"
    for tr in tier_results:
        label = tr["tier_label"]
        header += f" {label} |"
        sep    += "---------|"
    print(header)
    print(sep)

    for dim in dims:
        b_pass = baseline_result["pass_counts"][dim]
        b_score = baseline_result["scoreable"][dim]
        row = f"| {dim_labels[dim]} {dim.replace('_', ' ').title()} | {b_pass}/{b_score} |"
        for tr in tier_results:
            t_pass = tr["pass_counts"][dim]
            t_score = tr["scoreable"][dim]
            delta = t_pass - b_pass
            delta_str = f" (+{delta})" if delta > 0 else (f" ({delta})" if delta < 0 else "")
            row += f" {t_pass}/{t_score}{delta_str} |"
        print(row)

    # Cost comparison table
    print()
    print("**Cost comparison (average per determination and projected monthly)**")
    print()
    print("| Node | Baseline / det | Baseline / 1M mo |", end="")
    for tr in tier_results:
        print(f" {tr['tier_label']} / 1M mo |", end="")
    print()
    print("|------|---------------|-----------------|", end="")
    for _ in tier_results:
        print("---------|", end="")
    print()

    for node in LLM_NODES:
        baseline_node = next(
            (n for n in baseline_result["cost_report"]["nodes"] if n["node_name"] == node),
            None,
        )
        if baseline_node is None:
            continue
        b_cost_det = baseline_node["avg_cost_usd"]
        b_proj_1m = b_cost_det * 1_000_000
        row = (
            f"| {node} | ${b_cost_det:.4f} | ${b_proj_1m:,.0f} |"
        )
        for tr in tier_results:
            tier_node = next(
                (n for n in tr["cost_report"]["nodes"] if n["node_name"] == node),
                None,
            )
            if tier_node is None:
                row += " n/a |"
                continue
            t_proj_1m = tier_node["avg_cost_usd"] * 1_000_000
            saving_pct = ((b_proj_1m - t_proj_1m) / b_proj_1m * 100) if b_proj_1m else 0
            row += f" ${t_proj_1m:,.0f} (-{saving_pct:.0f}%) |"
        print(row)

    # Recommendation sentences
    print()
    print("**Recommendations (S3 = criterion evidence accuracy)**")
    print()
    for tr in tier_results:
        overrides = tr.get("node_overrides", {})
        for node, tier_model in overrides.items():
            baseline_node = next(
                (n for n in baseline_result["cost_report"]["nodes"] if n["node_name"] == node),
                None,
            )
            tier_node = next(
                (n for n in tr["cost_report"]["nodes"] if n["node_name"] == node),
                None,
            )
            if baseline_node is None or tier_node is None:
                continue
            rec = _recommendation(
                node=node,
                baseline_cost_1m=baseline_node["avg_cost_usd"] * 1_000_000,
                tier_cost_1m=tier_node["avg_cost_usd"] * 1_000_000,
                tier_model=tier_model,
                baseline_s3=baseline_result["pass_counts"]["criterion_evidence"],
                tier_s3=tr["pass_counts"]["criterion_evidence"],
                scoreable=tr["scoreable"]["criterion_evidence"],
            )
            print(f"- {rec}")
    print()


# ── main ──────────────────────────────────────────────────────────────────────


def main(argv: list[str]) -> int:
    if "--live" not in argv:
        print(
            "tier_analysis always requires live API calls — "
            "model outputs differ across tiers so cassettes cannot be used.\n"
            "Add --live to proceed.",
            file=sys.stderr,
        )
        return 1

    has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    if not has_key:
        print("ANTHROPIC_API_KEY not set", file=sys.stderr)
        return 1

    # Parse --node or --tier arguments
    node_filter: Optional[str] = None
    tier_preset: Optional[str] = None
    i = 0
    while i < len(argv):
        if argv[i] == "--node" and i + 1 < len(argv):
            node_filter = argv[i + 1]
            i += 2
            continue
        if argv[i] == "--tier" and i + 1 < len(argv):
            tier_preset = argv[i + 1]
            i += 2
            continue
        i += 1

    # Build tier configurations to run
    if tier_preset:
        tier_model = TIER_PRESETS.get(tier_preset)
        if not tier_model:
            print(f"Unknown tier preset {tier_preset!r}. Choose from: {list(TIER_PRESETS)}", file=sys.stderr)
            return 1
        nodes_to_tier = [node_filter] if node_filter else LLM_NODES
        tier_configs = [
            {
                "label": f"{tier_preset.title()} — {node}",
                "overrides": {node: tier_model},
            }
            for node in nodes_to_tier
        ]
    elif node_filter:
        # Run all three tiers for one node
        tier_configs = [
            {
                "label": f"{tier_name.title()} — {node_filter}",
                "overrides": {node_filter: tier_model},
            }
            for tier_name, tier_model in TIER_PRESETS.items()
            if tier_name != "opus"  # skip opus baseline re-run
        ]
    else:
        # Default: haiku tier for all three nodes
        tier_configs = [
            {
                "label": f"Haiku — {node}",
                "overrides": {node: TIER_PRESETS["haiku"]},
            }
            for node in LLM_NODES
        ]

    specs = [
        s for s in CaseSpec.load_all(CASES_DIR)
        if any(s.id.startswith(p) for p in LLM_CASE_PREFIXES)
    ]

    from prior_auth_agent.graph import build_graph
    from prior_auth_agent.config import MODEL
    graph = build_graph()

    # Run baseline (all nodes on default model)
    print(f"\nRunning baseline ({MODEL})...", file=sys.stderr)
    baseline_result = run_tier(graph, specs, {}, "Baseline")

    # Run each tier config
    tier_results = []
    for cfg in tier_configs:
        print(f"\nRunning {cfg['label']}...", file=sys.stderr)
        result = run_tier(graph, specs, cfg["overrides"], cfg["label"])
        tier_results.append(result)

    # Output
    print_tier_table(baseline_result, tier_results)

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out = RESULTS_DIR / f"tier_analysis_{ts}.json"
    out.write_text(
        json.dumps(
            {"baseline": baseline_result, "tiers": tier_results},
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\n  Artifact: {out.relative_to(REPO_ROOT)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

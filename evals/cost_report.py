"""Cost-per-determination rollup with 1K / 100K / 1M monthly projections.

Runs all 13 LLM golden-set cases in replay mode (cassettes — zero API cost)
and averages per-node token counts, latency, and cost across cases.

Outputs:
  - evals/results/cost_report_YYYYMMDD.json  (machine-readable)
  - stdout Markdown table                     (paste into README)

Usage:
    python -m evals.cost_report               # replay mode (requires cassettes)
    python -m evals.cost_report --live        # live mode (records cassettes)
    python -m evals.cost_report --no-color    # plain text, no ANSI codes

The "source" field in the JSON output marks whether numbers came from cassette
replay (recorded token counts) or a live API run.
"""

from __future__ import annotations

import json
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
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
from evals.fixtures import Cassette, StalenessError
from evals.recorder import CallCapture, make_recording_wrapper, save_cassette
from prior_auth_agent import telemetry
from prior_auth_agent.telemetry import NodeTelemetry, rollup

RESULTS_DIR = REPO_ROOT / "evals" / "results"

# Cases that reach the LLM (skip intake-fail 005 and eligibility-fail 009)
LLM_CASE_PREFIXES = [
    "case_001", "case_002", "case_003", "case_004",
    "case_006", "case_007", "case_008", "case_010",
    "case_011", "case_012", "case_013", "case_014", "case_015",
]


# ── pipeline patching ─────────────────────────────────────────────────────────


@contextmanager
def _replay_ctx(cassette: Cassette):
    with (
        patch("prior_auth_agent.nodes.criteria_mapper.structured_call",    cassette.replay_structured_call),
        patch("prior_auth_agent.nodes.evidence_extractor.structured_call", cassette.replay_structured_call),
        patch("prior_auth_agent.nodes.determination.structured_call",      cassette.replay_structured_call),
        patch("prior_auth_agent.nodes.policy_rag.get_collection",          cassette.make_collection_fn()),
    ):
        yield


@contextmanager
def _record_ctx(capture: CallCapture):
    wrapper = make_recording_wrapper(capture)
    with (
        patch("prior_auth_agent.nodes.criteria_mapper.structured_call",    wrapper),
        patch("prior_auth_agent.nodes.evidence_extractor.structured_call", wrapper),
        patch("prior_auth_agent.nodes.determination.structured_call",      wrapper),
    ):
        yield


# ── per-case runner ───────────────────────────────────────────────────────────


def _run_case(spec: CaseSpec, graph, *, live: bool) -> list[NodeTelemetry]:
    bundle = generate_bundle(spec)
    telemetry.reset()

    if live:
        capture = CallCapture()
        with _record_ctx(capture):
            state = graph.invoke({"fhir_bundle": bundle, "cpt_code": spec.cpt})
        save_cassette(spec.id, capture, state.get("policy_chunks", []))
    else:
        cassette = Cassette.load(spec.id)
        cassette.check_staleness()
        with _replay_ctx(cassette):
            graph.invoke({"fhir_bundle": bundle, "cpt_code": spec.cpt})

    return telemetry.drain()


# ── aggregation ───────────────────────────────────────────────────────────────


def aggregate(all_records: list[list[NodeTelemetry]]) -> dict:
    """Average per-node stats across all cases and compute projections."""
    combined: list[NodeTelemetry] = [r for case in all_records for r in case]
    return rollup(combined)


# ── Markdown table ─────────────────────────────────────────────────────────────


def _fmt_tokens(avg_in: int | None, avg_out: int | None) -> str:
    if avg_in is None:
        return "—"
    return f"{avg_in:,} / {avg_out:,}"


def _fmt_cost(c: float) -> str:
    if c == 0.0:
        return "$0.0000"
    if c < 0.001:
        return f"${c:.6f}"
    return f"${c:.4f}"


def _fmt_latency(ms: float) -> str:
    if ms < 1:
        return "<1 ms"
    if ms < 1000:
        return f"~{ms:.0f} ms"
    return f"~{ms/1000:.1f} s"


def _fmt_proj(v: float) -> str:
    if v < 1:
        return f"${v:.2f}"
    if v < 1000:
        return f"${v:.0f}"
    return f"${v:,.0f}"


def print_markdown_table(report: dict, source: str, case_count: int, model: str) -> None:
    nodes = report["nodes"]
    total = report["total_cost_usd"]
    proj = report["projections"]

    header_line = (
        f"*Model: {model} — averaged across {case_count} golden-set cases — "
        f"source: {source}*"
    )
    print()
    print("## Unit Economics")
    print()
    print(header_line)
    print()
    print("| Node | Type | Tokens (in / out) | Latency p50 | Cost / det | Note |")
    print("|------|------|-------------------|-------------|------------|------|")

    notes = {
        "intake":             "FHIR validation",
        "eligibility":        "Coverage status check",
        "policy_rag":         "ChromaDB retrieval",
        "criteria_mapper":    "Policy → criteria list",
        "evidence_extractor": "Per-criterion evidence (32k budget)",
        "citation_gate":      "Hard citation existence check",
        "determination":      "Approve / insufficient_evidence",
        "confidence_gate":    "Route: auto vs HITL",
        "auto_decision":      "Write final decision",
        "hitl_enqueue":       "Enqueue for human review",
    }

    for n in nodes:
        name = n["node_name"]
        tok_str = _fmt_tokens(n.get("avg_input_tokens"), n.get("avg_output_tokens"))
        lat_str = _fmt_latency(n["avg_latency_ms"])
        cost_str = _fmt_cost(n["avg_cost_usd"])
        note = notes.get(name, "")
        bold = "**" if n["node_type"] == "llm" else ""
        print(
            f"| {bold}{name}{bold} | {n['node_type']} | {tok_str} | "
            f"{lat_str} | {bold}{cost_str}{bold} | {note} |"
        )

    print(
        f"| **TOTAL** | | | {_fmt_latency(report['total_latency_ms'])} | "
        f"**{_fmt_cost(total)}** | |"
    )

    print()
    print("### Monthly projection")
    print()
    print("| Volume | Monthly cost |")
    print("|--------|-------------|")
    print(f"| 1,000 determinations / month | {_fmt_proj(proj['1k_per_month'])} |")
    print(f"| 100,000 determinations / month | {_fmt_proj(proj['100k_per_month'])} |")
    print(f"| 1,000,000 determinations / month | {_fmt_proj(proj['1m_per_month'])} |")
    print()
    print(
        "*To update: `python -m evals.run --live && python -m evals.cost_report`*"
    )
    print()


# ── JSON artifact ──────────────────────────────────────────────────────────────


def write_artifact(report: dict, source: str, case_count: int, model: str) -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out = RESULTS_DIR / f"cost_report_{ts}.json"
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model":        model,
        "case_count":   case_count,
        "source":       source,
        "report":       report,
    }
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return out


# ── main ──────────────────────────────────────────────────────────────────────


def main(argv: list[str]) -> int:
    live = "--live" in argv
    has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))

    if live and not has_key:
        print("--live requires ANTHROPIC_API_KEY", file=sys.stderr)
        return 1

    source = "live_run" if live else "cassette_replay"
    print(f"Running {len(LLM_CASE_PREFIXES)} LLM cases [{source}]...", file=sys.stderr)

    from prior_auth_agent.graph import build_graph
    from prior_auth_agent.config import MODEL
    graph = build_graph()

    specs_by_id = {s.id: s for s in CaseSpec.load_all(CASES_DIR)}
    all_records: list[list[NodeTelemetry]] = []
    skipped = 0

    for prefix in LLM_CASE_PREFIXES:
        spec = next((s for s in specs_by_id.values() if s.id.startswith(prefix)), None)
        if spec is None:
            print(f"  SKIP {prefix}: spec not found", file=sys.stderr)
            skipped += 1
            continue

        if not live and not Cassette.exists(spec.id):
            print(
                f"  SKIP {prefix}: no cassette "
                f"(run: python -m evals.run --live {spec.id})",
                file=sys.stderr,
            )
            skipped += 1
            continue

        try:
            records = _run_case(spec, graph, live=live)
            all_records.append(records)
            print(f"  OK   {spec.id}", file=sys.stderr)
        except StalenessError as exc:
            print(f"  STALE {prefix}: {exc}", file=sys.stderr)
            skipped += 1
        except Exception as exc:
            print(f"  ERROR {prefix}: {type(exc).__name__}: {exc}", file=sys.stderr)
            skipped += 1

    if not all_records:
        print("No cases ran — no report generated.", file=sys.stderr)
        return 1

    ran = len(all_records)
    report = aggregate(all_records)
    artifact = write_artifact(report, source, ran, MODEL)
    print(f"\n  Artifact: {artifact.relative_to(REPO_ROOT)}", file=sys.stderr)
    if skipped:
        print(f"  ({skipped} cases skipped)", file=sys.stderr)

    print_markdown_table(report, source, ran, MODEL)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

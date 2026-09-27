"""Retrieval evaluation runner.

Requires a populated ChromaDB index. Run ingest first:
  python -m prior_auth_agent.vectorstore.ingest

Usage:
  python -m evals.retrieval.runner                     # print report, exit 0
  python -m evals.retrieval.runner --baseline-check    # fail if below baseline
  python -m evals.retrieval.runner --update-baseline   # record current scores as baseline
  python -m evals.retrieval.runner --json              # write JSON to evals/retrieval/results/
  python -m evals.retrieval.runner --no-chunking-analysis  # skip corpus scan (faster)

The argument this harness makes:
  A determination pipeline that evaluates only its final output is measuring
  the wrong thing. If retrieval silently surfaces the wrong policy, the model
  can reason perfectly and the answer is still wrong. A perfect determination
  score on wrong passages is a false positive. Retrieval evaluation is a
  separate failure mode that determination evaluation cannot detect.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from prior_auth_agent.nodes.policy_rag import TOP_K
from prior_auth_agent.vectorstore.store import get_collection

from .chunking_analysis import classify_failure
from .metrics import CaseResult, PassageResult, SuiteResult

LABELED_DIR = Path(__file__).parent / "labeled"
RESULTS_DIR = Path(__file__).parent / "results"
BASELINE_FILE = Path(__file__).parent / "baseline.json"


def _load_labeled_cases() -> list[dict]:
    cases = []
    for path in sorted(LABELED_DIR.glob("*.yaml")):
        cases.append(yaml.safe_load(path.read_text(encoding="utf-8")))
    return cases


def _run_case(case: dict, collection: Any, run_chunking_analysis: bool) -> CaseResult:
    cpt = case["cpt_code"]
    # Use the exact same query format as policy_rag.py so the eval measures
    # real retrieval, not a differently-phrased surrogate.
    query = case.get(
        "query",
        f"Prior authorization medical necessity criteria for CPT {cpt}",
    )
    k = TOP_K
    known_gap = case.get("known_gap", False)

    results = collection.query(
        query_texts=[query],
        n_results=k,
        include=["documents", "metadatas", "distances"],
    )
    chunks = [
        {
            "text": doc,
            "source": meta.get("source", "unknown"),
            "distance": dist,
            "rank": i + 1,
        }
        for i, (doc, meta, dist) in enumerate(
            zip(
                results["documents"][0],
                results["metadatas"][0],
                results["distances"][0],
            )
        )
    ]

    passage_results = []
    for ep in case.get("expected_passages", []):
        fragment = ep["text_fragment"]

        found_at_rank = None
        for chunk in chunks:
            if fragment in chunk["text"]:
                found_at_rank = chunk["rank"]
                break

        found_in_corpus = None
        chunking_failure = None
        labeling_error = None

        if found_at_rank is None and run_chunking_analysis:
            analysis = classify_failure(fragment, ep["source"], collection)
            found_in_corpus = analysis["in_any_chunk"]
            chunking_failure = analysis["chunking_failure"]
            labeling_error = analysis["labeling_error"]

        passage_results.append(
            PassageResult(
                passage_id=ep["passage_id"],
                text_fragment=fragment,
                source=ep["source"],
                criterion_id=ep.get("criterion_id"),
                required=ep.get("required", True),
                found_at_rank=found_at_rank,
                found_in_corpus=found_in_corpus,
                chunking_failure=chunking_failure,
                labeling_error=labeling_error,
            )
        )

    return CaseResult(
        case_id=case["case_id"],
        cpt_code=cpt,
        query=query,
        k=k,
        known_gap=known_gap,
        retrieved_chunks=chunks,
        passage_results=passage_results,
        expected_wrong_source=case.get("expected_wrong_source"),
    )


def _fmt(value: float) -> str:
    return f"{value:.3f}"


def _print_report(suite: SuiteResult) -> None:
    k = suite.k
    scored = suite.scored_cases
    gaps = suite.gap_cases

    print(f"\n{'=' * 62}")
    print(f"  Retrieval Evaluation Report  (TOP_K = {k})")
    print(f"  {len(scored)} scored cases  |  {len(gaps)} known-gap cases (excluded from metrics)")
    print(f"{'=' * 62}")
    if scored:
        print(f"  Recall@1          {_fmt(suite.mean_recall_at_1)}")
        print(f"  Recall@3          {_fmt(suite.mean_recall_at_3)}")
        print(f"  Recall@{k}          {_fmt(suite.mean_recall_at_k)}")
        print(f"  MRR               {_fmt(suite.mean_mrr)}")
        print(f"  Rank inversion    {_fmt(suite.rank_inversion_rate)}  (fraction of cases with irrelevant chunk above correct)")
        print(f"  Chunking failures {_fmt(suite.chunking_failure_rate)}  (fraction of misses caused by boundary splits)")
    print(f"{'=' * 62}\n")

    for case in suite.case_results:
        tag = "[GAP]" if case.known_gap else f"recall@{k}={_fmt(case.recall_at(k))}"
        inv = "  ⚠ rank-inversion" if (not case.known_gap and case.has_rank_inversion) else ""
        print(f"  {case.case_id}  CPT {case.cpt_code}  {tag}  mrr={_fmt(case.mrr)}{inv}")

        if case.known_gap and case.expected_wrong_source:
            top_src = case.retrieved_chunks[0]["source"] if case.retrieved_chunks else "none"
            confirmed = "✓ confirmed" if case.wrong_source_confirmed else "✗ unexpected"
            print(f"    wrong-policy retrieved: {top_src}  ({confirmed})")

        for p in case.passage_results:
            if p.found_at_rank is not None:
                status = f"rank {p.found_at_rank}"
            else:
                status = "NOT FOUND"
                if p.chunking_failure:
                    status += "  [chunking failure — fragment spans chunk boundary]"
                elif p.labeling_error:
                    status += "  [labeling error — fragment absent from policy file]"
                elif p.found_in_corpus is True:
                    status += "  [ranking failure — exists in corpus but not retrieved]"
                elif p.found_in_corpus is False:
                    status += "  [not in any chunk — see chunking analysis]"
            req = "required" if p.required else "optional"
            crit = f"  {p.criterion_id}" if p.criterion_id else ""
            print(f"    {p.passage_id}  ({req}){crit}  {status}")
            if p.found_at_rank is None and not case.known_gap:
                preview = p.text_fragment[:72]
                if len(p.text_fragment) > 72:
                    preview += "…"
                print(f"      \"{preview}\"")
        print()

    if gaps:
        print("  Known gaps (policies not yet in corpus — not counted in metrics):")
        for case in gaps:
            print(f"    {case.case_id}  CPT {case.cpt_code}  — {case.cpt_code} policy missing from corpus")
        print()


def _to_json(suite: SuiteResult) -> dict:
    k = suite.k
    return {
        "k": k,
        "recall_at_1": round(suite.mean_recall_at_1, 4),
        "recall_at_3": round(suite.mean_recall_at_3, 4),
        f"recall_at_{k}": round(suite.mean_recall_at_k, 4),
        "mrr": round(suite.mean_mrr, 4),
        "rank_inversion_rate": round(suite.rank_inversion_rate, 4),
        "chunking_failure_rate": round(suite.chunking_failure_rate, 4),
        "scored_cases": len(suite.scored_cases),
        "known_gap_cases": len(suite.gap_cases),
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Retrieval evaluation harness for the prior-auth-agent policy corpus"
    )
    parser.add_argument(
        "--baseline-check",
        action="store_true",
        help="Exit non-zero if recall@k or MRR is below baseline thresholds",
    )
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="Write current scores to baseline.json",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Write JSON result to evals/retrieval/results/<timestamp>.json",
    )
    parser.add_argument(
        "--no-chunking-analysis",
        action="store_true",
        help="Skip per-chunk corpus scan for failure diagnosis (faster but less informative)",
    )
    args = parser.parse_args()

    cases = _load_labeled_cases()
    if not cases:
        print("No labeled cases found in evals/retrieval/labeled/. Nothing to evaluate.")
        sys.exit(0)

    collection = get_collection()
    run_analysis = not args.no_chunking_analysis
    case_results = [_run_case(c, collection, run_analysis) for c in cases]
    suite = SuiteResult(k=TOP_K, case_results=case_results)

    _print_report(suite)

    if args.json or args.update_baseline:
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    if args.json:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out_path = RESULTS_DIR / f"retrieval_{ts}.json"
        out_path.write_text(json.dumps(_to_json(suite), indent=2), encoding="utf-8")
        print(f"Results written to {out_path}")

    if args.update_baseline:
        baseline = {
            f"min_recall_at_{suite.k}": round(suite.mean_recall_at_k, 3),
            "min_mrr": round(suite.mean_mrr, 3),
        }
        BASELINE_FILE.write_text(json.dumps(baseline, indent=2), encoding="utf-8")
        print(f"Baseline updated: {baseline}")
        sys.exit(0)

    if args.baseline_check:
        if not BASELINE_FILE.exists():
            print(
                "No baseline.json found. Run with --update-baseline after a satisfactory eval run.",
                file=sys.stderr,
            )
            sys.exit(1)

        baseline = json.loads(BASELINE_FILE.read_text(encoding="utf-8"))
        failures = []
        recall_key = f"min_recall_at_{suite.k}"
        if recall_key in baseline and suite.mean_recall_at_k < baseline[recall_key]:
            failures.append(
                f"  recall@{suite.k}  {suite.mean_recall_at_k:.3f}  <  baseline {baseline[recall_key]}"
            )
        if "min_mrr" in baseline and suite.mean_mrr < baseline["min_mrr"]:
            failures.append(
                f"  MRR           {suite.mean_mrr:.3f}  <  baseline {baseline['min_mrr']}"
            )
        if failures:
            print("\nRetrieval eval FAILED:", file=sys.stderr)
            for msg in failures:
                print(msg, file=sys.stderr)
            sys.exit(1)

        print(
            f"Retrieval eval PASSED  "
            f"recall@{suite.k}={suite.mean_recall_at_k:.3f}  "
            f"MRR={suite.mean_mrr:.3f}"
        )


if __name__ == "__main__":
    main()

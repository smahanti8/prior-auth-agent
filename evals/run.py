"""Golden eval runner — scores the prior-auth pipeline across all 15 cases.

Scores 4 dimensions per case:
  S1  determination  — pipeline decision matches expected decision
  S2  routing        — pipeline route matches expected route
  S3  criterion_evidence — each expected criterion matched to pipeline evidence
                           and status verified; catches "right answer wrong reason"
  S4  citation_validity  — surviving citations in met evidence resolve in bundle

Usage:
    python -m evals.run                       # replay mode (requires cassettes)
    python -m evals.run --live                # live mode: call real API, save cassettes
    python -m evals.run --live case_007       # re-record one case only
    python -m evals.run --no-llm              # deterministic cases only (no cassettes needed)
    python -m evals.run --baseline-check      # fail if scores drop below baseline.json
    python -m evals.run --update-baseline     # write new baseline.json from this run

Exit code: 0 if all ran cases pass all four dimensions (and baseline check passes), 1 otherwise.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from unittest.mock import patch

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
except ImportError:
    pass

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from evals.golden.schema import CaseSpec, CriterionGroundTruth
from evals.golden.generator import generate_bundle, CASES_DIR
from evals.fixtures import Cassette, StalenessError
from evals.recorder import CallCapture, make_recording_wrapper, save_cassette
from prior_auth_agent.validation import _build_ref_index

RESULTS_DIR = Path(__file__).resolve().parent / "results"
BASELINE_PATH = Path(__file__).resolve().parent / "baseline.json"

_GREEN = "\033[32m"
_RED = "\033[31m"
_YELLOW = "\033[33m"
_RESET = "\033[0m"
_PASS = f"{_GREEN}PASS{_RESET}"
_FAIL = f"{_RED}FAIL{_RESET}"
_SKIP = f"{_YELLOW}SKIP{_RESET}"


# ── pipeline patching contexts ─────────────────────────────────────────────────


@contextmanager
def _replay_context(cassette: Cassette):
    """Patch all three node-level structured_call bindings + policy_rag's get_collection."""
    with (
        patch("prior_auth_agent.nodes.criteria_mapper.structured_call", cassette.replay_structured_call),
        patch("prior_auth_agent.nodes.evidence_extractor.structured_call", cassette.replay_structured_call),
        patch("prior_auth_agent.nodes.determination.structured_call", cassette.replay_structured_call),
        patch("prior_auth_agent.nodes.policy_rag.get_collection", cassette.make_collection_fn()),
    ):
        yield


@contextmanager
def _record_context(capture: CallCapture):
    """Patch node-level structured_call bindings with the recording wrapper.
    policy_rag runs normally; policy_chunks are captured from the final state."""
    wrapper = make_recording_wrapper(capture)
    with (
        patch("prior_auth_agent.nodes.criteria_mapper.structured_call", wrapper),
        patch("prior_auth_agent.nodes.evidence_extractor.structured_call", wrapper),
        patch("prior_auth_agent.nodes.determination.structured_call", wrapper),
    ):
        yield


# ── criterion matching ─────────────────────────────────────────────────────────


def _match_by_citations(
    criterion: CriterionGroundTruth,
    pipeline_evidence: list[dict],
) -> Optional[dict]:
    if not criterion.expected_citations:
        return None
    for item in pipeline_evidence:
        surviving = item.get("citations", [])
        summary = item.get("summary", "")
        if any(c in surviving or c in summary for c in criterion.expected_citations):
            return item
    return None


def _match_criterion(
    criterion: CriterionGroundTruth,
    pipeline_evidence: list[dict],
) -> Optional[dict]:
    """
    Find the best-fit pipeline evidence item for a single fixture criterion.

    For met and not_met criteria, expected_citations is non-empty — matched by
    citation overlap.

    For insufficient criteria (expected_citations == []):
    TODO: implement matching for purely-silent insufficient criteria.
    Options:
      text_overlap:  tokenize criterion.text and each evidence item's text;
                     return the highest-overlap item.
      positional:    match by index (fixture c1 -> evidence[0]).

    # Example text-overlap implementation:
    # criterion_words = set(criterion.text.lower().split())
    # best, best_score = None, 0
    # for item in pipeline_evidence:
    #     item_words = set(item.get("summary", "").lower().split())
    #     score = len(criterion_words & item_words)
    #     if score > best_score:
    #         best, best_score = item, score
    # return best if best_score > 0 else None
    """
    if criterion.expected_citations:
        return _match_by_citations(criterion, pipeline_evidence)
    return None


# ── S1: determination accuracy ─────────────────────────────────────────────────


def _score_determination(spec: CaseSpec, state: dict) -> dict:
    outcome = spec.ground_truth.outcome
    stage = outcome.stage

    if stage == "intake":
        valid = state.get("valid", True)
        errors = state.get("validation_errors", [])
        passed = not valid
        if passed and outcome.validation_error_contains:
            passed = any(outcome.validation_error_contains in e for e in errors)
        return {
            "result": "pass" if passed else "fail",
            "expected": f"intake_rejected({outcome.validation_error_contains})",
            "actual": f"valid={valid}, errors={errors}",
        }

    if stage == "eligibility":
        eligible = state.get("eligible", True)
        notes = state.get("eligibility_notes", "")
        passed = not eligible
        if passed and outcome.validation_error_contains:
            passed = outcome.validation_error_contains in notes
        no_det = "determination" not in state
        return {
            "result": "pass" if (passed and no_det) else "fail",
            "expected": f"eligibility_rejected({outcome.validation_error_contains})",
            "actual": f"eligible={eligible}, notes={notes!r}",
        }

    det = state.get("determination", {})
    actual_decision = det.get("decision", "")
    expected_decision = outcome.decision or ""
    passed = actual_decision == expected_decision
    return {
        "result": "pass" if passed else "fail",
        "expected": expected_decision,
        "actual": actual_decision,
        "confidence": det.get("confidence"),
    }


# ── S2: routing accuracy ───────────────────────────────────────────────────────


def _score_routing(spec: CaseSpec, state: dict) -> dict:
    outcome = spec.ground_truth.outcome
    if outcome.stage in ("intake", "eligibility"):
        return {"result": "skip", "reason": f"no route at {outcome.stage} stage"}

    expected_route = outcome.route or ""
    actual_route = state.get("route", "")
    passed = actual_route == expected_route
    return {
        "result": "pass" if passed else "fail",
        "expected": expected_route,
        "actual": actual_route,
    }


# ── S3: criterion-level evidence accuracy ─────────────────────────────────────


def _score_criterion_evidence(spec: CaseSpec, state: dict, bundle: dict) -> dict:
    outcome = spec.ground_truth.outcome
    if outcome.stage in ("intake", "eligibility") or not spec.ground_truth.criteria:
        return {"result": "skip", "reason": "no criteria to score at this stage"}

    pipeline_evidence: list[dict] = state.get("evidence", [])
    details = []
    all_pass = True

    for crit in spec.ground_truth.criteria:
        matched = _match_criterion(crit, pipeline_evidence)

        if matched is None:
            if crit.expected_status == "insufficient" and not crit.expected_citations:
                det_decision = state.get("determination", {}).get("decision", "")
                crit_pass = det_decision == "insufficient_evidence"
                details.append({
                    "criterion_id": crit.id,
                    "expected_status": crit.expected_status,
                    "actual_status": "unmatched",
                    "match_method": "none",
                    "result": "pass" if crit_pass else "fail",
                    "note": "unmatched insufficient criterion; scored via final decision",
                })
            else:
                all_pass = False
                details.append({
                    "criterion_id": crit.id,
                    "expected_status": crit.expected_status,
                    "actual_status": "unmatched",
                    "match_method": "none",
                    "result": "fail",
                    "note": "no matching evidence item found in pipeline output",
                })
            continue

        actual_status = matched.get("status", "")
        crit_pass = actual_status == crit.expected_status

        surviving_citations: list[str] = []
        if crit.expected_status == "met" and crit.expected_citations:
            surviving = matched.get("citations", [])
            surviving_citations = [c for c in crit.expected_citations if c in surviving]
            if not surviving_citations:
                crit_pass = False

        if not crit_pass:
            all_pass = False

        entry: dict = {
            "criterion_id": crit.id,
            "expected_status": crit.expected_status,
            "actual_status": actual_status,
            "match_method": "citation",
            "result": "pass" if crit_pass else "fail",
        }
        if crit.expected_citations:
            entry["citations_found"] = surviving_citations
        if "CITATION STRIPPED" in matched.get("summary", ""):
            entry["strip_log_fired"] = True
        details.append(entry)

    matched_count = sum(1 for d in details if d["result"] == "pass")
    total = len(details)
    return {
        "result": "pass" if all_pass else "fail",
        "matched": matched_count,
        "total": total,
        "details": details,
    }


# ── S4: citation validity ──────────────────────────────────────────────────────


def _score_citation_validity(spec: CaseSpec, state: dict, bundle: dict) -> dict:
    outcome = spec.ground_truth.outcome
    if outcome.stage in ("intake", "eligibility"):
        return {"result": "skip", "reason": f"no citations at {outcome.stage} stage"}

    ref_index = _build_ref_index(bundle)
    pipeline_evidence: list[dict] = state.get("evidence", [])

    stripped_count = 0
    unresolved_count = 0
    problems = []

    for item in pipeline_evidence:
        if item.get("status") != "met":
            continue
        summary = item.get("summary", "")
        if "CITATION STRIPPED" in summary:
            stripped_count += 1
        for citation in item.get("citations", []):
            parts = citation.strip().split("/")
            if len(parts) != 2:
                unresolved_count += 1
                problems.append(
                    f"malformed citation {citation!r} on criterion {item.get('criterion_id')}"
                )
                continue
            rtype, rid = parts[0].strip(), parts[1].strip()
            if (rtype, rid) not in ref_index:
                unresolved_count += 1
                problems.append(
                    f"unresolved citation {citation!r} not in bundle "
                    f"(criterion {item.get('criterion_id')}) — "
                    f"resolve_citations may not have run"
                )

    result: dict = {
        "result": "pass" if unresolved_count == 0 else "fail",
        "stripped_count": stripped_count,
        "unresolved_count": unresolved_count,
    }
    if problems:
        result["problems"] = problems
    return result


# ── case runner ────────────────────────────────────────────────────────────────


def _is_deterministic(spec: CaseSpec) -> bool:
    return spec.ground_truth.outcome.stage in ("intake", "eligibility")


def run_case(spec: CaseSpec, graph, *, live: bool = False) -> dict:
    """Run one case.  Uses cassette replay unless live=True."""
    bundle = generate_bundle(spec)

    if _is_deterministic(spec):
        state = graph.invoke({"fhir_bundle": bundle, "cpt_code": spec.cpt})

    elif live:
        capture = CallCapture()
        with _record_context(capture):
            state = graph.invoke({"fhir_bundle": bundle, "cpt_code": spec.cpt})
        cassette_path = save_cassette(
            spec.id,
            capture,
            state.get("policy_chunks", []),
        )
        print(f"              recorded -> {cassette_path.relative_to(REPO_ROOT)}")

    else:
        cassette = Cassette.load(spec.id)
        cassette.check_staleness()
        with _replay_context(cassette):
            state = graph.invoke({"fhir_bundle": bundle, "cpt_code": spec.cpt})

    s1 = _score_determination(spec, state)
    s2 = _score_routing(spec, state)
    s3 = _score_criterion_evidence(spec, state, bundle)
    s4 = _score_citation_validity(spec, state, bundle)

    overall = all(s["result"] in ("pass", "skip") for s in (s1, s2, s3, s4))
    return {
        "case_id": spec.id,
        "title": spec.title,
        "overall": "pass" if overall else "fail",
        "scores": {
            "determination": s1,
            "routing": s2,
            "criterion_evidence": s3,
            "citation_validity": s4,
        },
        "state_summary": {
            "final_decision": state.get("final_decision"),
            "route": state.get("route"),
            "determination": state.get("determination"),
            "evidence_count": len(state.get("evidence", [])),
        },
    }


# ── console output ─────────────────────────────────────────────────────────────


def _fmt(score_result: str) -> str:
    if score_result == "pass":
        return _PASS
    if score_result == "fail":
        return _FAIL
    return _SKIP


def _print_case_row(result: dict) -> None:
    s = result["scores"]
    crit = s["criterion_evidence"]
    crit_str = (
        f"{_fmt(crit['result'])}({crit.get('matched', '-')}/{crit.get('total', '-')})"
        if crit["result"] != "skip"
        else _SKIP
    )
    strip_note = ""
    if crit.get("result") != "skip":
        fires = sum(1 for d in crit.get("details", []) if d.get("strip_log_fired"))
        if fires:
            strip_note = f" [{fires} strip]"

    cite = s["citation_validity"]
    cite_str = _fmt(cite["result"])
    if cite.get("stripped_count"):
        cite_str += f"({cite['stripped_count']} stripped)"

    icon = _GREEN + "OK" + _RESET if result["overall"] == "pass" else _RED + "XX" + _RESET
    print(
        f"  {icon} {result['case_id']:<10}  "
        f"det={_fmt(s['determination']['result'])}  "
        f"route={_fmt(s['routing']['result'])}  "
        f"crit={crit_str}{strip_note}  "
        f"cite={cite_str}"
    )
    for dim, score in s.items():
        if score["result"] == "fail":
            if dim == "criterion_evidence":
                for d in score.get("details", []):
                    if d["result"] == "fail":
                        print(
                            f"              {_RED}-> {dim} {d['criterion_id']}: "
                            f"expected {d['expected_status']!r}, "
                            f"got {d['actual_status']!r}{_RESET}"
                        )
            else:
                exp = score.get("expected", "?")
                act = score.get("actual", "?")
                print(f"              {_RED}-> {dim}: expected {exp!r}, got {act!r}{_RESET}")


def _print_summary(results: list[dict], skipped: int) -> None:
    ran = [r for r in results if r["overall"] != "skipped"]
    dims = ["determination", "routing", "criterion_evidence", "citation_validity"]
    print()
    print("  " + "-" * 70)
    for dim in dims:
        passed = sum(1 for r in ran if r["scores"][dim]["result"] == "pass")
        failed = sum(1 for r in ran if r["scores"][dim]["result"] == "fail")
        skips = sum(1 for r in ran if r["scores"][dim]["result"] == "skip")
        label = dim.upper().replace("_", " ")
        scoreable = len(ran) - skips
        fraction = f"{passed}/{scoreable}" if scoreable else "n/a"
        skip_note = f"  ({skips} skip)" if skips else ""
        bar = _GREEN + "#" * passed + _RESET + _RED + "." * failed + _RESET
        print(f"  {label:<25} {fraction:<6}  {bar}{skip_note}")
    overall_pass = sum(1 for r in ran if r["overall"] == "pass")
    print(f"\n  OVERALL  {overall_pass}/{len(ran)} cases fully passed", end="")
    if skipped:
        print(f"  ({skipped} skipped)")
    else:
        print()


# ── JSON artifact ──────────────────────────────────────────────────────────────


def _get_git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=REPO_ROOT, text=True, stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return "unknown"


def _write_artifact(results: list[dict], skipped_ids: list[str]) -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = RESULTS_DIR / f"run_{timestamp}.json"

    dims = ["determination", "routing", "criterion_evidence", "citation_validity"]
    ran = [r for r in results if r["overall"] != "skipped"]
    summary: dict = {
        "total_cases": 15,
        "ran": len(ran),
        "skipped": len(skipped_ids),
        "skipped_ids": skipped_ids,
        "scores": {},
    }
    for dim in dims:
        summary["scores"][dim] = {
            "pass": sum(1 for r in ran if r["scores"][dim]["result"] == "pass"),
            "fail": sum(1 for r in ran if r["scores"][dim]["result"] == "fail"),
            "skip": sum(1 for r in ran if r["scores"][dim]["result"] == "skip"),
        }

    payload = {
        "run_id": timestamp,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "git_sha": _get_git_sha(),
        "summary": summary,
        "cases": results,
    }
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return out_path


# ── baseline ───────────────────────────────────────────────────────────────────


def _write_baseline(results: list[dict]) -> Path:
    dims = ["determination", "routing", "criterion_evidence", "citation_validity"]
    ran = [r for r in results if r["overall"] != "skipped"]
    scores: dict = {}
    for dim in dims:
        passed = sum(1 for r in ran if r["scores"][dim]["result"] == "pass")
        scoreable = sum(1 for r in ran if r["scores"][dim]["result"] != "skip")
        scores[dim] = {"pass": passed, "scoreable": scoreable}

    payload = {
        "committed_at": datetime.now(timezone.utc).isoformat(),
        "git_sha": _get_git_sha(),
        "scores": scores,
    }
    BASELINE_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return BASELINE_PATH


def _check_baseline(results: list[dict]) -> tuple[bool, list[str]]:
    """Return (passed, regression_messages)."""
    if not BASELINE_PATH.exists():
        return False, [
            "No baseline.json committed. "
            "Run: python -m evals.run --live && python -m evals.run --update-baseline"
        ]

    baseline = json.loads(BASELINE_PATH.read_text())
    dims = ["determination", "routing", "criterion_evidence", "citation_validity"]
    ran = [r for r in results if r["overall"] != "skipped"]

    regressions = []
    for dim in dims:
        current_pass = sum(1 for r in ran if r["scores"][dim]["result"] == "pass")
        baseline_pass = baseline.get("scores", {}).get(dim, {}).get("pass", 0)
        if current_pass < baseline_pass:
            regressions.append(
                f"{dim}: was {baseline_pass}, now {current_pass} "
                f"(dropped {baseline_pass - current_pass})"
            )

    return len(regressions) == 0, regressions


# ── main ───────────────────────────────────────────────────────────────────────


def main(argv: list[str]) -> int:
    live = "--live" in argv
    no_llm = "--no-llm" in argv
    do_baseline_check = "--baseline-check" in argv
    do_update_baseline = "--update-baseline" in argv
    prefixes = [a for a in argv if not a.startswith("--")]

    specs = CaseSpec.load_all(CASES_DIR)
    if prefixes:
        specs = [s for s in specs if any(s.id.startswith(p) for p in prefixes)]
    if no_llm:
        specs = [s for s in specs if _is_deterministic(s)]

    if not specs:
        print("No matching cases found.")
        return 1

    has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    mode_label = "LIVE (recording)" if live else "REPLAY (cassettes)"
    if no_llm:
        mode_label = "DETERMINISTIC only"
    print(f"\nRunning {len(specs)} golden case(s) [{mode_label}]...\n")

    from prior_auth_agent.graph import build_graph
    graph = build_graph()

    results: list[dict] = []
    skipped_ids: list[str] = []

    for spec in specs:
        needs_llm = not _is_deterministic(spec)

        if needs_llm and not live and not Cassette.exists(spec.id):
            print(
                f"  {_YELLOW}SKIP{_RESET} {spec.id:<10}  "
                f"(no cassette — run: python -m evals.run --live {spec.id})"
            )
            skipped_ids.append(spec.id)
            results.append({
                "case_id": spec.id, "title": spec.title, "overall": "skipped",
                "scores": {d: {"result": "skip"} for d in [
                    "determination", "routing", "criterion_evidence", "citation_validity"
                ]},
                "state_summary": {},
            })
            continue

        if needs_llm and live and not has_key:
            print(f"  {_RED}FAIL{_RESET} {spec.id:<10}  (--live requires ANTHROPIC_API_KEY)")
            skipped_ids.append(spec.id)
            continue

        try:
            result = run_case(spec, graph, live=live)
        except StalenessError as exc:
            print(f"  {_RED}STALE{_RESET} {spec.id:<10}")
            print(f"              {_RED}{exc}{_RESET}")
            result = {
                "case_id": spec.id, "title": spec.title, "overall": "fail",
                "scores": {d: {"result": "fail", "error": "stale_cassette"} for d in [
                    "determination", "routing", "criterion_evidence", "citation_validity"
                ]},
                "state_summary": {"error": "stale_cassette"},
            }
        except Exception as exc:
            result = {
                "case_id": spec.id, "title": spec.title, "overall": "fail",
                "scores": {d: {"result": "fail", "error": str(exc)} for d in [
                    "determination", "routing", "criterion_evidence", "citation_validity"
                ]},
                "state_summary": {"error": str(exc)},
            }
            print(f"  {_RED}ERROR{_RESET} {spec.id:<10}  {type(exc).__name__}: {exc}")

        results.append(result)
        _print_case_row(result)

    _print_summary(results, len(skipped_ids))

    artifact = _write_artifact(results, skipped_ids)
    print(f"\n  Artifact: {artifact.relative_to(REPO_ROOT)}\n")

    exit_code = 0
    ran = [r for r in results if r["overall"] != "skipped"]

    if do_update_baseline:
        bl_path = _write_baseline(results)
        print(f"  Baseline updated: {bl_path.relative_to(REPO_ROOT)}")

    if do_baseline_check:
        passed, regressions = _check_baseline(results)
        if not passed:
            print(f"\n  {_RED}BASELINE REGRESSION:{_RESET}")
            for msg in regressions:
                print(f"    {msg}")
            exit_code = 1
        else:
            print(f"  {_GREEN}Baseline check passed.{_RESET}")

    if not do_baseline_check:
        if any(r["overall"] == "fail" for r in ran):
            exit_code = 1

    return exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

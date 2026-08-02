"""Golden eval runner — scores the prior-auth pipeline across all 15 cases.

Scores 4 dimensions per case:
  S1  determination  — pipeline decision matches expected decision
  S2  routing        — pipeline route matches expected route
  S3  criterion_evidence — each expected criterion matched to pipeline evidence
                           and status verified; catches "right answer wrong reason"
  S4  citation_validity  — surviving citations in met evidence resolve in bundle

Writes a JSON artifact to evals/results/run_YYYYMMDD_HHMMSS.json on every run.

Usage:
    python -m evals.run                # run all 15 cases
    python -m evals.run case_009       # run by id prefix
    python -m evals.run --no-llm       # run only deterministic cases (intake/eligibility)

Exit code: 0 if all ran cases pass all four dimensions, 1 otherwise.
"""

from __future__ import annotations

import json
import os
import sys
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
except ImportError:
    pass

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from evals.golden.schema import CaseSpec, CriterionGroundTruth
from evals.golden.generator import generate_bundle, CASES_DIR
from prior_auth_agent.validation import _build_ref_index

RESULTS_DIR = Path(__file__).resolve().parent / "results"

_GREEN = "\033[32m"
_RED = "\033[31m"
_YELLOW = "\033[33m"
_RESET = "\033[0m"
_PASS = f"{_GREEN}PASS{_RESET}"
_FAIL = f"{_RED}FAIL{_RESET}"
_SKIP = f"{_YELLOW}SKIP{_RESET}"


# ── criterion matching ─────────────────────────────────────────────────────────


def _match_by_citations(
    criterion: CriterionGroundTruth,
    pipeline_evidence: list[dict],
) -> Optional[dict]:
    """Find the pipeline evidence item that cites at least one expected_citation."""
    if not criterion.expected_citations:
        return None
    for item in pipeline_evidence:
        surviving = item.get("citations", [])
        summary = item.get("summary", "")
        # Also check stripped citations: resolve_citations puts the original
        # resource id in the CITATION STRIPPED annotation in the summary.
        if any(c in surviving or c in summary for c in criterion.expected_citations):
            return item
    return None


def _match_criterion(
    criterion: CriterionGroundTruth,
    pipeline_evidence: list[dict],
) -> Optional[dict]:
    """
    Find the best-fit pipeline evidence item for a single fixture criterion.

    For met criteria, expected_citations is non-empty — matched by citation
    overlap (see _match_by_citations above).

    For insufficient and not_met criteria, expected_citations may or may not
    be present. Implement your matching strategy here.

    Options:
      text_overlap:  tokenize criterion.text and each evidence item's
                     criterion_text; return the highest-overlap item.
                     Works across all statuses; may mismatch if wrong-policy
                     criteria have similar wording.
      positional:    match by index (fixture c1 → evidence[0], c2 → evidence[1]).
                     Simple; breaks if the LLM maps extra or reordered criteria.
      citation_first_then_text: use citation match for met criteria, text
                     overlap for insufficient/not_met — the hybrid approach.

    For not_met criteria (cases 014, 015): expected_citations is non-empty
    (the resource documenting the contradiction), so _match_by_citations works
    for them too. The TODO below is specifically for purely-silent insufficient
    criteria (cases 002, 003, 011) where expected_citations == [].
    """
    # met and not_met criteria with expected_citations: match by citation
    if criterion.expected_citations:
        return _match_by_citations(criterion, pipeline_evidence)

    # TODO: implement matching for insufficient criteria (expected_citations == [])
    # These are cases 002 c2, 003 c2, 011 c1/c2/c3 — all have no citations to
    # match on because chart is silent.
    #
    # Suggested starting point (text overlap):
    #
    # criterion_words = set(criterion.text.lower().split())
    # best, best_score = None, 0
    # for item in pipeline_evidence:
    #     item_words = set(item.get("criterion_text", "").lower().split())
    #     score = len(criterion_words & item_words)
    #     if score > best_score:
    #         best, best_score = item, score
    # return best if best_score > 0 else None
    #
    # Or positional (fragile but deterministic):
    #
    # idx = next(
    #     (i for i, c in enumerate(
    #         [c for c in pipeline_evidence]
    #     ) if c == item), None
    # )
    # return pipeline_evidence[idx] if idx is not None else None
    #
    # For now: return None (S3 will mark these as unmatched / skipped).
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

    # determination stage
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
            # Could not find a matching evidence item
            if crit.expected_status == "insufficient" and not crit.expected_citations:
                # Unmatched insufficient criterion with no citations — acceptable
                # if the overall determination is also insufficient_evidence.
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
        match_method = "citation" if crit.expected_citations else "unmatched"
        crit_pass = actual_status == crit.expected_status

        # Additional check for met criteria: at least one expected_citation survives
        surviving_citations: list[str] = []
        if crit.expected_status == "met" and crit.expected_citations:
            surviving = matched.get("citations", [])
            surviving_citations = [c for c in crit.expected_citations if c in surviving]
            if not surviving_citations:
                # Expected citation was stripped entirely — criterion downgraded
                crit_pass = False

        if not crit_pass:
            all_pass = False

        entry: dict = {
            "criterion_id": crit.id,
            "expected_status": crit.expected_status,
            "actual_status": actual_status,
            "match_method": match_method,
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
                problems.append(f"malformed citation {citation!r} on criterion {item.get('criterion_id')}")
                continue
            rtype, rid = parts[0].strip(), parts[1].strip()
            if (rtype, rid) not in ref_index:
                unresolved_count += 1
                problems.append(
                    f"unresolved citation {citation!r} not in bundle "
                    f"(criterion {item.get('criterion_id')}) — resolve_citations may not have run"
                )

    passed = unresolved_count == 0
    result: dict = {
        "result": "pass" if passed else "fail",
        "stripped_count": stripped_count,
        "unresolved_count": unresolved_count,
    }
    if problems:
        result["problems"] = problems
    return result


# ── case runner ────────────────────────────────────────────────────────────────


def _is_deterministic(spec: CaseSpec) -> bool:
    return spec.ground_truth.outcome.stage in ("intake", "eligibility")


def run_case(spec: CaseSpec, graph) -> dict:
    bundle = generate_bundle(spec)
    state = graph.invoke({"fhir_bundle": bundle, "cpt_code": spec.cpt})

    s1 = _score_determination(spec, state)
    s2 = _score_routing(spec, state)
    s3 = _score_criterion_evidence(spec, state, bundle)
    s4 = _score_citation_validity(spec, state, bundle)

    overall = all(
        s["result"] in ("pass", "skip")
        for s in (s1, s2, s3, s4)
    )

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
    strip = ""
    if crit.get("result") != "skip":
        fires = sum(
            1 for d in crit.get("details", []) if d.get("strip_log_fired")
        )
        if fires:
            strip = f" [{fires} strip]"

    cite = s["citation_validity"]
    cite_str = _fmt(cite["result"])
    if cite.get("stripped_count"):
        cite_str += f"({cite['stripped_count']} stripped)"

    icon = _GREEN + "OK" + _RESET if result["overall"] == "pass" else _RED + "XX" + _RESET
    print(
        f"  {icon} {result['case_id']:<10}  "
        f"det={_fmt(s['determination']['result'])}  "
        f"route={_fmt(s['routing']['result'])}  "
        f"crit={crit_str}{strip}  "
        f"cite={cite_str}"
    )
    # Print failures inline
    for dim, score in s.items():
        if score["result"] == "fail":
            if dim == "criterion_evidence":
                for d in score.get("details", []):
                    if d["result"] == "fail":
                        print(
                            f"              {_RED}↳ {dim} {d['criterion_id']}: "
                            f"expected {d['expected_status']!r}, "
                            f"got {d['actual_status']!r}{_RESET}"
                        )
            else:
                exp = score.get("expected", "?")
                act = score.get("actual", "?")
                print(f"              {_RED}↳ {dim}: expected {exp!r}, got {act!r}{_RESET}")


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
        print(f"  ({skipped} skipped — no API key)")
    else:
        print()


# ── JSON artifact ──────────────────────────────────────────────────────────────


def _get_git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=REPO_ROOT,
            text=True,
            stderr=subprocess.DEVNULL,
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


# ── main ───────────────────────────────────────────────────────────────────────


def main(argv: list[str]) -> int:
    no_llm = "--no-llm" in argv
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
    print(f"\nRunning {len(specs)} golden case(s)...\n")

    from prior_auth_agent.graph import build_graph
    graph = build_graph()

    results: list[dict] = []
    skipped_ids: list[str] = []

    for spec in specs:
        if not _is_deterministic(spec) and not has_key:
            print(f"  {_SKIP} {spec.id:<10}  (no ANTHROPIC_API_KEY)")
            skipped_ids.append(spec.id)
            results.append({
                "case_id": spec.id,
                "title": spec.title,
                "overall": "skipped",
                "scores": {d: {"result": "skip"} for d in [
                    "determination", "routing", "criterion_evidence", "citation_validity"
                ]},
                "state_summary": {},
            })
            continue

        try:
            result = run_case(spec, graph)
        except Exception as exc:
            result = {
                "case_id": spec.id,
                "title": spec.title,
                "overall": "fail",
                "scores": {d: {"result": "fail", "error": str(exc)} for d in [
                    "determination", "routing", "criterion_evidence", "citation_validity"
                ]},
                "state_summary": {"error": str(exc)},
            }

        results.append(result)
        _print_case_row(result)

    _print_summary(results, len(skipped_ids))

    artifact = _write_artifact(results, skipped_ids)
    print(f"\n  Artifact: {artifact.relative_to(REPO_ROOT)}\n")

    ran = [r for r in results if r["overall"] != "skipped"]
    return 0 if all(r["overall"] == "pass" for r in ran) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

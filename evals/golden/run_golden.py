"""Golden eval runner — runs the full prior-auth pipeline against each fixture.

Requires ANTHROPIC_API_KEY (or equivalent in .env). Cases 1-4 make LLM calls;
case_005 short-circuits at intake and requires no API key.

Usage:
    python -m evals.run_golden                # run all 5 cases
    python -m evals.run_golden case_005       # run one case by id prefix
    python -m evals.run_golden --intake-only  # run only the intake-rejection case

Exit code: 0 if all run cases pass, 1 if any fail.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Load .env from repo root so ANTHROPIC_API_KEY is available.
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
except ImportError:
    pass

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from evals.golden.schema import CaseSpec
from evals.golden.generator import generate_bundle, CASES_DIR, BUNDLES_DIR
from prior_auth_agent.graph import build_graph

PASS = "\033[32m✓\033[0m"
FAIL = "\033[31m✗\033[0m"


# ── assertion helpers ──────────────────────────────────────────────────────────


def _assert_intake_case(spec: CaseSpec, state: dict) -> list[str]:
    """Check a case expected to be rejected at intake."""
    failures = []
    valid = state.get("valid", True)
    errors = state.get("validation_errors", [])

    if valid:
        failures.append(f"expected intake rejection but valid=True")
    if spec.ground_truth.outcome.validation_error_contains:
        needle = spec.ground_truth.outcome.validation_error_contains
        if not any(needle in e for e in errors):
            failures.append(
                f"expected '{needle}' in validation_errors; got: {errors}"
            )
    # confirm no LLM nodes ran (no determination in state)
    if "determination" in state:
        failures.append("LLM determination was run — should have short-circuited at intake")
    return failures


def _assert_determination_case(spec: CaseSpec, state: dict) -> list[str]:
    """Check a case expected to reach determination."""
    failures = []
    outcome = spec.ground_truth.outcome

    final = state.get("final_decision", "")
    route = state.get("route", "")
    det = state.get("determination", {})

    if outcome.route and route != outcome.route:
        failures.append(f"route: expected '{outcome.route}', got '{route}'")

    if outcome.decision:
        model_decision = det.get("decision", "")
        if model_decision != outcome.decision:
            failures.append(
                f"decision: expected '{outcome.decision}', got '{model_decision}' "
                f"(confidence={det.get('confidence', '?')})"
            )

    return failures


def run_case(spec: CaseSpec, graph) -> tuple[bool, str]:
    bundle = generate_bundle(spec)
    state = graph.invoke({"fhir_bundle": bundle, "cpt_code": spec.cpt})

    if spec.ground_truth.outcome.stage == "intake":
        failures = _assert_intake_case(spec, state)
    else:
        failures = _assert_determination_case(spec, state)

    passed = len(failures) == 0
    detail_lines = [f"  {spec.id}: {spec.title}"]
    detail_lines.append(f"    final_decision : {state.get('final_decision', '-')}")
    detail_lines.append(f"    route          : {state.get('route', '-')}")
    if det := state.get("determination"):
        detail_lines.append(
            f"    determination  : {det.get('decision')} "
            f"(confidence={det.get('confidence', '?'):.2f})"
        )
    if failures:
        for f in failures:
            detail_lines.append(f"    FAIL  {f}")
    return passed, "\n".join(detail_lines)


def main(argv: list[str]) -> int:
    intake_only = "--intake-only" in argv
    prefixes = [a for a in argv if not a.startswith("--")]

    specs = CaseSpec.load_all(CASES_DIR)
    if prefixes:
        specs = [s for s in specs if any(s.id.startswith(p) for p in prefixes)]
    if intake_only:
        specs = [s for s in specs if s.ground_truth.outcome.stage == "intake"]

    if not specs:
        print("No matching cases found.")
        return 1

    print(f"\nRunning {len(specs)} golden case(s)...\n")
    graph = build_graph()

    passed_count = 0
    for spec in specs:
        # Skip LLM cases if no API key is available
        if spec.ground_truth.outcome.stage != "intake":
            import os
            if not os.environ.get("ANTHROPIC_API_KEY"):
                print(f"  {spec.id}: skipped (no ANTHROPIC_API_KEY)")
                continue

        try:
            passed, detail = run_case(spec, graph)
        except Exception as e:
            passed = False
            detail = f"  {spec.id}: raised {type(e).__name__}: {e}"

        icon = PASS if passed else FAIL
        print(f"{icon} {detail}\n")
        if passed:
            passed_count += 1

    ran = len(specs)
    print(f"Results: {passed_count}/{ran} passed")
    return 0 if passed_count == ran else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

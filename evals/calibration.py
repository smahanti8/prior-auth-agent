"""Standalone confidence-calibration report — Brier score over the golden
determination-stage cases, with an explicit gate on the binned reliability
diagram / expected calibration error (ECE).

Mirrors evals/cost_report.py's shape (own artifact under evals/results, own
printed report). Unlike cost_report.py it does not re-run the pipeline: it
reads the latest evals/results/run_*.json produced by `python -m evals.run`
and reuses prior_auth_agent.audit_packet._calibration_metrics — the same
Brier computation the audit packet's S1 scoreboard uses — so there is one
source of truth for the number, not two.

ECE is gated on sample size: with k bins, a bin's average confidence/outcome
is only meaningful with a handful of samples in it. Below that minimum this
report says so explicitly instead of printing a number the sample size can't
support (see EVALS.md S5 / Limitations #7).

Outputs:
  - evals/results/calibration_report_YYYYMMDD_HHMMSS.json (machine-readable)
  - stdout Markdown report

Usage:
    python -m evals.calibration
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "src"))

from prior_auth_agent.audit_packet import _calibration_metrics, _latest_json

RESULTS_DIR = REPO_ROOT / "evals" / "results"

DEFAULT_BIN_COUNT = 10
MIN_PER_BIN = 5


def min_n_for_bins(bin_count: int, min_per_bin: int = MIN_PER_BIN) -> int:
    return bin_count * min_per_bin


def build_report(eval_data: dict | None, bin_count: int = DEFAULT_BIN_COUNT) -> dict:
    cal = _calibration_metrics(eval_data)
    n = cal["n"]
    min_n = min_n_for_bins(bin_count)

    report = {
        "n": n,
        "brier_score": cal["brier_score"],
        "pairs": cal["pairs"],
        "bin_count": bin_count,
        "min_n_for_bins": min_n,
    }

    if n < min_n:
        report["ece"] = None
        report["ece_note"] = (
            f"ECE not reported: N too small; minimum N for {bin_count} bins is {min_n}."
        )
    else:
        # N supports binning, but the binned reliability diagram itself is
        # deferred until this branch is actually reached (see EVALS.md S5).
        report["ece"] = None
        report["ece_note"] = (
            f"N={n} reaches the minimum for {bin_count} bins ({min_n}), but "
            "the reliability-diagram computation is not yet implemented."
        )

    return report


def write_artifact(report: dict) -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out = RESULTS_DIR / f"calibration_report_{ts}.json"
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        **report,
    }
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return out


def print_markdown_report(report: dict) -> None:
    print()
    print("## S1 Confidence Calibration")
    print()
    n = report["n"]
    brier = report["brier_score"]
    brier_str = f"{brier:.6f}" if brier is not None else "n/a"
    print(f"N = {n} determination-stage golden cases.")
    print()
    print(f"Brier score = mean((confidence - outcome)**2) = **{brier_str}**")
    print()
    if report["ece"] is not None:
        print(f"ECE ({report['bin_count']} bins) = {report['ece']:.6f}")
    else:
        print(report["ece_note"])
    print()


def main(argv: list[str]) -> int:
    eval_data = _latest_json("run_")
    if eval_data is None:
        print(
            "No evals/results/run_*.json found — run `python -m evals.run` first.",
            file=sys.stderr,
        )
        return 1

    report = build_report(eval_data)
    artifact = write_artifact(report)
    print(f"Artifact: {artifact.relative_to(REPO_ROOT)}", file=sys.stderr)

    print_markdown_report(report)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

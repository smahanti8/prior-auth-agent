"""Compliance audit-packet generator.

Composes system identity, governance invariants (with live test results),
golden-set scoreboard, citation coverage, policy criterion traceability, cost
economics, and limitations into a single human-readable document.

Everything is derived from data the pipeline already produces. No LLM call.
No manual authoring. The only manual surface is two small config dicts at the
top of this file: _INVARIANT_CONFIG (rule → test mapping) and _CRITERION_CONFIG
(policy criterion → covering test mapping). Update these when a new invariant
or criterion is added.

Usage:
    python -m prior_auth_agent.audit_packet
    python -m prior_auth_agent.audit_packet --no-pytest   # skip live test run
    python -m prior_auth_agent.audit_packet --out docs/   # custom output dir

Output:
    docs/audit_packet_YYYYMMDD_GITSHA.md   (Markdown source, versioned)
    docs/audit_packet.html                  (self-contained HTML, always latest)
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS_DIR = REPO_ROOT / "docs"
EVALS_RESULTS_DIR = REPO_ROOT / "evals" / "results"
DECISIONS_MD = REPO_ROOT / "DECISIONS.md"
EVALS_MD = REPO_ROOT / "EVALS.md"
README_MD = REPO_ROOT / "README.md"
TESTS_DIR = REPO_ROOT / "tests"


# ── Manual config — the ONLY section requiring human upkeep ───────────────────
# Update _INVARIANT_CONFIG when a new governance rule or test is added.
# Update _CRITERION_CONFIG when the policy or criteria change.

_INVARIANT_CONFIG = [
    {
        "rule": "No AI denial",
        "description": (
            "The AI cannot frame, recommend, or auto-finalize a denial. "
            "Permitted outputs are `approve` and `insufficient_evidence` only. "
            "A human makes any denial."
        ),
        "decisions": ["D9"],
        "test_file": "tests/test_no_denial.py",
    },
    {
        "rule": "Deterministic autonomy gate",
        "description": (
            "Auto-finalization is pure Python: confidence ≥ 0.85 AND decision = approve "
            "AND no required criterion is insufficient. No LLM call in the gate."
        ),
        "decisions": ["D3"],
        "test_file": "tests/test_no_denial.py",
    },
    {
        "rule": "Bilateral citation gate (hard reject)",
        "description": (
            "A criterion marked `met` must carry both a policy-side quote and a "
            "chart-side FHIR citation. Missing either triggers hard rejection before "
            "determination — not a soft HITL route, a hard discard."
        ),
        "decisions": ["D2", "D10"],
        "test_file": "tests/test_citation_gate.py",
    },
    {
        "rule": "Citation existence check",
        "description": (
            "FHIR citations are resolved against the submitted bundle post-LLM. "
            "Unresolvable citations are stripped; a criterion with no surviving "
            "citations is downgraded to `insufficient`."
        ),
        "decisions": ["D2", "D10"],
        "test_file": "tests/test_citation_resolution.py",
    },
    {
        "rule": "Tamper-evident audit log",
        "description": (
            "Every audit record carries SHA-256 `prev_hash` and `entry_hash`. "
            "Editing, deleting, or reordering any record breaks chain verification. "
            "Limitation documented: whole-file rewrite can rebuild the chain."
        ),
        "decisions": ["D7"],
        "test_file": "tests/test_audit_log.py",
    },
    {
        "rule": "Append-only pending queue",
        "description": (
            "Reviewer resolutions are appended as events; the queue is never "
            "rewritten in place. Chain is verified on every read — a tampered "
            "queue fails loudly rather than serving doctored cases."
        ),
        "decisions": ["D8"],
        "test_file": "tests/test_audit_log.py",
    },
]

_CRITERION_CONFIG = [
    {
        "id": "C1",
        "text": "MRI-confirmed full-thickness rotator cuff tear",
        "required": True,
        "policy": "rotator_cuff_repair_29827.md",
        "cpt": "29827",
        "cases_met":         ["case_007"],
        "cases_insufficient": [],
        "cases_not_met":     [],
        "test_patterns": [
            "test_met_expected_citations_resolve_in_bundle[case_007]",
        ],
    },
    {
        "id": "C2",
        "text": "Failure of ≥3 months conservative management including PT",
        "required": True,
        "policy": "rotator_cuff_repair_29827.md",
        "cpt": "29827",
        "cases_met":         ["case_007"],
        "cases_insufficient": [],
        "cases_not_met":     [],
        "test_patterns": [
            "test_met_expected_citations_resolve_in_bundle[case_007]",
            "test_case_007_ghost_citation_absent_from_bundle",
        ],
    },
    {
        "id": "C3",
        "text": "Documented functional impairment affecting ADLs",
        "required": True,
        "policy": "rotator_cuff_repair_29827.md",
        "cpt": "29827",
        "cases_met":         ["case_007"],
        "cases_insufficient": [],
        "cases_not_met":     [],
        "test_patterns": [
            "test_met_expected_citations_resolve_in_bundle[case_007]",
        ],
    },
]

_KEY_CASES = {
    "case_007": "Strip-and-log: ghost citation absent from bundle stripped by existence check; criterion stays `met` on surviving citations",
    "case_009": "Eligibility gate: `Coverage.status=cancelled` halts pipeline before any LLM call",
    "case_011": "All criteria insufficient: bundle has only Patient+Coverage, zero clinical resources",
    "case_014": "`not_met`: DiagnosticReport explicitly denies psychiatric clearance for surgery",
    "case_015": "`not_met`: Observation documents patient refused PT — C2 not satisfied",
}


# ── Data loaders ──────────────────────────────────────────────────────────────


def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=REPO_ROOT, text=True, stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return "unknown"


def _prompt_hashes() -> dict[str, dict]:
    """Hash each LLM node's SYSTEM string from source — no cassette needed."""
    results = {}
    nodes = [
        ("criteria_mapper",    "prior_auth_agent.nodes.criteria_mapper"),
        ("evidence_extractor", "prior_auth_agent.nodes.evidence_extractor"),
        ("determination",      "prior_auth_agent.nodes.determination"),
    ]
    for name, module_path in nodes:
        try:
            mod = __import__(module_path, fromlist=["SYSTEM"])
            system = getattr(mod, "SYSTEM", "")
            h = hashlib.sha256(system.encode()).hexdigest()[:16]
            results[name] = {"hash": h, "length": len(system)}
        except Exception:
            results[name] = {"hash": "unavailable", "length": 0}
    return results


def _run_pytest_file(test_file: str, *, run_tests: bool) -> tuple[int, int]:
    """Return (passed, total) for a test file. Returns (-1, -1) on skip."""
    if not run_tests:
        return (-1, -1)
    path = REPO_ROOT / test_file
    if not path.exists():
        return (0, 0)
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", str(path), "-q", "--tb=no", "--no-header"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            env={**__import__("os").environ, "PYTHONPATH": str(REPO_ROOT / "src")},
        )
        output = result.stdout + result.stderr
        m = re.search(r"(\d+) passed", output)
        f = re.search(r"(\d+) failed", output)
        passed = int(m.group(1)) if m else 0
        failed = int(f.group(1)) if f else 0
        return (passed, passed + failed)
    except Exception:
        return (-1, -1)


def _run_all_tests(*, run_tests: bool) -> tuple[int, int]:
    """Return (passed, total) across all tests."""
    if not run_tests:
        return (-1, -1)
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/", "-q", "--tb=no", "--no-header"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            env={**__import__("os").environ, "PYTHONPATH": str(REPO_ROOT / "src")},
        )
        output = result.stdout + result.stderr
        m = re.search(r"(\d+) passed", output)
        f = re.search(r"(\d+) failed", output)
        passed = int(m.group(1)) if m else 0
        failed = int(f.group(1)) if f else 0
        return (passed, passed + failed)
    except Exception:
        return (-1, -1)


def _latest_json(prefix: str) -> Optional[dict]:
    """Return the contents of the most-recent file matching evals/results/<prefix>*.json."""
    candidates = sorted(EVALS_RESULTS_DIR.glob(f"{prefix}*.json"), reverse=True)
    for c in candidates:
        if c.name == ".gitkeep":
            continue
        try:
            return json.loads(c.read_text(encoding="utf-8"))
        except Exception:
            continue
    return None


def _parse_evals_limitations() -> str:
    """Extract the Limitations section from EVALS.md."""
    try:
        text = EVALS_MD.read_text(encoding="utf-8")
        m = re.search(r"## Limitations.*?(?=\Z)", text, re.DOTALL)
        if m:
            # Strip the header line itself
            body = re.sub(r"^## Limitations[^\n]*\n", "", m.group(0))
            return body.strip()
    except Exception:
        pass
    return ""


def _parse_readme_limitations() -> list[str]:
    """Extract Known Limitations bullets from README.md."""
    try:
        text = README_MD.read_text(encoding="utf-8")
        m = re.search(r"## Known Limitations\n(.*?)(?=\n## |\Z)", text, re.DOTALL)
        if m:
            lines = [ln.strip() for ln in m.group(1).strip().splitlines() if ln.strip()]
            items = []
            for ln in lines:
                if ln.startswith(("1.", "2.", "3.", "4.", "5.", "6.", "7.", "8.", "9.", "-")):
                    items.append(re.sub(r"^\d+\.\s*|-\s*", "", ln).strip())
                elif items:
                    items[-1] += " " + ln
            return items
    except Exception:
        pass
    return []


def _citation_coverage(eval_data: dict) -> dict:
    """Derive citation-gate counts from eval results."""
    total_criteria = 0
    met_bilateral = 0
    stripped_citations = 0
    downgraded_criteria = 0
    hard_rejected_cases = 0

    for case in eval_data.get("cases", []):
        s3 = case.get("scores", {}).get("criterion_evidence", {})
        s4 = case.get("scores", {}).get("citation_validity", {})
        if s3.get("result") == "skip":
            continue
        for detail in s3.get("details", []):
            total_criteria += 1
            if detail.get("expected_status") == "met":
                if detail.get("result") == "pass":
                    met_bilateral += 1
            if detail.get("strip_log_fired"):
                stripped_citations += 1
        stripped = s4.get("stripped_count", 0)
        if stripped:
            downgraded_criteria += stripped

        fin = case.get("state_summary", {}).get("final_decision", "") or ""
        if "rejected_uncited" in fin or "citation_reject" in fin:
            hard_rejected_cases += 1

    return {
        "total_criteria": total_criteria,
        "met_bilateral": met_bilateral,
        "stripped_citations": stripped_citations,
        "downgraded_criteria": downgraded_criteria,
        "hard_rejected_cases": hard_rejected_cases,
    }


def _routing_precision_recall(eval_data: dict) -> dict:
    """Compute HITL routing precision and recall from eval results."""
    tp = fp = fn = 0
    for case in eval_data.get("cases", []):
        s2 = case.get("scores", {}).get("routing", {})
        if s2.get("result") == "skip":
            continue
        expected = s2.get("expected", "")
        actual = s2.get("actual", "")
        if expected == "hitl" and actual == "hitl":
            tp += 1
        elif expected == "hitl" and actual != "hitl":
            fn += 1
        elif expected != "hitl" and actual == "hitl":
            fp += 1
    precision = tp / (tp + fp) if (tp + fp) > 0 else None
    recall = tp / (tp + fn) if (tp + fn) > 0 else None
    return {
        "tp": tp, "fp": fp, "fn": fn,
        "precision": precision, "recall": recall,
        "hitl_expected": tp + fn,
        "hitl_actual": tp + fp,
    }


# ── Markdown section builders ─────────────────────────────────────────────────


def _pct(n: int, d: int) -> str:
    return f"{n}/{d} ({100*n//d}%)" if d else "n/a"


def _status(passed: int, total: int) -> str:
    if passed == -1:
        return "(skipped)"
    if total == 0:
        return "file not found"
    if passed == total:
        return f"**PASS** ({passed}/{total})"
    return f"**FAIL** ({passed}/{total})"


def _na(v) -> str:
    if v is None:
        return "—"
    return str(v)


def _display_path(p: Path) -> str:
    """Render a path relative to REPO_ROOT for a friendly log line, falling
    back to the absolute path when --out points outside the repo (relative_to
    raises ValueError in that case rather than producing a '../' path)."""
    try:
        return str(p.relative_to(REPO_ROOT))
    except ValueError:
        return str(p)


def _section_identity(sha: str, prompt_hashes: dict) -> str:
    from .config import MODEL, CONFIDENCE_THRESHOLD
    rows = [
        ("LLM model", f"`{MODEL}`"),
        ("Confidence threshold", str(CONFIDENCE_THRESHOLD)),
    ]
    for node, info in prompt_hashes.items():
        rows.append((
            f"{node.replace('_', ' ').title()} prompt",
            f"`{info['hash']}`  (SHA-256[:16] of SYSTEM string, commit `{sha}`)",
        ))
    table = "| Component | Value |\n|-----------|-------|\n"
    table += "\n".join(f"| {k} | {v} |" for k, v in rows)
    note = (
        "\n\n*Prompt hashes are computed from each node's `SYSTEM` constant "
        "at generation time. A changed prompt produces a different hash and "
        "this table changes on next regeneration.*"
    )
    return table + note


def _section_invariants(test_results: dict[str, tuple[int, int]]) -> str:
    header = (
        "| Rule | Description | Decision(s) | Enforcing test | Status |\n"
        "|------|-------------|-------------|----------------|--------|\n"
    )
    rows = []
    for cfg in _INVARIANT_CONFIG:
        decisions = " / ".join(cfg["decisions"])
        tf = cfg["test_file"]
        passed, total = test_results.get(tf, (-1, -1))
        rows.append(
            f"| {cfg['rule']} | {cfg['description']} | {decisions} "
            f"| `{tf}` | {_status(passed, total)} |"
        )
    note = (
        "\n\n*A rule without a passing test is not a rule — it is a comment. "
        "Status is computed by running each test file at generation time.*"
    )
    return header + "\n".join(rows) + note


def _section_scoreboard(eval_data: Optional[dict], run_tests: bool) -> str:
    if eval_data is None:
        return (
            "> **[LIVE RUN REQUIRED]** No eval results found.\n"
            "> Run: `python -m evals.run --live` then `python -m evals.run --update-baseline`"
        )

    summary = eval_data.get("summary", {})
    ts = eval_data.get("timestamp", "unknown")
    sha = eval_data.get("git_sha", "unknown")
    ran = summary.get("ran", 0)
    total = summary.get("total_cases", 0)
    scores = summary.get("scores", {})

    dims = [
        ("S1 Determination accuracy", "determination",
         "Pipeline decision matches expected (approve / insufficient_evidence / reject)"),
        ("S2 Routing accuracy", "routing",
         "auto vs HITL routing matches expected for determination-stage cases"),
        ("S3 Criterion-level evidence accuracy", "criterion_evidence",
         "Each criterion matched to correct pipeline evidence with correct status and citations"),
        ("S4 Citation validity", "citation_validity",
         "Surviving citations in `met` evidence resolve against the submitted bundle"),
    ]

    meta = (
        f"**Eval run:** `evals/results/run_{eval_data.get('run_id', 'unknown')}.json`  \n"
        f"**Run timestamp:** {ts}  \n"
        f"**Eval git SHA:** `{sha}`  \n"
        f"**Cases:** {ran} ran of {total} total  \n"
        f"**Ground truth:** human-authored from policy documents — LLM never used to generate expected outcomes.\n\n"
    )

    table = "| Dimension | What it measures | Scoreable | Result |\n|-----------|-----------------|-----------|--------|\n"
    for label, key, desc in dims:
        s = scores.get(key, {})
        p, f, sk = s.get("pass", 0), s.get("fail", 0), s.get("skip", 0)
        scoreable = ran - sk
        result = f"{p}/{scoreable}" if scoreable else "n/a"
        result_str = f"**{result}**" if f == 0 and scoreable > 0 else result
        table += f"| {label} | {desc} | {scoreable} | {result_str} |\n"

    # Routing precision/recall
    pr = _routing_precision_recall(eval_data)
    precision_str = f"{pr['precision']:.0%}" if pr['precision'] is not None else "n/a"
    recall_str = f"{pr['recall']:.0%}" if pr['recall'] is not None else "n/a"
    pr_table = (
        "\n**S2 HITL routing — precision and recall:**\n\n"
        "| Metric | Value |\n|--------|-------|\n"
        f"| Cases expected HITL | {pr['hitl_expected']} |\n"
        f"| True positives (correctly sent to HITL) | {pr['tp']} |\n"
        f"| False positives (sent to HITL unnecessarily) | {pr['fp']} |\n"
        f"| False negatives (missed — sent to auto) | {pr['fn']} |\n"
        f"| Precision | {precision_str} |\n"
        f"| Recall | {recall_str} |\n"
    )

    # Key behavioral cases
    cases_by_id = {c["case_id"]: c for c in eval_data.get("cases", [])}
    key_rows = []
    for case_id, behavior in _KEY_CASES.items():
        case = cases_by_id.get(case_id)
        if case is None:
            key_rows.append(f"| `{case_id}` | {behavior} | — | — | — | — |")
            continue
        s = case.get("scores", {})
        def r(dim):
            res = s.get(dim, {}).get("result", "—")
            return {"pass": "PASS", "fail": "**FAIL**", "skip": "skip"}.get(res, res)
        key_rows.append(
            f"| `{case_id}` | {behavior} | {r('determination')} | {r('routing')} | {r('criterion_evidence')} | {r('citation_validity')} |"
        )

    key_table = (
        "\n**Key behavioral cases:**\n\n"
        "| Case | Behavior | S1 | S2 | S3 | S4 |\n"
        "|------|----------|----|----|----|----|\n"
        + "\n".join(key_rows)
    )

    return meta + table + pr_table + key_table


def _section_citation_coverage(eval_data: Optional[dict]) -> str:
    if eval_data is None:
        return "> **[LIVE RUN REQUIRED]** No eval results found.\n"
    cov = _citation_coverage(eval_data)
    table = (
        "| Metric | Count |\n|--------|-------|\n"
        f"| Total criteria evaluated across determination-stage cases | {cov['total_criteria']} |\n"
        f"| Met criteria with bilateral citations (passed gate) | {cov['met_bilateral']} |\n"
        f"| Citations stripped by existence check | {cov['stripped_citations']} |\n"
        f"| Criteria downgraded to `insufficient` (no citations survived) | {cov['downgraded_criteria']} |\n"
        f"| Cases hard-rejected at citation gate (never reached determination) | {cov['hard_rejected_cases']} |\n"
    )
    note = (
        "\n*Derived from `scores.criterion_evidence.details[].strip_log_fired` "
        "and `scores.citation_validity.stripped_count` in the eval results JSON. "
        "`case_007` is the designed strip-and-log case; any other stripped citations "
        "represent unintended pipeline behavior.*"
    )
    return table + note


def _section_traceability(test_results_overall: tuple[int, int]) -> str:
    header = (
        "**Policy document:** `data/policies/rotator_cuff_repair_29827.md`  \n"
        "**CPT code:** 29827\n\n"
        "| ID | Criterion | Required | Met in | Insufficient in | Not-met in | Covering test patterns | Status |\n"
        "|----|-----------|----------|--------|----------------|------------|------------------------|--------|\n"
    )
    rows = []
    passed_all, total_all = test_results_overall
    for c in _CRITERION_CONFIG:
        req = "Yes" if c["required"] else "No"
        met = ", ".join(f"`{x}`" for x in c["cases_met"]) or "—"
        insuf = ", ".join(f"`{x}`" for x in c["cases_insufficient"]) or "—"
        not_met = ", ".join(f"`{x}`" for x in c["cases_not_met"]) or "—"
        patterns = "<br>".join(f"`{p}`" for p in c["test_patterns"])
        status = _status(passed_all, total_all) if passed_all != -1 else "(skipped)"
        rows.append(f"| {c['id']} | {c['text']} | {req} | {met} | {insuf} | {not_met} | {patterns} | {status} |")
    note = (
        "\n\n*The criterion→test mapping in this table is declared in the generator "
        "config (~10 lines). It is the only section requiring human update when "
        "criteria or test names change. CPT 29827 currently has exactly one golden "
        "case (`case_007`), which exercises `met` for all three criteria — there is "
        "no `insufficient` or `not_met` example for this policy in the golden set "
        "yet, hence the empty columns above.*"
    )
    return header + "\n".join(rows) + note


def _section_cost(cost_data: Optional[dict]) -> str:
    if cost_data is None:
        return (
            "> **[LIVE RUN REQUIRED]** No cost report found.  \n"
            "> Run: `python -m evals.run --live && python -m evals.cost_report`"
        )

    source = cost_data.get("source", "unknown")
    model = cost_data.get("model", "unknown")
    case_count = cost_data.get("case_count", 0)
    report = cost_data.get("report", {})
    nodes = report.get("nodes", [])
    total_cost = report.get("total_cost_usd", 0)
    proj = report.get("projections", {})

    meta = (
        f"**Source:** {source} (recorded token counts from live run)  \n"
        f"**Model:** `{model}`  \n"
        f"**Cases averaged:** {case_count} LLM-stage golden-set cases\n\n"
    )

    node_rows = []
    for n in nodes:
        name = n["node_name"]
        ntype = n["node_type"]
        if ntype == "llm":
            tok = f"{n.get('avg_input_tokens', '—')} / {n.get('avg_output_tokens', '—')}"
            cost = f"**${n['avg_cost_usd']:.4f}**"
            bold = "**"
        else:
            tok = "—"
            cost = "$0.0000"
            bold = ""
        node_rows.append(f"| {bold}{name}{bold} | {ntype} | {tok} | {cost} |")
    node_rows.append(f"| **TOTAL** | | | **${total_cost:.4f}** |")

    node_table = (
        "| Node | Type | Avg tokens (in / out) | Cost / determination |\n"
        "|------|------|-----------------------|---------------------|\n"
        + "\n".join(node_rows)
    )

    det_note = (
        "\n\n*5 of 8 nodes cost $0 in LLM terms. The cost story: three nodes are "
        "billable; the rest — including all safety-critical gates — are deterministic code.*\n"
    )

    proj_table = (
        "\n**Monthly projection:**\n\n"
        "| Volume | Monthly cost |\n|--------|-------------|\n"
        f"| 1,000 determinations / month | ${proj.get('1k_per_month', 0):.2f} |\n"
        f"| 100,000 determinations / month | ${proj.get('100k_per_month', 0):,.2f} |\n"
        f"| 1,000,000 determinations / month | ${proj.get('1m_per_month', 0):,.2f} |\n"
    )

    return meta + node_table + det_note + proj_table


def _section_limitations() -> str:
    evals_lim = _parse_evals_limitations()
    readme_lim = _parse_readme_limitations()

    parts = []
    if evals_lim:
        parts.append("### From the evaluation methodology (EVALS.md)\n\n" + evals_lim)
    else:
        parts.append("*[Limitations section not found in EVALS.md]*")

    if readme_lim:
        bullets = "\n".join(f"- {item}" for item in readme_lim)
        parts.append("### Operational limitations (from README)\n\n" + bullets)

    return "\n\n".join(parts)


def _section_provenance(
    sha: str,
    eval_data: Optional[dict],
    cost_data: Optional[dict],
    test_run: tuple[int, int],
) -> str:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    eval_src = (
        f"`evals/results/run_{eval_data['run_id']}.json`" if eval_data else "not available"
    )
    cost_src = (
        f"`evals/results/cost_report_{cost_data.get('generated_at', '')[:10].replace('-','')}.json`"
        if cost_data else "not available"
    )
    p, t = test_run
    test_str = _status(p, t)

    table = (
        "| Field | Value |\n|-------|-------|\n"
        f"| Generated at | {now} |\n"
        f"| Git SHA | `{sha}` |\n"
        f"| Generator | `python -m prior_auth_agent.audit_packet` |\n"
        f"| Eval results | {eval_src} |\n"
        f"| Cost report | {cost_src} |\n"
        f"| Test run at generation | {test_str} |\n"
    )
    note = (
        "\n*Regenerate after any model change, prompt change, or new eval run. "
        "Do not use a packet whose SHA does not match the deployed system.*"
    )
    return table + note


# ── Markdown assembly ─────────────────────────────────────────────────────────


def build_markdown(
    sha: str,
    eval_data: Optional[dict],
    cost_data: Optional[dict],
    prompt_hashes: dict,
    test_results: dict[str, tuple[int, int]],
    test_run_overall: tuple[int, int],
) -> str:
    from .config import MODEL

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    eval_ts = eval_data.get("timestamp", "—") if eval_data else "—"
    cost_ts = cost_data.get("generated_at", "—") if cost_data else "—"

    header = textwrap.dedent(f"""\
        # AI Prior-Authorization System — Compliance Audit Packet

        **Generated:** {now}
        **Git commit:** `{sha}`
        **Model:** `{MODEL}`
        **Eval run:** {f"`evals/results/run_{eval_data['run_id']}.json`" if eval_data else "not available"} ({eval_ts})
        **Cost report:** {f"available ({cost_ts})" if cost_data else "not available"}

        This packet is machine-generated from live system data. It does not require
        manual maintenance. To regenerate after any system change:

            python -m prior_auth_agent.audit_packet

        The packet describes the system at the Git SHA above. A different commit, a
        different model, or a new eval run produces a different packet. Do not use
        a packet whose SHA does not match the deployed system.

        ---
    """)

    sections = [
        ("## 1. System Identity", _section_identity(sha, prompt_hashes)),
        ("## 2. Governance Invariants", _section_invariants(test_results)),
        ("## 3. Golden-Set Evaluation Scoreboard", _section_scoreboard(eval_data, True)),
        ("## 4. Citation-Gate Coverage", _section_citation_coverage(eval_data)),
        ("## 5. Traceability: Policy Criteria → Tests", _section_traceability(test_run_overall)),
        ("## 6. Cost per Determination", _section_cost(cost_data)),
        ("## 7. Limitations", _section_limitations()),
        ("## 8. Generation Provenance", _section_provenance(sha, eval_data, cost_data, test_run_overall)),
    ]

    body = []
    for heading, content in sections:
        body.append(f"{heading}\n\n{content}")

    return header + "\n\n---\n\n".join(body) + "\n"


# ── HTML renderer ─────────────────────────────────────────────────────────────

_CSS = """
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
       max-width: 980px; margin: 40px auto; padding: 0 24px;
       color: #24292e; line-height: 1.6; }
h1 { font-size: 1.8em; border-bottom: 2px solid #e1e4e8; padding-bottom: .3em; }
h2 { font-size: 1.35em; border-bottom: 1px solid #e1e4e8; padding-bottom: .2em;
     margin-top: 2em; }
h3 { font-size: 1.1em; margin-top: 1.5em; }
table { border-collapse: collapse; width: 100%; margin: 1em 0; font-size: .9em; }
th { background: #f6f8fa; text-align: left; padding: 8px 12px;
     border: 1px solid #d0d7de; }
td { padding: 6px 12px; border: 1px solid #d0d7de; vertical-align: top; }
tr:nth-child(even) td { background: #f6f8fa; }
code { background: #f0f2f5; padding: 2px 5px; border-radius: 3px;
       font-family: 'SFMono-Regular', Consolas, monospace; font-size: .88em; }
pre { background: #f6f8fa; border: 1px solid #e1e4e8; border-radius: 4px;
      padding: 12px 16px; overflow-x: auto; }
pre code { background: none; padding: 0; }
blockquote { border-left: 4px solid #d0d7de; margin: 0; padding: 8px 16px;
             color: #57606a; background: #f6f8fa; }
hr { border: none; border-top: 1px solid #e1e4e8; margin: 24px 0; }
strong { font-weight: 600; }
em { font-style: italic; }
.provenance { background: #fff8c5; border: 1px solid #d4a72c;
              border-radius: 4px; padding: 10px 16px; margin-bottom: 1.5em; }
"""


def _render_html(md: str) -> str:
    """Minimal Markdown → HTML for the constructs this generator uses.

    Not a general Markdown parser. Handles: ATX headers, GFM tables, fenced
    code blocks, **bold**, *italic*, `inline code`, `---` rules, blockquotes,
    ordered/unordered lists, and paragraphs. Sufficient for this packet.
    """
    import html as _html

    lines = md.splitlines()
    out: list[str] = []
    i = 0
    in_list = False
    in_blockquote = False
    para_lines: list[str] = []

    def flush_para():
        nonlocal para_lines
        if para_lines:
            text = " ".join(para_lines)
            if text.strip():
                out.append(f"<p>{_inline(text)}</p>")
            para_lines = []

    def flush_list():
        nonlocal in_list
        if in_list:
            out.append("</ul>")
            in_list = False

    def flush_bq():
        nonlocal in_blockquote
        if in_blockquote:
            out.append("</blockquote>")
            in_blockquote = False

    def _escape(s: str) -> str:
        return _html.escape(s, quote=False)

    def _inline(s: str) -> str:
        # Bold+italic ***x***
        s = re.sub(r"\*\*\*(.+?)\*\*\*", r"<strong><em>\1</em></strong>", s)
        # Bold **x**
        s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
        # Italic *x*
        s = re.sub(r"\*(.+?)\*", r"<em>\1</em>", s)
        # Inline code `x`
        s = re.sub(r"`([^`]+)`", lambda m: f"<code>{_escape(m.group(1))}</code>", s)
        # Hard line break (two spaces + newline already collapsed)
        s = s.replace("  ", "<br>")
        return s

    while i < len(lines):
        line = lines[i]

        # Fenced code block
        if line.startswith("```"):
            flush_para()
            flush_list()
            flush_bq()
            lang = line[3:].strip()
            code_lines = []
            i += 1
            while i < len(lines) and not lines[i].startswith("```"):
                code_lines.append(_escape(lines[i]))
                i += 1
            out.append(f'<pre><code class="language-{lang}">' + "\n".join(code_lines) + "</code></pre>")
            i += 1
            continue

        # ATX heading
        m = re.match(r"(#{1,6})\s+(.*)", line)
        if m:
            flush_para()
            flush_list()
            flush_bq()
            level = len(m.group(1))
            text = _inline(m.group(2))
            out.append(f"<h{level}>{text}</h{level}>")
            i += 1
            continue

        # HR
        if re.match(r"^---+$", line.strip()):
            flush_para()
            flush_list()
            flush_bq()
            out.append("<hr>")
            i += 1
            continue

        # GFM table (detect by | at start and separator row)
        if "|" in line and i + 1 < len(lines) and re.match(r"[\|\s\-:]+", lines[i + 1]):
            flush_para()
            flush_list()
            flush_bq()
            # header
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            out.append("<table>")
            out.append("<tr>" + "".join(f"<th>{_inline(c)}</th>" for c in cells) + "</tr>")
            i += 2  # skip separator
            while i < len(lines) and "|" in lines[i]:
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                out.append("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in cells) + "</tr>")
                i += 1
            out.append("</table>")
            continue

        # Blockquote
        if line.startswith("> "):
            flush_para()
            flush_list()
            if not in_blockquote:
                out.append("<blockquote>")
                in_blockquote = True
            out.append(f"<p>{_inline(line[2:])}</p>")
            i += 1
            continue
        else:
            flush_bq()

        # Unordered list
        m = re.match(r"^[-*]\s+(.*)", line)
        if m:
            flush_para()
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{_inline(m.group(1))}</li>")
            i += 1
            continue

        # Ordered list
        m = re.match(r"^\d+\.\s+(.*)", line)
        if m:
            flush_para()
            if not in_list:
                out.append("<ol>")
                in_list = True
            out.append(f"<li>{_inline(m.group(1))}</li>")
            i += 1
            continue
        else:
            flush_list()

        # Blank line
        if not line.strip():
            flush_para()
            i += 1
            continue

        # Indented code (4 spaces)
        if line.startswith("    "):
            flush_para()
            out.append(f"<pre><code>{_escape(line[4:])}</code></pre>")
            i += 1
            continue

        # Paragraph accumulator
        para_lines.append(_inline(line))
        i += 1

    flush_para()
    flush_list()
    flush_bq()

    body = "\n".join(out)
    return (
        "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n"
        "<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        "<title>Prior-Auth Compliance Audit Packet</title>\n"
        f"<style>{_CSS}</style>\n"
        "</head>\n<body>\n"
        + body
        + "\n</body>\n</html>\n"
    )


# ── Main ──────────────────────────────────────────────────────────────────────


def main(argv: list[str]) -> int:
    run_tests = "--no-pytest" not in argv
    out_dir_arg = None
    for idx, a in enumerate(argv):
        if a == "--out" and idx + 1 < len(argv):
            out_dir_arg = Path(argv[idx + 1])

    out_dir = out_dir_arg or DOCS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    print("Collecting data...", file=sys.stderr)

    sha = _git_sha()
    prompt_hashes = _prompt_hashes()
    eval_data = _latest_json("run_")
    cost_data = _latest_json("cost_report_")

    # Run per-file tests for governance table
    seen_files: set[str] = set()
    test_results: dict[str, tuple[int, int]] = {}
    if run_tests:
        print("  Running tests...", file=sys.stderr)
        for cfg in _INVARIANT_CONFIG:
            tf = cfg["test_file"]
            if tf not in seen_files:
                seen_files.add(tf)
                p, t = _run_pytest_file(tf, run_tests=True)
                test_results[tf] = (p, t)
                print(f"    {tf}: {p}/{t}", file=sys.stderr)
        test_run_overall = _run_all_tests(run_tests=True)
        print(f"  Overall: {test_run_overall[0]}/{test_run_overall[1]}", file=sys.stderr)
    else:
        test_results = {cfg["test_file"]: (-1, -1) for cfg in _INVARIANT_CONFIG}
        test_run_overall = (-1, -1)

    print("Building Markdown...", file=sys.stderr)
    md = build_markdown(
        sha=sha,
        eval_data=eval_data,
        cost_data=cost_data,
        prompt_hashes=prompt_hashes,
        test_results=test_results,
        test_run_overall=test_run_overall,
    )

    now_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    md_path = out_dir / f"audit_packet_{now_str}_{sha}.md"
    md_path.write_text(md, encoding="utf-8")
    print(f"  Markdown: {_display_path(md_path)}", file=sys.stderr)

    print("Rendering HTML...", file=sys.stderr)
    html = _render_html(md)
    html_path = out_dir / "audit_packet.html"
    html_path.write_text(html, encoding="utf-8")
    print(f"  HTML:     {_display_path(html_path)}", file=sys.stderr)

    print("Done.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

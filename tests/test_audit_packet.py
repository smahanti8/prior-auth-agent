"""Tests for the audit-packet generator.

All tests run without an API key and without launching a subprocess pytest run
(--no-pytest equivalent: tests call build_markdown() directly so we don't get
recursive test-inside-test behaviour). The assertions mirror what an auditor
would check when reviewing the packet.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))


from prior_auth_agent.audit_packet import (
    _INVARIANT_CONFIG,
    _CRITERION_CONFIG,
    _render_html,
    _section_cost,
    _section_identity,
    _section_invariants,
    _section_scoreboard,
    _section_traceability,
    build_markdown,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

FAKE_SHA = "abc1234"

FAKE_EVAL_DATA = {
    "run_id": "20260728_215756",
    "timestamp": "2026-07-28T21:57:56Z",
    "git_sha": FAKE_SHA,
    "summary": {
        "total_cases": 15,
        "ran": 13,
        "skipped": 2,
        "scores": {
            "determination":     {"pass": 13, "fail": 0, "skip": 2},
            "routing":           {"pass": 11, "fail": 0, "skip": 2},
            "criterion_evidence":{"pass": 13, "fail": 0, "skip": 2},
            "citation_validity": {"pass": 13, "fail": 0, "skip": 2},
        },
    },
    "cases": [
        {
            "case_id": "case_001",
            "title": "Full evidence, bilateral citations",
            "overall": "pass",
            "scores": {
                "determination": {"result": "pass", "expected": "approve", "actual": "approve"},
                "routing":       {"result": "pass", "expected": "auto",   "actual": "auto"},
                "criterion_evidence": {
                    "result": "pass",
                    "matched": 3, "total": 3,
                    "details": [
                        {"criterion_id": "C1", "expected_status": "met", "actual_status": "met", "result": "pass", "strip_log_fired": False},
                        {"criterion_id": "C2", "expected_status": "met", "actual_status": "met", "result": "pass", "strip_log_fired": False},
                        {"criterion_id": "C3", "expected_status": "met", "actual_status": "met", "result": "pass", "strip_log_fired": False},
                    ],
                },
                "citation_validity": {"result": "pass", "stripped_count": 0, "unresolved_count": 0},
            },
            "state_summary": {"final_decision": "approve"},
        },
        {
            "case_id": "case_007",
            "title": "Strip-and-log",
            "overall": "pass",
            "scores": {
                "determination": {"result": "pass", "expected": "approve", "actual": "approve"},
                "routing":       {"result": "pass", "expected": "auto",   "actual": "auto"},
                "criterion_evidence": {
                    "result": "pass",
                    "matched": 3, "total": 3,
                    "details": [
                        {"criterion_id": "C1", "expected_status": "met", "actual_status": "met", "result": "pass", "strip_log_fired": True},
                    ],
                },
                "citation_validity": {"result": "pass", "stripped_count": 1, "unresolved_count": 0},
            },
            "state_summary": {"final_decision": "approve"},
        },
        {
            "case_id": "case_009",
            "title": "Eligibility gate",
            "overall": "skip",
            "scores": {
                "determination": {"result": "skip", "expected": None, "actual": None},
                "routing":       {"result": "skip", "expected": None, "actual": None},
                "criterion_evidence": {"result": "skip", "matched": 0, "total": 0, "details": []},
                "citation_validity": {"result": "skip", "stripped_count": 0, "unresolved_count": 0},
            },
            "state_summary": {"final_decision": "rejected_at_intake: ineligible"},
        },
    ],
}

FAKE_COST_DATA = {
    "source": "cassette_replay",
    "model": "claude-opus-4-8",
    "case_count": 13,
    "generated_at": "2026-07-28T22:00:00Z",
    "report": {
        "nodes": [
            {"node_name": "criteria_mapper", "node_type": "llm",
             "avg_input_tokens": 2000, "avg_output_tokens": 400,
             "avg_latency_ms": 1800.0, "avg_cost_usd": 0.0600},
            {"node_name": "evidence_extractor", "node_type": "llm",
             "avg_input_tokens": 8000, "avg_output_tokens": 1200,
             "avg_latency_ms": 5400.0, "avg_cost_usd": 0.2100},
            {"node_name": "determination", "node_type": "llm",
             "avg_input_tokens": 3000, "avg_output_tokens": 600,
             "avg_latency_ms": 2000.0, "avg_cost_usd": 0.0900},
        ],
        "total_cost_usd": 0.36,
        "total_latency_ms": 9200.0,
        "projections": {
            "1k_per_month":   360.0,
            "100k_per_month": 36_000.0,
            "1m_per_month":   360_000.0,
        },
    },
}

FAKE_PROMPT_HASHES = {
    "criteria_mapper":    {"hash": "aabb1122ccdd3344", "length": 1200},
    "evidence_extractor": {"hash": "eeff5566aabb7788", "length": 2400},
    "determination":      {"hash": "11223344aabbccdd", "length": 900},
}

FAKE_TEST_RESULTS = {cfg["test_file"]: (4, 4) for cfg in _INVARIANT_CONFIG}


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_build_markdown_runs_without_error():
    """Generator must not raise given valid synthetic data."""
    md = build_markdown(
        sha=FAKE_SHA,
        eval_data=FAKE_EVAL_DATA,
        cost_data=FAKE_COST_DATA,
        prompt_hashes=FAKE_PROMPT_HASHES,
        test_results=FAKE_TEST_RESULTS,
        test_run_overall=(92, 92),
    )
    assert isinstance(md, str)
    assert len(md) > 500  # not empty / not degenerate


def test_build_markdown_contains_all_section_headings():
    """All 8 section headings must be present — an auditor needs every section."""
    md = build_markdown(
        sha=FAKE_SHA,
        eval_data=FAKE_EVAL_DATA,
        cost_data=FAKE_COST_DATA,
        prompt_hashes=FAKE_PROMPT_HASHES,
        test_results=FAKE_TEST_RESULTS,
        test_run_overall=(92, 92),
    )
    for heading in [
        "## 1. System Identity",
        "## 2. Governance Invariants",
        "## 3. Golden-Set Evaluation Scoreboard",
        "## 4. Citation-Gate Coverage",
        "## 5. Traceability: Policy Criteria → Tests",
        "## 6. Cost per Determination",
        "## 7. Limitations",
        "## 8. Generation Provenance",
    ]:
        assert heading in md, f"Missing section: {heading}"


def test_build_markdown_provenance_block_contains_sha():
    """The provenance block must record the exact git SHA used at generation."""
    md = build_markdown(
        sha="deadbeef",
        eval_data=FAKE_EVAL_DATA,
        cost_data=FAKE_COST_DATA,
        prompt_hashes=FAKE_PROMPT_HASHES,
        test_results=FAKE_TEST_RESULTS,
        test_run_overall=(92, 92),
    )
    assert "`deadbeef`" in md


def test_invariants_table_has_all_configured_rules():
    """The invariants table must have one row for every entry in _INVARIANT_CONFIG."""
    test_results = {cfg["test_file"]: (4, 4) for cfg in _INVARIANT_CONFIG}
    table = _section_invariants(test_results)
    for cfg in _INVARIANT_CONFIG:
        assert cfg["rule"] in table, f"Missing invariant row: {cfg['rule']}"


def test_invariants_table_shows_pass_status():
    """A test file returning (4, 4) must render as PASS in the table."""
    test_results = {cfg["test_file"]: (4, 4) for cfg in _INVARIANT_CONFIG}
    table = _section_invariants(test_results)
    assert "**PASS**" in table


def test_invariants_table_shows_fail_status():
    """A test file returning (3, 4) must render as FAIL in the table."""
    failing = {cfg["test_file"]: (3, 4) for cfg in _INVARIANT_CONFIG}
    table = _section_invariants(failing)
    assert "**FAIL**" in table


def test_invariants_table_shows_skipped_when_no_pytest():
    """A skipped run ((-1, -1)) must render as '(skipped)', not PASS or FAIL."""
    skipped = {cfg["test_file"]: (-1, -1) for cfg in _INVARIANT_CONFIG}
    table = _section_invariants(skipped)
    assert "(skipped)" in table
    assert "**PASS**" not in table
    assert "**FAIL**" not in table


def test_scoreboard_no_live_run_required_placeholder_when_data_present():
    """When eval data is available, no [LIVE RUN REQUIRED] placeholder appears."""
    section = _section_scoreboard(FAKE_EVAL_DATA, run_tests=False)
    assert "[LIVE RUN REQUIRED]" not in section


def test_scoreboard_live_run_required_placeholder_when_no_data():
    """When eval data is absent, the section must show [LIVE RUN REQUIRED]."""
    section = _section_scoreboard(None, run_tests=False)
    assert "[LIVE RUN REQUIRED]" in section


def test_scoreboard_shows_all_four_dimensions():
    section = _section_scoreboard(FAKE_EVAL_DATA, run_tests=False)
    for label in ["S1 Determination accuracy", "S2 Routing accuracy",
                  "S3 Criterion-level evidence accuracy", "S4 Citation validity"]:
        assert label in section, f"Missing dimension: {label}"


def test_cost_section_no_live_run_required_when_data_present():
    section = _section_cost(FAKE_COST_DATA)
    assert "[LIVE RUN REQUIRED]" not in section


def test_cost_section_live_run_required_when_no_data():
    section = _section_cost(None)
    assert "[LIVE RUN REQUIRED]" in section


def test_cost_section_shows_total():
    """Total cost row must appear — auditor needs cost-per-determination."""
    section = _section_cost(FAKE_COST_DATA)
    assert "TOTAL" in section
    assert "$0.36" in section


def test_traceability_table_has_all_criterion_ids():
    """Every criterion in _CRITERION_CONFIG must appear in the traceability table."""
    table = _section_traceability((92, 92))
    for c in _CRITERION_CONFIG:
        assert c["id"] in table, f"Missing criterion: {c['id']}"


def test_identity_section_shows_prompt_hashes():
    """Every LLM node's hash must appear in the identity section."""
    section = _section_identity(FAKE_SHA, FAKE_PROMPT_HASHES)
    for node, info in FAKE_PROMPT_HASHES.items():
        assert info["hash"] in section, f"Missing hash for {node}"


def test_render_html_produces_valid_html_shell():
    """Rendered HTML must have opening and closing body tags and a style block."""
    md = "# Title\n\nSome text with **bold** and `code`.\n"
    html = _render_html(md)
    assert "<!DOCTYPE html>" in html
    assert "<body>" in html
    assert "</body>" in html
    assert "<style>" in html


def test_render_html_table_becomes_table_element():
    """A GFM table in Markdown must produce a <table> element in HTML."""
    md = "| A | B |\n|---|---|\n| 1 | 2 |\n"
    html = _render_html(md)
    assert "<table>" in html
    assert "<th>A</th>" in html
    assert "<td>1</td>" in html


def test_render_html_fenced_code_block():
    md = "```python\nprint('hello')\n```\n"
    html = _render_html(md)
    assert "<pre>" in html
    assert "print(&#x27;hello&#x27;)" in html or "print('hello')" in html


def test_render_html_headings():
    md = "# H1\n## H2\n### H3\n"
    html = _render_html(md)
    assert "<h1>" in html
    assert "<h2>" in html
    assert "<h3>" in html

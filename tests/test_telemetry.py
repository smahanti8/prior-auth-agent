"""Unit tests for the telemetry module.

All tests run without an API key or LLM. They verify cost math, the
accumulator lifecycle, the contextvar wiring in _timed(), and rollup
arithmetic — the failure modes that won't show up in a live run.
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from prior_auth_agent import telemetry
from prior_auth_agent.telemetry import (
    NodeTelemetry,
    _lookup_pricing,
    _timed,
    cost_usd,
    drain,
    record_deterministic,
    record_llm,
    reset,
    rollup,
)


# ── helpers ────────────────────────────────────────────────────────────────────

def _fake_node(state):
    return state


def _raising_node(state):
    raise ValueError("boom")


# ── pricing math ───────────────────────────────────────────────────────────────


def test_cost_usd_input_tokens_only():
    # 1M input tokens on Opus at $15/MTok = $15.00
    assert cost_usd("claude-opus-4-8", 1_000_000, 0) == pytest.approx(15.00)


def test_cost_usd_output_tokens_only():
    # 1M output tokens on Opus at $75/MTok = $75.00
    assert cost_usd("claude-opus-4-8", 0, 1_000_000) == pytest.approx(75.00)


def test_cost_usd_haiku_cheaper_than_opus():
    tokens = {"input_tokens": 500_000, "output_tokens": 100_000}
    opus_cost = cost_usd("claude-opus-4-8",  tokens["input_tokens"], tokens["output_tokens"])
    haiku_cost = cost_usd("claude-haiku-4-5", tokens["input_tokens"], tokens["output_tokens"])
    assert haiku_cost < opus_cost


def test_cost_usd_unknown_model_returns_zero():
    # Unknown model must not raise; returns $0 so the pipeline doesn't crash on a
    # new model ID before _PRICING is updated.
    assert cost_usd("claude-unknown-99", 1_000_000, 1_000_000) == 0.0


def test_lookup_pricing_versioned_suffix():
    # claude-haiku-4-5-20251001 must resolve to the claude-haiku-4-5 entry.
    # This is the failure mode when Anthropic releases a new dated model ID.
    in_p, out_p = _lookup_pricing("claude-haiku-4-5-20251001")
    expected_in, expected_out = _lookup_pricing("claude-haiku-4-5")
    assert in_p == expected_in
    assert out_p == expected_out


def test_lookup_pricing_exact_match():
    in_p, _ = _lookup_pricing("claude-opus-4-8")
    assert in_p == pytest.approx(15.00)


# ── accumulator lifecycle ──────────────────────────────────────────────────────


def setup_function():
    reset()


def test_record_llm_appends_to_accumulator():
    telemetry._current_node.set("criteria_mapper")
    record_llm(input_tokens=500, output_tokens=100, model="claude-opus-4-8", latency_ms=1200.0)
    records = drain()
    assert len(records) == 1
    assert records[0].node_name == "criteria_mapper"
    assert records[0].node_type == "llm"
    assert records[0].input_tokens == 500
    assert records[0].output_tokens == 100
    assert records[0].cost_usd > 0


def test_record_deterministic_appends_zero_cost():
    record_deterministic("intake", 2.5)
    records = drain()
    assert len(records) == 1
    assert records[0].node_type == "deterministic"
    assert records[0].cost_usd == 0.0
    assert records[0].input_tokens is None
    assert records[0].model is None


def test_drain_clears_accumulator():
    record_deterministic("intake", 1.0)
    drain()
    assert drain() == []


def test_reset_clears_accumulator():
    record_deterministic("intake", 1.0)
    reset()
    assert drain() == []


def test_multiple_records_accumulate():
    record_deterministic("intake", 1.0)
    record_deterministic("eligibility", 0.5)
    records = drain()
    assert len(records) == 2
    assert [r.node_name for r in records] == ["intake", "eligibility"]


# ── _timed() wrapper ───────────────────────────────────────────────────────────


def test_timed_deterministic_records_zero_cost():
    wrapped = _timed("intake", _fake_node)
    wrapped({"fhir_bundle": {}, "cpt_code": "29881"})
    records = drain()
    assert len(records) == 1
    assert records[0].node_name == "intake"
    assert records[0].cost_usd == 0.0


def test_timed_sets_current_node_contextvar():
    """The contextvar must be set to the node name while the wrapped function runs."""
    captured = []

    def probe_node(state):
        captured.append(telemetry._current_node.get())
        return state

    wrapped = _timed("criteria_mapper", probe_node)
    wrapped({})
    assert captured == ["criteria_mapper"]


def test_timed_resets_contextvar_after_call():
    original = telemetry._current_node.get()
    wrapped = _timed("evidence_extractor", _fake_node)
    wrapped({})
    assert telemetry._current_node.get() == original


def test_timed_resets_contextvar_on_exception():
    original = telemetry._current_node.get()
    wrapped = _timed("determination", _raising_node)
    with pytest.raises(ValueError):
        wrapped({})
    assert telemetry._current_node.get() == original


def test_timed_llm_node_does_not_double_record():
    """LLM nodes record via record_llm() inside structured_call — _timed() must
    not also call record_deterministic() for them."""
    # Simulate what happens when an LLM node runs: record_llm() is called
    # directly (as if structured_call fired), then _timed() finishes.
    # The accumulator should have exactly one record, not two.
    def llm_node_sim(state):
        # structured_call would fire record_llm internally
        record_llm(100, 50, "claude-opus-4-8", 3000.0)
        return state

    wrapped = _timed("criteria_mapper", llm_node_sim)
    wrapped({})
    records = drain()
    # Only the one LLM record — no extra deterministic record
    assert len(records) == 1
    assert records[0].node_type == "llm"


# ── rollup arithmetic ──────────────────────────────────────────────────────────


def test_rollup_averages_tokens_and_cost():
    records = [
        NodeTelemetry("criteria_mapper", "llm", 400, 100, "claude-opus-4-8", 1000.0,
                      cost_usd("claude-opus-4-8", 400, 100)),
        NodeTelemetry("criteria_mapper", "llm", 600, 200, "claude-opus-4-8", 2000.0,
                      cost_usd("claude-opus-4-8", 600, 200)),
    ]
    result = rollup(records)
    node = result["nodes"][0]
    assert node["avg_input_tokens"] == 500
    assert node["avg_output_tokens"] == 150
    assert node["avg_latency_ms"] == pytest.approx(1500.0)


def test_rollup_total_cost_is_sum_not_average():
    records = [
        NodeTelemetry("criteria_mapper", "llm", 1_000_000, 0, "claude-opus-4-8", 1000.0, 15.0),
        NodeTelemetry("evidence_extractor", "llm", 1_000_000, 0, "claude-opus-4-8", 2000.0, 15.0),
    ]
    result = rollup(records)
    assert result["total_cost_usd"] == pytest.approx(30.0)


def test_rollup_projections():
    # $1.00 per determination → $1,000 at 1K, $100,000 at 100K, $1,000,000 at 1M
    records = [
        NodeTelemetry("criteria_mapper", "llm", None, None, None, 0.0, 1.0),
    ]
    proj = rollup(records)["projections"]
    assert proj["1k_per_month"] == pytest.approx(1_000.0)
    assert proj["100k_per_month"] == pytest.approx(100_000.0)
    assert proj["1m_per_month"] == pytest.approx(1_000_000.0)


def test_rollup_deterministic_nodes_contribute_zero_cost():
    records = [
        NodeTelemetry("intake", "deterministic", None, None, None, 2.0, 0.0),
        NodeTelemetry("eligibility", "deterministic", None, None, None, 1.0, 0.0),
    ]
    result = rollup(records)
    assert result["total_cost_usd"] == 0.0
    assert result["projections"]["1m_per_month"] == 0.0

"""Unit tests for otel_tracing.traced().

All tests run without an API key, an LLM, or a network call: spans are
captured with opentelemetry's InMemorySpanExporter. They verify the span
attribute contract, and that behavior falls back to _timed() unchanged when
OpenTelemetry is unavailable.
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

from prior_auth_agent import otel_tracing, telemetry
from prior_auth_agent.telemetry import record_llm

@pytest.fixture(autouse=True)
def _reset_telemetry():
    telemetry.reset()
    yield
    telemetry.reset()


@pytest.fixture
def exporter():
    """Fresh TracerProvider + InMemorySpanExporter, installed as the global provider.

    opentelemetry.trace.set_tracer_provider() is a set-once gate (guards
    against accidental overrides in production code) — tests need to reset
    both the stored provider and its Once gate to install a fresh one per test.
    """
    exp = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exp))
    trace._TRACER_PROVIDER = None
    trace._TRACER_PROVIDER_SET_ONCE._done = False
    trace.set_tracer_provider(provider)
    yield exp
    trace._TRACER_PROVIDER = None
    trace._TRACER_PROVIDER_SET_ONCE._done = False


def test_traced_emits_span_with_expected_attributes(exporter):
    def deterministic_node(state):
        return {**state, "case_id": "pa-test"}

    wrapped = otel_tracing.traced("intake", deterministic_node)
    wrapped({"case_id": "pa-test"})

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    attrs = spans[0].attributes
    assert attrs["prior_auth.case_id"] == "pa-test"
    assert attrs["prior_auth.node.type"] == "deterministic"
    assert attrs["prior_auth.node.cost_usd"] == 0.0
    assert "prior_auth.node.latency_ms" in attrs


def test_traced_llm_node_span_carries_model_and_tokens(exporter):
    def llm_node_sim(state):
        record_llm(100, 50, "claude-opus-4-8", 3000.0)
        return {**state, "case_id": "pa-test"}

    wrapped = otel_tracing.traced("criteria_mapper", llm_node_sim)
    wrapped({"case_id": "pa-test"})

    attrs = exporter.get_finished_spans()[0].attributes
    assert attrs["prior_auth.node.model"] == "claude-opus-4-8"
    assert attrs["prior_auth.node.input_tokens"] == 100
    assert attrs["prior_auth.node.output_tokens"] == 50


def test_traced_falls_back_to_timed_when_otel_unavailable(monkeypatch, exporter):
    monkeypatch.setattr(otel_tracing, "OTEL_AVAILABLE", False)

    def deterministic_node(state):
        return {**state, "case_id": "pa-test"}

    wrapped = otel_tracing.traced("intake", deterministic_node)
    wrapped({"case_id": "pa-test"})

    # No span emitted, but the underlying telemetry accumulator still recorded it.
    assert len(exporter.get_finished_spans()) == 0
    records = telemetry.drain()
    assert len(records) == 1
    assert records[0].node_name == "intake"

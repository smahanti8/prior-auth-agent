"""Unit tests for otel_tracing.traced().

All tests run without an API key, an LLM, or a network call: spans are
captured with opentelemetry's InMemorySpanExporter. They verify the span
attribute contract and, critically, that chart content can never reach a
span attribute, event, or status — on the success path or the exception path.
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

MARKER = "ZZZ-DISTINCTIVE-CHART-MARKER-42"


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


def _all_text(spans) -> str:
    """Flatten every attribute/event/status value across spans into one string."""
    chunks = []
    for span in spans:
        chunks.append(str(span.attributes))
        if span.status.description:
            chunks.append(span.status.description)
        for event in span.events:
            chunks.append(event.name)
            chunks.append(str(event.attributes))
    return "\n".join(chunks)


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


def test_traced_marker_never_appears_in_span(exporter):
    # An LLM node name with no record_llm() call records nothing, so no span
    # attributes would be built and this test would pass vacuously (found by
    # mutation: adding str(result) to the span still passed). Use an LLM
    # node that does record a call, and assert attributes really were set.
    def node_with_chart_content(state):
        record_llm(10, 5, "claude-opus-4-8", 100.0)
        return {"case_id": "pa-test", "chart_note": MARKER}

    wrapped = otel_tracing.traced("evidence_extractor", node_with_chart_content)
    result = wrapped({"case_id": "pa-test"})

    spans = exporter.get_finished_spans()
    assert result["chart_note"] == MARKER  # sanity: the node itself still returns it
    assert "prior_auth.node.model" in spans[0].attributes  # non-vacuity guard
    assert MARKER not in _all_text(spans)


def test_traced_exception_does_not_leak_via_span_status_or_event(exporter):
    def raising_node(state):
        raise ValueError(f"failed while processing chart: {MARKER}")

    wrapped = otel_tracing.traced("determination", raising_node)
    with pytest.raises(ValueError):
        wrapped({"case_id": "pa-test"})

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].status.status_code == trace.StatusCode.ERROR
    assert MARKER not in _all_text(spans)


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

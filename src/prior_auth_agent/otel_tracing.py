"""Optional OpenTelemetry span emission for the per-node telemetry in telemetry.py.

Traces are an additional sink, not a replacement: telemetry._timed() and the
audit-log write path in graph.py are unchanged. When opentelemetry is not
installed, or no TracerProvider has been configured by the caller, traced()
behaves exactly like telemetry._timed() — the OTel API's default global
tracer is a no-op, so this is "off by default" without needing our own flag.
"""

from __future__ import annotations

from typing import Callable

from . import telemetry
from .telemetry import _timed

try:
    from opentelemetry import trace

    OTEL_AVAILABLE = True
except ImportError:
    OTEL_AVAILABLE = False


def enable_console_tracing() -> None:
    """Install a TracerProvider that prints spans to stdout.

    Call explicitly (e.g. graph.py's __main__) — never at import time, so
    importing this module never has side effects on the global tracer and
    never fights with a test's own TracerProvider.
    """
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor

    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
    trace.set_tracer_provider(provider)


def _span_attributes(rec: telemetry.NodeTelemetry, case_id) -> dict:
    attrs: dict = {
        "prior_auth.case_id": case_id or "unknown",
        "prior_auth.node.type": rec.node_type,
        "prior_auth.node.latency_ms": rec.latency_ms,
        "prior_auth.node.cost_usd": rec.cost_usd,
    }
    if rec.model is not None:
        attrs["prior_auth.node.model"] = rec.model
    if rec.input_tokens is not None:
        attrs["prior_auth.node.input_tokens"] = rec.input_tokens
    if rec.output_tokens is not None:
        attrs["prior_auth.node.output_tokens"] = rec.output_tokens
    return attrs


def traced(name: str, fn: Callable) -> Callable:
    """Drop-in replacement for telemetry._timed: same behavior, plus a span.

    Span attributes are built only from the NodeTelemetry record _timed()
    already produced (node type, latency, model, tokens, cost) plus the
    pseudonymous case_id already written to the audit log — never from
    state/result content, so chart data structurally cannot reach a span.

    record_exception/set_status_on_exception default to True in OTel and
    would attach str(exception) to the span — evidence_extractor and
    determination errors can carry FHIR fragments (see README.md, "What
    must never enter logs or error traces"), so both are disabled here and
    the exception propagates with a static error status instead.
    """
    timed_fn = _timed(name, fn)
    if not OTEL_AVAILABLE:
        return timed_fn

    tracer = trace.get_tracer("prior_auth_agent")

    def wrapper(state):
        with tracer.start_as_current_span(
            f"node.{name}", record_exception=False, set_status_on_exception=False
        ) as span:
            try:
                result = timed_fn(state)
            except Exception:
                span.set_status(trace.Status(trace.StatusCode.ERROR, "node_failed"))
                raise
            rec = telemetry.peek_last()
            case_id = result.get("case_id") or state.get("case_id")
            if rec is not None:
                span.set_attributes(_span_attributes(rec, case_id))
        return result

    wrapper.__name__ = name
    return wrapper

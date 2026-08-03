"""Per-node token, latency, and cost instrumentation for the prior-auth pipeline.

Usage pattern (managed by graph.py):
  1. graph.py's _timed() wrapper sets _current_node before each node call.
  2. structured_call() reads _current_node to label LLM telemetry records.
  3. structured_call() calls record_llm() after every API response.
  4. deterministic nodes are recorded by _timed() calling record_deterministic().
  5. graph.py's run() (or evals/run.py's run_case()) calls drain() after invoke().

The module-level accumulator is safe for one determination at a time (the
pipeline is synchronous). Concurrent use would require per-context accumulators.

PRICING: The _PRICING table must be kept current with published Anthropic
pricing at https://www.anthropic.com/pricing before using these figures for
financial decisions. The values below were sourced on 2026-07-28.
"""

from __future__ import annotations

import dataclasses
from contextvars import ContextVar
from typing import Optional


# ── Pricing lookup ─────────────────────────────────────────────────────────────
# (input_usd_per_mtok, output_usd_per_mtok)
# Matched by prefix so version-dated IDs (e.g. claude-haiku-4-5-20251001) resolve.

_PRICING: dict[str, tuple[float, float]] = {
    "claude-opus-5":       (15.00, 75.00),
    "claude-opus-4-8":     (15.00, 75.00),
    "claude-sonnet-5":      (3.00, 15.00),
    "claude-sonnet-4-6":    (3.00, 15.00),
    "claude-haiku-4-5":     (0.80,  4.00),
}

KNOWN_MODEL_IDS = list(_PRICING.keys())


def _lookup_pricing(model: str) -> tuple[float, float]:
    for key, prices in _PRICING.items():
        if model.startswith(key) or key.startswith(model):
            return prices
    return (0.0, 0.0)


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    in_p, out_p = _lookup_pricing(model)
    return (input_tokens / 1_000_000) * in_p + (output_tokens / 1_000_000) * out_p


# ── NodeTelemetry ──────────────────────────────────────────────────────────────


@dataclasses.dataclass
class NodeTelemetry:
    node_name: str
    node_type: str            # "llm" | "deterministic"
    input_tokens: Optional[int]
    output_tokens: Optional[int]
    model: Optional[str]
    latency_ms: float
    cost_usd: float

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


# ── Accumulators ──────────────────────────────────────────────────────────────

_current_node: ContextVar[str] = ContextVar("_current_node", default="unknown")

# Model override: set by tier_analysis.py to run a node on a different model.
# None means "use the default MODEL from config."
_current_model_override: ContextVar[Optional[str]] = ContextVar(
    "_current_model_override", default=None
)

_accumulator: list[NodeTelemetry] = []


def reset() -> None:
    _accumulator.clear()


def drain() -> list[NodeTelemetry]:
    result = list(_accumulator)
    _accumulator.clear()
    return result


def record_llm(
    input_tokens: int,
    output_tokens: int,
    model: str,
    latency_ms: float,
) -> None:
    node_name = _current_node.get()
    _accumulator.append(
        NodeTelemetry(
            node_name=node_name,
            node_type="llm",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            model=model,
            latency_ms=latency_ms,
            cost_usd=cost_usd(model, input_tokens, output_tokens),
        )
    )


def record_deterministic(node_name: str, latency_ms: float) -> None:
    _accumulator.append(
        NodeTelemetry(
            node_name=node_name,
            node_type="deterministic",
            input_tokens=None,
            output_tokens=None,
            model=None,
            latency_ms=latency_ms,
            cost_usd=0.0,
        )
    )


# ── Node timing wrapper ───────────────────────────────────────────────────────

# Nodes that record telemetry via record_llm() inside structured_call.
# _timed() skips the record_deterministic() call for these.
_LLM_NODES = frozenset({"criteria_mapper", "evidence_extractor", "determination"})


def _timed(name: str, fn):
    """Wrap a graph node to record wall-clock latency.

    Sets _current_node contextvar so structured_call can label LLM telemetry.
    For deterministic nodes, records a zero-cost entry explicitly — "most nodes
    are free" is part of the cost story, not an absence of data.
    """
    import time

    def wrapper(state):
        tok = _current_node.set(name)
        t0 = time.perf_counter()
        try:
            result = fn(state)
        finally:
            elapsed_ms = (time.perf_counter() - t0) * 1000
            _current_node.reset(tok)
        if name not in _LLM_NODES:
            record_deterministic(name, elapsed_ms)
        return result

    wrapper.__name__ = name
    return wrapper


# ── Rollup ────────────────────────────────────────────────────────────────────


def rollup(records: list[NodeTelemetry]) -> dict:
    """Compute per-node averages and per-determination totals.

    Used by evals/cost_report.py to produce the README table and
    cost-per-determination JSON artifact.
    """
    from collections import defaultdict

    by_node: dict[str, list[NodeTelemetry]] = defaultdict(list)
    for r in records:
        by_node[r.node_name].append(r)

    # Maintain pipeline execution order where possible
    pipeline_order = [
        "intake", "eligibility", "policy_rag", "criteria_mapper",
        "evidence_extractor", "citation_gate", "determination",
        "confidence_gate", "auto_decision", "hitl_enqueue",
        "reject", "citation_reject",
    ]
    ordered_keys = [k for k in pipeline_order if k in by_node] + [
        k for k in by_node if k not in pipeline_order
    ]

    nodes_out = []
    for node_name in ordered_keys:
        items = by_node[node_name]
        first = items[0]
        latencies = [i.latency_ms for i in items]
        in_toks = [i.input_tokens for i in items if i.input_tokens is not None]
        out_toks = [i.output_tokens for i in items if i.output_tokens is not None]
        costs = [i.cost_usd for i in items]

        nodes_out.append({
            "node_name": node_name,
            "node_type": first.node_type,
            "model": first.model,
            "n": len(items),
            "avg_latency_ms": round(sum(latencies) / len(latencies), 1),
            "avg_input_tokens": round(sum(in_toks) / len(in_toks)) if in_toks else None,
            "avg_output_tokens": round(sum(out_toks) / len(out_toks)) if out_toks else None,
            "avg_cost_usd": sum(costs) / len(costs),
        })

    total_cost = sum(r.cost_usd for r in records)
    total_latency = sum(r.latency_ms for r in records)

    return {
        "nodes": nodes_out,
        "total_cost_usd": total_cost,
        "total_latency_ms": round(total_latency, 1),
        "projections": {
            "1k_per_month":   round(total_cost * 1_000, 2),
            "100k_per_month": round(total_cost * 100_000, 2),
            "1m_per_month":   round(total_cost * 1_000_000, 2),
        },
    }

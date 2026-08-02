"""Recording infrastructure for golden eval cassettes.

Wraps structured_call to capture responses, prompt hashes, and API usage
(token counts + model) during a --live run.  After graph.invoke() completes,
save_cassette() writes the cassette JSON alongside the policy_chunks captured
from the final pipeline state.

Token counts are recorded so that replay mode can produce accurate cost reports
without re-calling the API — cost_report.py uses the stored counts.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from evals.fixtures import RECORDED_DIR, _fingerprint, _hash_string

# ── Build schema-fingerprint → node-name map at import time ───────────────────
# Each node uses a distinct schema so fingerprinting is unambiguous.

def _build_schema_map() -> dict[str, str]:
    from prior_auth_agent.nodes.criteria_mapper import SCHEMA as CM
    from prior_auth_agent.nodes.evidence_extractor import SCHEMA as EE
    from prior_auth_agent.nodes.determination import SCHEMA as DET
    return {
        _fingerprint(CM): "criteria_mapper",
        _fingerprint(EE): "evidence_extractor",
        _fingerprint(DET): "determination",
    }


SCHEMA_TO_NODE: dict[str, str] = _build_schema_map()


# ── Call capture ───────────────────────────────────────────────────────────────


class CallCapture:
    """Accumulates structured_call invocations during a live run."""

    def __init__(self) -> None:
        self._calls: list[dict] = []

    def add(
        self,
        system: str,
        schema: dict,
        response: dict,
        input_tokens: int,
        output_tokens: int,
        model: str,
    ) -> None:
        fp = _fingerprint(schema)
        node_name = SCHEMA_TO_NODE.get(fp, f"unknown_{fp}")
        self._calls.append({
            "node_name":         node_name,
            "schema_fingerprint": fp,
            "system_hash":       _hash_string(system),
            "response":          response,
            "input_tokens":      input_tokens,
            "output_tokens":     output_tokens,
            "model":             model,
        })

    def to_nodes_dict(self) -> dict[str, dict]:
        result: dict[str, dict] = {}
        for call in self._calls:
            result[call["node_name"]] = call
        return result


def make_recording_wrapper(capture: CallCapture):
    """Return a replacement for structured_call that records each call.

    Intercepts the real function before the node-level patches so that both
    the response AND the usage data (token counts, model) are captured.
    """
    from prior_auth_agent.llm import client, MODEL
    from prior_auth_agent import telemetry as _tel
    import json
    import time

    def wrapper(
        system: str,
        user_content: str,
        schema: dict[str, Any],
        max_tokens: int = 16000,
    ) -> dict[str, Any]:
        active_model = _tel._current_model_override.get() or MODEL
        t0 = time.perf_counter()
        with client.messages.stream(
            model=active_model,
            max_tokens=max_tokens,
            thinking={"type": "adaptive"},
            system=system,
            messages=[{"role": "user", "content": user_content}],
            output_config={"format": {"type": "json_schema", "schema": schema}},
        ) as stream:
            response = stream.get_final_message()
        _tel.record_llm(
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            model=response.model,
            latency_ms=(time.perf_counter() - t0) * 1000,
        )
        if response.stop_reason == "refusal":
            raise RuntimeError("Model refused the request; route case to human review.")
        text = next(b.text for b in response.content if b.type == "text")
        result = json.loads(text)
        capture.add(
            system=system,
            schema=schema,
            response=result,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            model=response.model,
        )
        return result

    return wrapper


# ── Cassette writer ────────────────────────────────────────────────────────────


def save_cassette(
    case_id: str,
    capture: CallCapture,
    policy_chunks: list[dict],
) -> Path:
    from prior_auth_agent.config import MODEL

    RECORDED_DIR.mkdir(parents=True, exist_ok=True)
    path = RECORDED_DIR / f"{case_id}.json"

    payload: dict = {
        "case_id":     case_id,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "model":       MODEL,
        "nodes": {
            "policy_rag": {"chunks": policy_chunks},
            **capture.to_nodes_dict(),
        },
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path

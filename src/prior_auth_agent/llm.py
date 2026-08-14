"""Shared Anthropic client and structured-output helper for graph nodes.

Backend selection (config.LLM_BACKEND):
  "anthropic" — direct Anthropic API, credentials from ANTHROPIC_API_KEY (default)
  "bedrock"   — Amazon Bedrock, credentials from AWS SDK credential chain (instance
                profile / environment / SSO). Use inside a BAA-covered AWS account.
                Requires: pip install prior-auth-agent[bedrock]
"""

import json
import time
from typing import Any

import anthropic

from . import config
from . import telemetry as _tel


def _make_client() -> anthropic.Anthropic | anthropic.AnthropicBedrock:
    if config.LLM_BACKEND == "bedrock":
        # AnthropicBedrock resolves AWS credentials from the SDK credential chain.
        # No ANTHROPIC_API_KEY is read or required on this path.
        return anthropic.AnthropicBedrock()
    return anthropic.Anthropic()


# Module-level singleton — constructed once at import time.
# LLM_BACKEND is read from config at startup; changing it at runtime has no effect.
client = _make_client()


def structured_call(
    system: str,
    user_content: str,
    schema: dict[str, Any],
    max_tokens: int = 16000,
) -> dict[str, Any]:
    """Call Claude with adaptive thinking and a JSON schema-constrained response.

    Returns the parsed JSON object. `output_config.format` guarantees the first
    text block is valid JSON matching the schema.

    Streams the response: at the token budgets these nodes use (up to 32k), the
    SDK requires streaming for requests that could exceed its 10-minute
    non-streaming ceiling. `get_final_message()` still returns the complete
    message, so callers see no difference.

    Telemetry: token counts and latency are recorded to the module-level
    accumulator in telemetry.py after every call. The node name is read from
    the _current_node contextvar set by graph.py's _timed() wrapper.
    """
    override = _tel._current_model_override.get()
    if override:
        active_model = override
    elif config.LLM_BACKEND == "bedrock":
        active_model = config.BEDROCK_MODEL_ID  # Bedrock ARN-format ID
    else:
        active_model = config.MODEL

    t0 = time.perf_counter()
    with client.messages.stream(
        model=active_model,
        max_tokens=max_tokens,
        thinking={"type": "adaptive"},
        system=system,
        messages=[{"role": "user", "content": user_content}],
        # NOTE: output_config.format (JSON schema constraint) is an Anthropic-platform
        # extension. Verify availability on Bedrock before using LLM_BACKEND=bedrock
        # in production; if unavailable, re-implement via the tools API on the Bedrock
        # path. Extended thinking is supported on Bedrock.
        output_config={
            "format": {"type": "json_schema", "schema": schema}
        },
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
    return json.loads(text)

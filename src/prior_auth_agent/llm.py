"""Shared Anthropic client and structured-output helper for graph nodes."""

import json
from typing import Any

import anthropic

from .config import MODEL

# Resolves credentials from ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN / `ant auth login`
client = anthropic.Anthropic()


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
    """
    with client.messages.stream(
        model=MODEL,
        max_tokens=max_tokens,
        thinking={"type": "adaptive"},
        system=system,
        messages=[{"role": "user", "content": user_content}],
        output_config={
            "format": {"type": "json_schema", "schema": schema}
        },
    ) as stream:
        response = stream.get_final_message()
    if response.stop_reason == "refusal":
        raise RuntimeError("Model refused the request; route case to human review.")
    text = next(b.text for b in response.content if b.type == "text")
    return json.loads(text)

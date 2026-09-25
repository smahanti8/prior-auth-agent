"""Tests for llm.structured_call's model-dependent thinking config.

Claude Haiku models reject thinking={"type": "adaptive"} — confirmed via a
live tier_analysis run that crashed with:
  anthropic.BadRequestError: 400 ... "adaptive thinking is not supported on
  this model"
structured_call must disable thinking for haiku-family models and keep it
adaptive for opus/sonnet, which do support it.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from prior_auth_agent import llm
from prior_auth_agent import telemetry as _tel


def _mock_stream(model: str):
    message = MagicMock()
    message.usage.input_tokens = 10
    message.usage.output_tokens = 5
    message.model = model
    message.stop_reason = "end_turn"
    message.content = [MagicMock(type="text", text='{"ok": true}')]

    stream = MagicMock()
    stream.__enter__ = MagicMock(return_value=stream)
    stream.__exit__ = MagicMock(return_value=False)
    stream.get_final_message = MagicMock(return_value=message)
    return stream


@pytest.mark.parametrize(
    "model,expected_thinking",
    [
        ("claude-haiku-4-5-20251001", {"type": "disabled"}),
        ("claude-haiku-4-5", {"type": "disabled"}),
        ("claude-opus-4-8", {"type": "adaptive"}),
        ("claude-sonnet-5", {"type": "adaptive"}),
    ],
)
def test_structured_call_thinking_config_matches_model(model, expected_thinking):
    tok = _tel._current_model_override.set(model)
    try:
        with patch.object(
            llm.client.messages, "stream", return_value=_mock_stream(model)
        ) as mock_stream:
            llm.structured_call(system="s", user_content="u", schema={"type": "object"})
        assert mock_stream.call_args.kwargs["thinking"] == expected_thinking
    finally:
        _tel._current_model_override.reset(tok)

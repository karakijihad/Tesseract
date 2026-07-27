"""2026-07-09 backend-halt diagnosis — Responses API call_id corruption.

Live log (both nano + mini fallbacks, → whole turn dies):
`OpenAI Responses error: 400 - No tool call found for function call output
with call_id call_XXXX.`

The Responses API rejects a `function_call_output` item whose `call_id` has
no matching `function_call` in the same input. History trimming (prompt
budget) or a tool-loop-cap reset can drop the assistant `function_call`
message while keeping the tool-result (`role: "tool"`) message, leaving an
orphan output. `_to_responses_input` must strip those orphans so the request
stays well-formed regardless of how history got trimmed.
"""

from __future__ import annotations

from tesseract.kernel.adapters.openai import OpenAIAdapter


def _adapter() -> OpenAIAdapter:
    # AsyncOpenAI constructs offline; no network call is made by
    # `_to_responses_input`, which is a pure history→input translator.
    return OpenAIAdapter(
        api_key="test-key",
        base_url="https://example.invalid/v1",
        timeout=1.0,
        max_retries=0,
    )


def _outputs(items: list[dict]) -> list[dict]:
    return [it for it in items if it.get("type") == "function_call_output"]


def _calls(items: list[dict]) -> list[dict]:
    return [it for it in items if it.get("type") == "function_call"]


def test_orphan_tool_output_is_dropped() -> None:
    """A tool result whose paired assistant function_call was trimmed away
    must NOT emit a `function_call_output` — it would 400 the request."""
    history = [
        {"role": "user", "content": "hi"},
        # NOTE: the assistant message that issued call_orphan is gone
        # (trimmed / reset) — only its tool result survives.
        {"role": "tool", "tool_call_id": "call_orphan", "content": "stale result"},
    ]
    _instructions, items = _adapter()._to_responses_input(history)
    orphan = [o for o in _outputs(items) if o.get("call_id") == "call_orphan"]
    assert orphan == [], "orphan function_call_output must be stripped"


def test_paired_tool_output_survives() -> None:
    """A well-formed call/result pair must round-trip untouched."""
    history = [
        {"role": "user", "content": "weather?"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_ok",
                    "function": {"name": "get_weather", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_ok", "content": "sunny"},
    ]
    _instructions, items = _adapter()._to_responses_input(history)
    assert [c["call_id"] for c in _calls(items)] == ["call_ok"]
    assert [o["call_id"] for o in _outputs(items)] == ["call_ok"]


def test_mixed_paired_and_orphan() -> None:
    """When both a valid pair and an orphan output are present, only the
    orphan is stripped; the valid pair survives intact."""
    history = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "call_ok", "function": {"name": "f", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "tool_call_id": "call_ok", "content": "ok result"},
        {"role": "tool", "tool_call_id": "call_gone", "content": "orphan result"},
    ]
    _instructions, items = _adapter()._to_responses_input(history)
    assert [o["call_id"] for o in _outputs(items)] == ["call_ok"]

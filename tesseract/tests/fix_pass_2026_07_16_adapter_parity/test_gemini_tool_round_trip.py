"""2026-07-16 adapter-parity fix — Gemini tool-loop history conversion.

ChatSession keeps history in OpenAI shape. Before this fix Gemini's
`_split_messages` leaked `role:"tool"` results into user turns as plain
text and dropped assistant `tool_calls` entirely, so multi-step tool
loops degraded. Now: `function_call` parts on model turns,
`function_response` parts (keyed by function NAME) on user turns.
"""

from __future__ import annotations

from tesseract.kernel.adapters.gemini import GeminiAdapter


def _adapter() -> GeminiAdapter:
    # _split_messages is a pure translator — no SDK client needed.
    return GeminiAdapter.__new__(GeminiAdapter)


def test_tool_round_trip_converts_to_gemini_shape() -> None:
    msgs = [
        {"role": "user", "content": "time in Tokyo?"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "get_time", "arguments": '{"city": "Tokyo"}'}},
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "09:00 JST"},
    ]
    _sys, contents = _adapter()._split_messages(msgs)
    assert contents[1]["role"] == "model"
    fc = contents[1]["parts"][0]["function_call"]
    assert fc["name"] == "get_time"
    assert fc["args"] == {"city": "Tokyo"}
    assert fc["id"] == "c1"
    fr = contents[2]["parts"][0]["function_response"]
    assert contents[2]["role"] == "user"
    assert fr["name"] == "get_time"
    assert fr["id"] == "c1"  # id pairs response↔call
    assert fr["response"]["output"] == "09:00 JST"


def test_parallel_same_name_calls_keep_distinct_ids() -> None:
    """Two calls to the SAME function in one turn must not cross-wire:
    each function_response carries the id of its own call."""
    msgs = [
        {"role": "user", "content": "read both"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "file_read", "arguments": '{"file_path": "a.py"}'}},
                {"id": "c2", "type": "function",
                 "function": {"name": "file_read", "arguments": '{"file_path": "b.py"}'}},
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "AAA"},
        {"role": "tool", "tool_call_id": "c2", "content": "BBB"},
    ]
    _sys, contents = _adapter()._split_messages(msgs)
    calls = [p["function_call"] for p in contents[1]["parts"]]
    assert [c["id"] for c in calls] == ["c1", "c2"]
    responses = [c["parts"][0]["function_response"] for c in contents[2:]]
    assert [(r["id"], r["response"]["output"]) for r in responses] == [
        ("c1", "AAA"), ("c2", "BBB"),
    ]


def test_orphan_tool_result_dropped() -> None:
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "tool", "tool_call_id": "gone", "content": "stale"},
    ]
    _sys, contents = _adapter()._split_messages(msgs)
    assert contents == [{"role": "user", "parts": [{"text": "hi"}]}]


def test_reasoning_marker_messages_skipped() -> None:
    msgs = [
        {"_reasoning": True, "id": "r1", "encrypted_content": "blob"},
        {"role": "user", "content": "hi"},
    ]
    _sys, contents = _adapter()._split_messages(msgs)
    assert contents == [{"role": "user", "parts": [{"text": "hi"}]}]

from tesseract.orchestrator.tars_controller.interactive.stream_parser import (
    ClaudeTurnAccumulator,
)

def _events():
    return [
        {"type": "system", "subtype": "init", "session_id": "sess-abc"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Hello "}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "world"}]}},
        {"type": "result", "subtype": "success", "result": "Hello world",
         "usage": {"input_tokens": 10, "output_tokens": 2}},
    ]

def test_accumulates_text_and_session_id():
    acc = ClaudeTurnAccumulator()
    for ev in _events():
        acc.feed(ev)
    assert acc.session_id == "sess-abc"
    assert acc.done is True
    assert acc.result_text == "Hello world"
    assert acc.usage == {"input_tokens": 10, "output_tokens": 2}
    assert acc.is_error is False

def test_error_result_sets_flag():
    acc = ClaudeTurnAccumulator()
    acc.feed({"type": "result", "subtype": "error_max_turns", "is_error": True, "result": "boom"})
    assert acc.done is True
    assert acc.is_error is True
    assert acc.result_text == "boom"

def test_text_fallback_when_no_result_field():
    acc = ClaudeTurnAccumulator()
    acc.feed({"type": "assistant", "message": {"content": [{"type": "text", "text": "partial"}]}})
    acc.feed({"type": "result", "subtype": "success"})
    assert acc.result_text == "partial"
    assert acc.done is True

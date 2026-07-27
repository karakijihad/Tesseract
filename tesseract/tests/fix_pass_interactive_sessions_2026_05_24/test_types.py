from tesseract.orchestrator.tars_controller.interactive.types import (
    TurnResult, SessionStatus,
)

def test_turn_result_defaults():
    tr = TurnResult(handle="h1", target="claude", turn_index=0, result_text="hi")
    assert tr.status is SessionStatus.DONE
    assert tr.is_error is False
    assert tr.usage == {}

def test_turn_result_error():
    tr = TurnResult(
        handle="h1", target="claude", turn_index=1, result_text="",
        status=SessionStatus.ERROR, is_error=True,
    )
    assert tr.is_error is True
    assert tr.status is SessionStatus.ERROR

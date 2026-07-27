"""Phase 3: `cost_delta` envelope shape produced by `make_cost_delta`.

Frontend `useCostStore.applyDelta` + `CostChip` lock the envelope to the
contract below. Breaking the wire format without updating the store is the
most common regression category for this surface, so one shape-fence test
lives right next to the ledger tests."""

from __future__ import annotations

from tesseract.brain.cost.ledger import BudgetState, CostEvent
from tesseract.mirror.server.envelope import make_cost_delta


def _event() -> CostEvent:
    return CostEvent(
        timestamp="2026-04-24T12:00:00Z",
        local_date="2026-04-24",
        role="chat_brain",
        model="gpt-5.4-nano",
        input_tokens=1000,
        output_tokens=500,
        cached_tokens=200,
        cost_usd=0.0123,
        daily_total_usd=0.0123,
        role_total_usd=0.0123,
    )


def _state(blocked: bool = False, warning: bool = False) -> BudgetState:
    return BudgetState(
        spent_usd=0.0123,
        warning_usd=1.0,
        cap_usd=3.0,
        role_spent_usd=0.0123,
        role_cap_usd=2.5,
        warning=warning,
        blocked=blocked,
    )


def test_cost_delta_envelope_shape() -> None:
    env = make_cost_delta("sess-abc", _event(), _state())

    assert env["type"] == "cost_delta"
    assert env["category"] == "cost"
    assert env["session_id"] == "sess-abc"
    assert "timestamp" in env

    data = env["data"]
    # `kind` is required on the wire (Phase 16 S3 — voice cost lane). Chat
    # rows tag as "chat"; voice rows synthesize role="voice_tts"|"voice_stt".
    assert data["kind"] == "chat"
    assert data["role"] == "chat_brain"
    assert data["model"] == "gpt-5.4-nano"
    assert data["cost_usd"] == 0.0123
    assert data["daily_total_usd"] == 0.0123
    assert data["role_total_usd"] == 0.0123

    state = data["state"]
    assert state["spent_usd"] == 0.0123
    assert state["warning_usd"] == 1.0
    assert state["cap_usd"] == 3.0
    assert state["role_spent_usd"] == 0.0123
    assert state["role_cap_usd"] == 2.5
    assert state["warning"] is False
    assert state["blocked"] is False


def test_cost_delta_envelope_preserves_null_role_cap() -> None:
    """A role without a sub-cap gets `role_cap_usd: null` on the wire — the
    frontend CostChip checks `role_cap_usd === null` to skip meter math."""
    state = BudgetState(
        spent_usd=0.1,
        warning_usd=1.0,
        cap_usd=3.0,
        role_spent_usd=0.1,
        role_cap_usd=None,
        warning=False,
        blocked=False,
    )
    env = make_cost_delta("sess-abc", _event(), state)
    assert env["data"]["state"]["role_cap_usd"] is None


def test_cost_delta_envelope_blocked_flag_propagates() -> None:
    """The sticky 'budget exhausted' toast fires off `state.blocked` — the
    flag must round-trip without reinterpretation."""
    env = make_cost_delta("sess-abc", _event(), _state(blocked=True, warning=True))
    assert env["data"]["state"]["blocked"] is True
    assert env["data"]["state"]["warning"] is True

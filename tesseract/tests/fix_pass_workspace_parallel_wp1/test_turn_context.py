"""WP-2-prep-3 — per-task turn context + envelope turn_id discriminator.

Two invariants the wiring must guarantee for parallel synthetic turns:
  1. `make_envelope` stamps `turn_id` from the ContextVar so concurrent
     turns can't share an envelope-routing identity.
  2. Synthetic turns get a `syn:<event_id>:<short>` prefix so the frontend
     can route their envelopes away from the chat conversation store.
"""

from __future__ import annotations

import asyncio

import pytest


@pytest.mark.asyncio
async def test_envelope_picks_up_turn_id_from_contextvar() -> None:
    from tesseract.mirror.server.envelope import make_envelope
    from tesseract.mirror.server.turn_context import current_turn_id

    token = current_turn_id.set("syn:evt_abc:0000a1b2")
    try:
        env = make_envelope("stream_text", "loop", "sid", {"delta": "hi"})
    finally:
        current_turn_id.reset(token)

    assert env["turn_id"] == "syn:evt_abc:0000a1b2"


@pytest.mark.asyncio
async def test_envelope_without_turn_id_omits_field() -> None:
    """Out-of-turn broadcasts (catchup, scheduler events) must not carry
    a stale turn_id from a prior turn."""
    from tesseract.mirror.server.envelope import make_envelope
    from tesseract.mirror.server.turn_context import current_turn_id

    # Default state — no turn active.
    assert current_turn_id.get() is None
    env = make_envelope("stream_text", "loop", "sid", {"delta": "hi"})
    assert "turn_id" not in env


@pytest.mark.asyncio
async def test_turn_scoped_envelope_types_stamp_turn_id() -> None:
    """Audit-fix M1+M2: stream_start and tool_status must be in the
    turn-scoped allowlist so their synthetic-turn instances get a `syn:`
    discriminator and the frontend dispatch guard can suppress them.
    Regression for the audit gap that would otherwise leak synthetic
    pulse activity to the chat surface under WP-2 parallel turns.
    """
    from tesseract.mirror.server.envelope import make_envelope
    from tesseract.mirror.server.turn_context import current_turn_id

    token = current_turn_id.set("syn:evt_abc:0000a1b2")
    try:
        for env_type in (
            "loop_start",
            "loop_end",
            "stream_start",
            "stream_text",
            "stream_tool_call_start",
            "stream_tool_call_end",
            "stream_tool_result",
            "stream_stop",
            "stream_error",
            "tool_ask",
            "tool_status",
            "spawn_done",
        ):
            env = make_envelope(env_type, "loop", "sid", {})
            assert env.get("turn_id") == "syn:evt_abc:0000a1b2", (
                f"{env_type} missed the turn_id stamp"
            )
    finally:
        current_turn_id.reset(token)


@pytest.mark.asyncio
async def test_out_of_turn_envelope_types_never_stamp_turn_id() -> None:
    """Broadcast envelope types (cost_delta, log_error, workspace_event_*,
    agenda/worker/governor state changes) are out-of-turn signals — they
    must NEVER carry turn_id even if a parent task context has one set.
    Otherwise the frontend dispatch guard would drop the operator's HUD
    cost chip / log row when triggered during a synthetic turn.
    """
    from tesseract.mirror.server.envelope import make_envelope
    from tesseract.mirror.server.turn_context import current_turn_id

    token = current_turn_id.set("syn:evt_abc:0000a1b2")
    try:
        # Sample several broadcast types — all must skip the stamp.
        for env_type in [
            "cost_delta",
            "cost_warning",
            "log_error",
            "workspace_event_appended",
            "workspace_comment_appended",
            "workspace_thread_pending",
            "entity_signals",
            "entity_state_set",
            "voice_state",
            "tts_chunk",
            "config_reloaded",
        ]:
            env = make_envelope(env_type, "session", "sid", {})
            assert "turn_id" not in env, f"{env_type} unexpectedly stamped"
    finally:
        current_turn_id.reset(token)


@pytest.mark.asyncio
async def test_contextvar_isolated_across_tasks() -> None:
    """Two concurrent tasks each set their own turn_id and never see
    each other's value. This is the asyncio.create_task semantics that
    underpins WP-2's parallel synthetic turns."""
    from tesseract.mirror.server.turn_context import current_turn_id

    seen: dict[str, str | None] = {}

    async def run(tag: str, sleep_s: float) -> None:
        token = current_turn_id.set(tag)
        try:
            await asyncio.sleep(sleep_s)
            seen[tag] = current_turn_id.get()
        finally:
            current_turn_id.reset(token)

    await asyncio.gather(
        run("A", 0.01),
        run("B", 0.005),
        run("C", 0.0),
    )
    assert seen == {"A": "A", "B": "B", "C": "C"}


@pytest.mark.asyncio
async def test_workspace_origin_contextvar_isolated() -> None:
    """Same isolation guarantee for current_workspace_origin."""
    from tesseract.mirror.server.turn_context import current_workspace_origin

    seen: dict[str, dict | None] = {}

    async def run(tag: str, origin: dict | None, sleep_s: float) -> None:
        token = current_workspace_origin.set(origin)
        try:
            await asyncio.sleep(sleep_s)
            seen[tag] = current_workspace_origin.get()
        finally:
            current_workspace_origin.reset(token)

    await asyncio.gather(
        run("chat", None, 0.01),
        run("syn", {"event_id": "evt_1", "comment_id": "cmt_1"}, 0.005),
    )
    assert seen == {
        "chat": None,
        "syn": {"event_id": "evt_1", "comment_id": "cmt_1"},
    }


@pytest.mark.asyncio
async def test_send_envelope_falls_back_when_lock_attr_missing() -> None:
    """Legacy test stubs without `ws_send_lock` must still pass the WS
    write through — production sessions have the lock, tests don't, both
    work."""
    from tesseract.mirror.server.session import send_envelope
    from types import SimpleNamespace

    sent: list[dict] = []

    class _WS:
        closed = False

        async def send_json(self, payload: dict) -> None:
            sent.append(payload)

    # No `ws_send_lock` attribute on this stub.
    session = SimpleNamespace(
        session_id="sid",
        ws=_WS(),
        event_log=[],
    )
    await send_envelope(session, {"type": "x", "data": {}})
    assert len(sent) == 1

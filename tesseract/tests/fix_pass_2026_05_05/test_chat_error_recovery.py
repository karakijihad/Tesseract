"""Layer 2 (2026-05-05) — `ChatSession.send()` must not silently terminate
on a `ChunkType.ERROR`. Instead:

1. Yield the ERROR for the UI red bubble (existing contract preserved).
2. Append a synthetic `role:system` message to history so TARS sees the
   error on the next iteration and can recover.
3. Continue the outer tool-loop, capped by
   `max_consecutive_adapter_errors` (per CLAUDE.md hard rule on retry
   loops).
4. Reset the breaker counter on any successful STOP.

Before this fix, an OpenAI 500 produced a red bubble and the turn ended;
TARS had no idea anything had failed. The operator had to paste the
error into chat for TARS to see it.
"""

from __future__ import annotations

from typing import Any, AsyncGenerator

import pytest

from tesseract.brain.chat import ChatSession
from tesseract.kernel.adapters.base import (
    AdapterOptions,
    ChunkType,
    ErrorKind,
    StreamChunk,
)


class _ScriptedAdapter:
    """Streams a scripted sequence of scenes, one scene per `stream()` call."""

    def __init__(self, scenes: list[list[StreamChunk]]) -> None:
        self.model = "scripted"
        self._scenes = list(scenes)
        self.calls = 0

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        options: AdapterOptions | None = None,
    ) -> AsyncGenerator[StreamChunk, None]:
        self.calls += 1
        if not self._scenes:
            raise AssertionError(f"stream() called {self.calls}× — no scenes left")
        for chunk in self._scenes.pop(0):
            yield chunk

    def count_tokens(self, messages: list[dict[str, Any]]) -> int:
        return 0

    async def check_available(self) -> bool:
        return True


def _ok(text: str = "ok") -> list[StreamChunk]:
    return [
        StreamChunk(type=ChunkType.TEXT, text=text),
        StreamChunk(type=ChunkType.STOP, stop_reason="end_turn"),
    ]


def _err(text: str = "boom") -> list[StreamChunk]:
    return [StreamChunk(
        type=ChunkType.ERROR, error=text, error_kind=ErrorKind.TRANSIENT,
    )]


def _err_warning(text: str) -> list[StreamChunk]:
    return [StreamChunk(
        type=ChunkType.ERROR, error=text, raw={"severity": "warning"},
    )]


def _err_soft(text: str) -> list[StreamChunk]:
    """Post-commit soft error — FallbackAdapter tags these so the chat
    layer can pass them through without bumping the breaker."""
    return [StreamChunk(
        type=ChunkType.ERROR,
        error=text,
        error_kind=ErrorKind.TRANSIENT,
        raw={
            "severity": "soft",
            "kind": "post_commit_partial",
            "model": "scripted",
            "chain_index": 0,
            "provider_error": text,
        },
    )]


# Caps now live in `roles.yaml::roles.chat_brain.{tool_iteration_cap,
# consecutive_error_cap}` (boot.ChatBrainConfig forwards them). Tests pick
# their own values to drive the assertions; production wiring uses the YAML.
_TEST_TOOL_CAP = 25
_TEST_BREAKER_CAP = 3


def _session(adapter: object, breaker_cap: int = _TEST_BREAKER_CAP) -> ChatSession:
    return ChatSession(
        adapter=adapter,
        system_prompt="sys",
        max_tool_iterations=_TEST_TOOL_CAP,
        max_consecutive_adapter_errors=breaker_cap,
    )


@pytest.mark.asyncio
async def test_error_then_recovery_writes_system_message_and_retries() -> None:
    """ERROR on iteration 0 → system note appended to history → adapter
    re-enters and succeeds on iteration 1. send() yields both the ERROR
    and the recovered TEXT."""
    adapter = _ScriptedAdapter(scenes=[_err("openai 500"), _ok("recovered")])
    session = _session(adapter)

    chunks = [c async for c in session.send("hi")]

    assert adapter.calls == 2
    err_chunks = [c for c in chunks if c.type == ChunkType.ERROR]
    text_chunks = [c for c in chunks if c.type == ChunkType.TEXT]
    assert len(err_chunks) == 1
    assert "openai 500" in (err_chunks[0].error or "")
    assert any(c.text == "recovered" for c in text_chunks)

    # History ordering: user → system note → assistant("recovered").
    roles = [m["role"] for m in session.history if not m.get("_reasoning")]
    assert roles[0] == "user"
    assert roles[1] == "system"
    assert "[chat_brain error]" in session.history[1]["content"]
    assert "openai 500" in session.history[1]["content"]
    assert roles[-1] == "assistant"
    # Counter reset on successful STOP so a later error starts fresh.
    assert session._consecutive_adapter_errors == 0


@pytest.mark.asyncio
async def test_three_consecutive_errors_trip_circuit_breaker() -> None:
    """N=session.max_consecutive_adapter_errors errors in a row → final ERROR
    yielded with `severity=warning` and `reason=consecutive_adapter_errors`,
    send() returns. No infinite loop."""
    cap = _TEST_BREAKER_CAP
    scenes = [_err(f"err {i}") for i in range(cap + 5)]
    adapter = _ScriptedAdapter(scenes=scenes)
    session = _session(adapter)

    chunks = [c async for c in session.send("hi")]

    # The breaker fires after exactly `cap` attempts.
    assert adapter.calls == cap

    err_chunks = [c for c in chunks if c.type == ChunkType.ERROR]
    # `cap` adapter errors + 1 final breaker-tripped ERROR.
    assert len(err_chunks) == cap + 1
    final = err_chunks[-1]
    assert "circuit-breaker" in (final.error or "")
    raw = final.raw or {}
    assert raw.get("reason") == "consecutive_adapter_errors"
    assert raw.get("severity") == "warning"
    # Counter reset after tripping so the next send() starts clean.
    assert session._consecutive_adapter_errors == 0


@pytest.mark.asyncio
async def test_warning_severity_error_terminates_immediately() -> None:
    """Cost-cap / budget-exhausted errors carry `severity=warning` and
    must abort cleanly — no system-message injection, no retry, no
    bumping of the consecutive-error counter."""
    adapter = _ScriptedAdapter(scenes=[_err_warning("budget exhausted")])
    session = _session(adapter)

    chunks = [c async for c in session.send("hi")]

    assert adapter.calls == 1
    err_chunks = [c for c in chunks if c.type == ChunkType.ERROR]
    assert len(err_chunks) == 1
    # No system note injected for warning-severity errors.
    assert not any(
        m.get("role") == "system" and "[chat_brain error]" in (m.get("content") or "")
        for m in session.history
    )
    assert session._consecutive_adapter_errors == 0


@pytest.mark.asyncio
async def test_recovery_resets_breaker_between_errors() -> None:
    """Two errors with a successful STOP between them must NOT trip the
    breaker — a single recovery resets the counter."""
    adapter = _ScriptedAdapter(scenes=[
        _err("first"),
        _ok("recovered"),
        _err("second"),
        _ok("recovered again"),
    ])
    session = _session(adapter)

    # First send: error → recover.
    chunks1 = [c async for c in session.send("turn 1")]
    assert any(c.type == ChunkType.TEXT and c.text == "recovered" for c in chunks1)
    assert session._consecutive_adapter_errors == 0

    # Second send: error → recover. Counter must NOT have carried over.
    chunks2 = [c async for c in session.send("turn 2")]
    assert any(c.type == ChunkType.TEXT and c.text == "recovered again" for c in chunks2)
    assert session._consecutive_adapter_errors == 0

    # Total adapter calls: 2 errors + 2 recoveries = 4.
    assert adapter.calls == 4


@pytest.mark.asyncio
async def test_partial_text_persisted_with_interrupted_marker() -> None:
    """When the adapter yields some TEXT then ERROR, the partial text is
    appended to history with an `[interrupted by adapter error]` marker
    so the chat log preserves what TARS started saying."""
    adapter = _ScriptedAdapter(scenes=[
        [
            StreamChunk(type=ChunkType.TEXT, text="I was about to "),
            StreamChunk(type=ChunkType.ERROR, error="late 500", error_kind=ErrorKind.TRANSIENT),
        ],
        _ok("retry succeeded"),
    ])
    session = _session(adapter)

    [c async for c in session.send("hi")]

    # Roles: user → assistant(partial+marker) → system(error note) → assistant(recovered).
    roles_and_content = [
        (m["role"], m.get("content"))
        for m in session.history
        if not m.get("_reasoning")
    ]
    assert roles_and_content[0][0] == "user"
    assert roles_and_content[1][0] == "assistant"
    assert "I was about to " in roles_and_content[1][1]
    assert "[interrupted by adapter error]" in roles_and_content[1][1]
    assert roles_and_content[2][0] == "system"
    assert "[chat_brain error]" in roles_and_content[2][1]
    assert roles_and_content[3][0] == "assistant"
    assert roles_and_content[3][1] == "retry succeeded"


@pytest.mark.asyncio
async def test_soft_error_does_not_bump_breaker() -> None:
    """Post-commit soft errors (FallbackAdapter recovered via the next
    iteration) must NOT count toward `_consecutive_adapter_errors`. A
    breaker_cap=2 session must absorb three soft errors in a row provided
    each is followed by a successful recovery — they are streaming-layer
    flakes, not the kind of consecutive hard failures the breaker exists
    to gate. Each `send()` consumes one soft + one recovery scene via the
    outer-loop retry."""
    cap = 2
    adapter = _ScriptedAdapter(scenes=[
        _err_soft("openai 500 #1"), _ok("recovered #1"),
        _err_soft("openai 500 #2"), _ok("recovered #2"),
        _err_soft("openai 500 #3"), _ok("recovered #3"),
    ])
    session = _session(adapter, breaker_cap=cap)

    for i in range(1, 4):
        chunks = [c async for c in session.send(f"turn {i}")]
        assert any(
            c.type == ChunkType.TEXT and c.text == f"recovered #{i}" for c in chunks
        )
        # Counter stays at 0 — soft errors don't bump it.
        assert session._consecutive_adapter_errors == 0
    # 3 sends × (1 soft + 1 ok) = 6 adapter calls.
    assert adapter.calls == 6


@pytest.mark.asyncio
async def test_soft_errors_do_not_trip_breaker_even_back_to_back() -> None:
    """Even N consecutive soft errors with no successful recovery in
    between must NOT trip the breaker — the soft tag is the contract.
    The session terminates because the adapter eventually exhausts its
    scenes, but `_consecutive_adapter_errors` stays 0 throughout."""
    cap = 2
    # Soft, soft, soft, then OK so the loop terminates cleanly.
    adapter = _ScriptedAdapter(scenes=[
        _err_soft("flake 1"),
        _err_soft("flake 2"),
        _err_soft("flake 3"),
        _ok("finally"),
    ])
    session = _session(adapter, breaker_cap=cap)

    chunks = [c async for c in session.send("hi")]

    # All four adapter scenes consumed by a single send (each soft error
    # triggers one outer-loop retry).
    assert adapter.calls == 4
    # Breaker counter stayed at 0 the whole time — no trip ERROR.
    assert session._consecutive_adapter_errors == 0
    err_chunks = [c for c in chunks if c.type == ChunkType.ERROR]
    assert all(
        (c.raw or {}).get("reason") != "consecutive_adapter_errors"
        for c in err_chunks
    )
    assert any(
        c.type == ChunkType.TEXT and c.text == "finally" for c in chunks
    )


@pytest.mark.asyncio
async def test_reset_clears_breaker_counter() -> None:
    """`ChatSession.reset()` must zero the breaker counter alongside
    history — otherwise a session that was reset after near-tripping
    would trip on a single new error."""
    adapter = _ScriptedAdapter(scenes=[])
    session = _session(adapter)
    session._consecutive_adapter_errors = 2
    session.reset()
    assert session._consecutive_adapter_errors == 0

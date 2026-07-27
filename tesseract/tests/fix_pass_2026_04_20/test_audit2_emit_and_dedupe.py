"""Codex audit-2 residuals — emit import, subscriber dedupe gate, observer lock,
deque-slice regression from the simplifier pass.

- #1 (high): observer_consent._make_emit_fn called `to_envelope_data` without
  importing it → NameError on every background emit.
- #2 (med): ObserverSubscriber._run ignored ingest_memory_suggestion's bool
  return, so duplicates still reached the UI.
- #3 (med): concurrent producers (subscriber + PTY task) mutated shared
  Observer state without serialization.
- #4 (regression): Group H replaced `islice(self._transcript.chat_turns, start, None)`
  with `self._transcript.chat_turns[start:]`, but chat_turns is a deque and
  doesn't support slicing — the next live observe_incremental raises TypeError.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, AsyncGenerator

from tesseract.brain.chat import ChatSession
from tesseract.brain.memory_suggestion import MemoryPath, MemorySuggestion
from tesseract.brain.observer import Observer, ObserverConfig
from tesseract.brain.observer_budget import CircuitBreaker
from tesseract.brain.observer_subscriber import ObserverSubscriber
from tesseract.kernel.adapters.base import AdapterOptions, ChunkType, ModelAdapter, StreamChunk


async def test_audit2_1_emit_has_to_envelope_data_import() -> None:
    """Importing + invoking _make_emit_fn must not raise NameError."""
    from tesseract.mirror.server.routes import observer_consent

    assert hasattr(observer_consent, "to_envelope_data"), (
        "BUG (audit-2 #1): observer_consent does not expose to_envelope_data — "
        "the _make_emit_fn call site will raise NameError on every background emit"
    )

    sent: list[dict[str, Any]] = []

    class _FakeWS:
        closed = False

        async def send_json(self, payload):
            sent.append(payload)

    session = SimpleNamespace(ws=_FakeWS(), session_id="s1")
    emit = observer_consent._make_emit_fn(session)
    suggestion = MemorySuggestion(
        kind="remember",
        target=MemoryPath(path="x.md"),
        reason="r",
        confidence=0.9,
        observation_id="obs_emit_1",
    )
    # The bug-before-fix: this awaited call raised NameError.
    await emit(suggestion)
    assert sent, "emit() produced no envelope"
    env = sent[0]
    assert env["type"] == "memory_suggestion"
    assert env["data"]["observation_id"] == "obs_emit_1"


class _StubObserver:
    """Minimal stand-in that returns a fixed suggestion from observe_incremental."""

    def __init__(self, suggestion: MemorySuggestion) -> None:
        self._suggestion = suggestion
        self.calls = 0

    async def observe_incremental(self, new_turns, mode="meta"):
        self.calls += 1
        return self._suggestion


class _FakeChatSession:
    def __init__(self, accept: bool) -> None:
        self._accept = accept
        self.ingest_calls = 0

    def ingest_memory_suggestion(self, suggestion: MemorySuggestion) -> bool:
        self.ingest_calls += 1
        return self._accept


async def test_audit2_2_subscriber_skips_emit_on_dedupe() -> None:
    """When ingest_memory_suggestion returns False (observation_id already
    seen), the subscriber must NOT forward the suggestion to the UI emit."""
    suggestion = MemorySuggestion(
        kind="remember",
        target=MemoryPath(path="x.md"),
        reason="r",
        confidence=0.9,
        observation_id="obs_dup",
    )
    obs = _StubObserver(suggestion)
    sub = ObserverSubscriber(obs)
    chat = _FakeChatSession(accept=False)

    emit_count = 0

    async def _emit(s):
        nonlocal emit_count
        emit_count += 1

    sub.attach(chat, _emit)
    await sub._run([{"role": "user", "content": "hi"}])

    assert chat.ingest_calls == 1, "ingest not called"
    assert emit_count == 0, (
        f"BUG (audit-2 #2): dedupe returned False but subscriber still emitted "
        f"({emit_count} call(s)) — duplicate will reach the UI"
    )

    # And the inverse: when ingest accepts, emit must fire.
    chat_ok = _FakeChatSession(accept=True)
    emit_count = 0
    sub.attach(chat_ok, _emit)
    await sub._run([{"role": "user", "content": "hi2"}])
    assert emit_count == 1, "emit should fire when ingest accepts"


class _SlowAdapter(ModelAdapter):
    """Adapter that waits on an event so we can interleave two calls."""

    def __init__(self, release: asyncio.Event) -> None:
        self._release = release
        self.concurrent_peak = 0
        self._inflight = 0
        self._peak_lock = asyncio.Lock()

    async def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        options: AdapterOptions | None = None,
    ) -> AsyncGenerator[StreamChunk, None]:
        async with self._peak_lock:
            self._inflight += 1
            self.concurrent_peak = max(self.concurrent_peak, self._inflight)
        try:
            await self._release.wait()
            yield StreamChunk(type=ChunkType.TEXT, text="NONE")
            yield StreamChunk(type=ChunkType.STOP, stop_reason="end_turn", raw={"usage": {}})
        finally:
            async with self._peak_lock:
                self._inflight -= 1

    def count_tokens(self, messages):
        return 0

    async def check_available(self) -> bool:
        return True


async def test_audit2_3_observer_serializes_concurrent_calls() -> None:
    """Two concurrent observe_incremental() calls must not run the adapter
    stream at the same time — the observer's internal lock serializes them."""
    release = asyncio.Event()
    adapter = _SlowAdapter(release)
    cfg = ObserverConfig(
        model="fake", provider="fake",
        temperature=0.5, max_output_tokens=64,
        context_window=1024, timeout_seconds=10, max_retries=0,
    )
    observer = Observer.__new__(Observer)
    observer._adapter = adapter
    observer._config = cfg
    observer._agent_def = None  # type: ignore[assignment]
    from tesseract.brain.observation_transcript import ObservationTranscript
    observer._transcript = ObservationTranscript()
    observer._circuit_breaker = CircuitBreaker()
    observer._fires_total = 0
    observer._tokens_used_total = 0
    observer._last_fired_at = None
    observer._last_suggestion_observation_id = None
    observer._lock = asyncio.Lock()
    observer._cost_ledger = None

    # Need an agent_def so _compose_system_prompt doesn't NPE — stub it.
    class _Stub:
        name = "observer"

        def get_section(self, _):
            return "{transcript}\n{pty_context}\n{schema}\n{observation_id}"

    observer._agent_def = _Stub()

    async def _call(text):
        return await observer.observe_incremental(
            [{"role": "user", "content": text}]
        )

    t1 = asyncio.create_task(_call("a"))
    t2 = asyncio.create_task(_call("b"))
    await asyncio.sleep(0.05)  # give both tasks time to enter
    # If the lock works, only one stream is in-flight at a time.
    assert adapter.concurrent_peak <= 1, (
        f"BUG (audit-2 #3): observe_incremental not serialized — "
        f"adapter saw {adapter.concurrent_peak} concurrent streams"
    )
    release.set()
    await asyncio.gather(t1, t2)


async def test_audit2_4_deque_slice_regression() -> None:
    """Group H replaced islice(...) with a deque slice. deque doesn't support
    slicing, so observe_incremental raised TypeError on the second turn with
    a non-empty transcript. Verify the fix (list conversion) works."""
    release = asyncio.Event()
    release.set()  # don't block
    adapter = _SlowAdapter(release)
    cfg = ObserverConfig(
        model="fake", provider="fake",
        temperature=0.5, max_output_tokens=64,
        context_window=1024, timeout_seconds=10, max_retries=0,
    )
    observer = Observer.__new__(Observer)
    observer._adapter = adapter
    observer._config = cfg
    from tesseract.brain.observation_transcript import ObservationTranscript
    observer._transcript = ObservationTranscript()
    observer._circuit_breaker = CircuitBreaker()
    observer._fires_total = 0
    observer._tokens_used_total = 0
    observer._last_fired_at = None
    observer._last_suggestion_observation_id = None
    observer._lock = asyncio.Lock()
    observer._cost_ledger = None

    class _Stub:
        name = "observer"

        def get_section(self, _):
            return "{transcript}\n{pty_context}\n{schema}\n{observation_id}"

    observer._agent_def = _Stub()

    # Populate past DEFAULT_CONTEXT_TURNS so the window slice is non-trivial.
    for i in range(15):
        observer._transcript.append_chat_turns(
            [{"role": "user", "content": f"u{i}"},
             {"role": "assistant", "content": f"a{i}"}]
        )

    # Before the list(...) fix, this raised:
    # TypeError: sequence index must be integer, not 'slice'
    result = await observer.observe_incremental(
        [{"role": "user", "content": "new_turn"}]
    )
    # result is None (NONE suggestion), but the important thing is the
    # call returned without TypeError.
    assert result is None or isinstance(result, MemorySuggestion)

"""Audit-2 (2026-05-24) — follow-up regression suite.

Covers the Major findings the codex audit flagged on the shared
dispatcher / controller rollout:

* M2 — ControllerRuntime persists one ``ChatSession`` per controller
  ``session_id`` so conversation history survives across inputs.
* M3 — that ``ChatSession`` is constructed with the same runtime
  context Mirror plumbs (``ToolContext`` with workspace_root,
  session_id, tool_registry_provider; adapter options preserved).
* M4 — approval IPC rejects senders that aren't an interactive
  attachment to the target session, and every resolved approval
  (approve / deny / timeout) appends a closing ``PermissionRequestEvent``
  to the transcript.
* M5 — Mirror chat envelopes preserve tool-author metadata so
  ``start_controller_session`` results carry session_id / mode /
  ws_path / child_transcript_path through to the frontend.
* M6 — per-client outbound queues are bounded; an overflow detaches
  the offending writer instead of growing daemon memory unboundedly.

All tests run against a live :class:`ControllerDaemon` on tmp ports and
use the ``isolated_home`` fixture so production logs/state never see
test pollution (see ``conftest.py`` for the production-substrate guard).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, AsyncIterator

import pytest

from tesseract.orchestrator.tars_controller import auth as ctrl_auth
from tesseract.orchestrator.tars_controller.daemon import (
    ControllerDaemon,
    _OUTBOUND_QUEUE_MAX,
)
from tesseract.orchestrator.tars_controller.events import (
    PermissionRequestEvent,
)
from tesseract.orchestrator.tars_controller.ipc_client import (
    ControllerClient,
)
from tesseract.orchestrator.tars_controller.sessions import SessionRegistry
from tesseract.orchestrator.tars_controller.transcript import (
    TranscriptReader,
)


# ── shared fixtures ───────────────────────────────────────────────────


@pytest.fixture
async def live_daemon(isolated_home: Path) -> AsyncIterator[ControllerDaemon]:
    ctrl_auth.write_token(ctrl_auth.mint_token())
    daemon = ControllerDaemon(
        controller_id="ctrl-audit2-test",
        token=ctrl_auth.read_token() or "",
        registry=SessionRegistry(),
    )
    await daemon.start(host="127.0.0.1", port=0)
    try:
        yield daemon
    finally:
        await daemon.stop()


# ── M2 / M3 — ControllerRuntime session wiring ────────────────────────


def _runtime_with_stub_adapter() -> Any:
    """Build a :class:`ControllerRuntime` whose adapter / tool_registry /
    system_prompt are stubbed so we can poke ``make_dispatch_turn``
    without spinning the real chat brain stack."""
    from tesseract.scripts.tars_controller import ControllerRuntime

    runtime = ControllerRuntime()
    runtime.adapter = object()  # sentinel — never actually called
    runtime.tool_registry = object()
    runtime.system_prompt = "stub system prompt"
    runtime.adapter_options = None
    runtime.chat_brain_config = None
    return runtime


@pytest.mark.asyncio
async def test_chat_session_is_persisted_per_session_id(
    isolated_home: Path,
) -> None:
    """M2: a second turn against the same session_id must reuse the
    cached :class:`ChatSession`, not build a fresh one — otherwise
    ``history`` (and tool-loop state) is silently discarded."""
    runtime = _runtime_with_stub_adapter()

    from tesseract.orchestrator.tars_controller.sessions import (
        ControllerSessionRecord,
    )

    record = ControllerSessionRecord(
        session_id="sess-cache-1",
        controller_id="ctrl-test",
        mode="chat",
        origin="cli",
        transcript_path="ignored-in-this-test",
    )

    # Empty-iterator ChatSession stub keeps the test independent of the
    # real chat brain — the cache contract is independent of what the
    # session does on send().
    build_calls = {"n": 0}

    class _StubSession:
        async def send(self, *_args: Any, **_kwargs: Any):
            if False:
                yield None  # pragma: no cover

    def _stub_build(_record: Any, _daemon: Any) -> Any:
        build_calls["n"] += 1
        return _StubSession()

    runtime._build_chat_session = _stub_build  # type: ignore[assignment]

    class _NoopDaemon:
        async def append_event(self, *_: Any, **__: Any) -> int:
            return 0

    dispatch = runtime.make_dispatch_turn()
    daemon = _NoopDaemon()
    await dispatch(record, "first input", daemon)
    first = runtime._chat_sessions["sess-cache-1"]
    await dispatch(record, "second input", daemon)
    second = runtime._chat_sessions["sess-cache-1"]
    assert first is second
    assert build_calls["n"] == 1, (
        f"M2 regression: dispatch_turn rebuilt the ChatSession between "
        f"inputs (build_calls={build_calls['n']}) — history would be lost"
    )


@pytest.mark.asyncio
async def test_reload_invalidates_chat_session_cache(
    isolated_home: Path,
) -> None:
    """M2: a reload must drop cached sessions so the next turn picks
    up the freshly built adapter / tools / prompt."""
    runtime = _runtime_with_stub_adapter()
    runtime._chat_sessions["sess-keep"] = object()

    # Stub out the rebuild helpers — we only care that ``reload`` clears
    # the cache, not that the real adapter rebuild succeeds.
    runtime._rebuild_adapter = lambda: ([], [])  # type: ignore[assignment]
    runtime._rebuild_tools = lambda: ([], [])  # type: ignore[assignment]
    runtime._rebuild_prompt = lambda: ([], [])  # type: ignore[assignment]

    await runtime.reload("all")
    assert runtime._chat_sessions == {}, (
        "M2 regression: reload left stale ChatSession instances in the "
        "cache; next turn would use the old adapter / tool registry"
    )


@pytest.mark.asyncio
async def test_chat_session_built_with_full_tool_context(
    isolated_home: Path,
) -> None:
    """M3: the controller's ChatSession must carry a populated
    ``ToolContext`` (workspace_root, session_id, tool_registry_provider,
    ask_fn) — Mirror's wiring path is the reference."""
    runtime = _runtime_with_stub_adapter()
    sentinel_registry = object()
    runtime.tool_registry = sentinel_registry

    from tesseract.orchestrator.tars_controller.sessions import (
        ControllerSessionRecord,
    )

    record = ControllerSessionRecord(
        session_id="sess-ctx-1",
        controller_id="ctrl-test",
        mode="chat",
        origin="cli",
        transcript_path="ignored-in-this-test",
    )

    class _StubDaemon:
        async def request_permission(self, *_: Any, **__: Any) -> bool:
            return False

    session = runtime._build_chat_session(record, _StubDaemon())
    ctx = session.tool_context
    assert ctx.session_id == "sess-ctx-1"
    assert ctx.workspace_root  # not the empty-string default
    assert ctx.workspace_root != "."
    assert callable(ctx.tool_registry_provider)
    assert ctx.tool_registry_provider() is sentinel_registry
    assert callable(ctx.ask_fn)
    # Scheduler provider must be wired explicitly even though the
    # controller is a sibling process that returns None — Mirror's
    # factory plumbs the slot and tools key off callable presence to
    # distinguish "feature unavailable in this context" from "wiring
    # forgotten".
    assert callable(ctx.scheduler_provider)
    assert ctx.scheduler_provider() is None
    # CLI sink must be wired so chat tools that stream subprocess
    # output (delegate_claude, delegate_codex, bash_tool) emit
    # CliChunkEvents into the controller transcript instead of running
    # silently — matches the "see what claude is doing" affordance of
    # the bare claude CLI.
    assert callable(ctx.cli_sink)


# ── A1 — drop_session hook prunes the ChatSession cache ──────────────


@pytest.mark.asyncio
async def test_drop_session_hook_prunes_chat_session_cache(
    isolated_home: Path,
) -> None:
    """Audit-2 A1 — ``ControllerRuntime.drop_session(id)`` must remove
    the cached :class:`ChatSession` so a deleted session doesn't leak
    a dead entry until the next ``reload``. The hook is registered on
    the daemon as ``on_session_deleted`` (see ``run_controller`` in
    ``tesseract/scripts/tars_controller.py``)."""
    runtime = _runtime_with_stub_adapter()

    # Seed the cache with two sessions; drop one.
    sentinel_kept = object()
    sentinel_drop = object()
    runtime._chat_sessions["sess-keep"] = sentinel_kept
    runtime._chat_sessions["sess-drop"] = sentinel_drop

    await runtime.drop_session("sess-drop")

    assert "sess-drop" not in runtime._chat_sessions, (
        "A1 regression: drop_session left the dead entry behind"
    )
    assert runtime._chat_sessions.get("sess-keep") is sentinel_kept, (
        "A1 regression: drop_session removed the wrong entry"
    )

    # Idempotent — dropping an unknown session must not raise.
    await runtime.drop_session("sess-unknown")
    assert "sess-keep" in runtime._chat_sessions


@pytest.mark.asyncio
async def test_daemon_invokes_on_session_deleted_callback(
    isolated_home: Path,
) -> None:
    """Audit-2 A1 — ``_on_delete_session`` must call the runtime hook
    after the registry delete succeeds, so cache pruning runs even
    when the operator deletes a session that no longer has an active
    chat turn in flight."""
    from tesseract.orchestrator.tars_controller.daemon import (
        ControllerDaemon,
    )

    seen: list[str] = []

    async def _on_deleted(session_id: str) -> None:
        seen.append(session_id)

    ctrl_auth.write_token(ctrl_auth.mint_token())
    daemon = ControllerDaemon(
        controller_id="ctrl-a1-test",
        token=ctrl_auth.read_token() or "",
        registry=SessionRegistry(),
        on_session_deleted=_on_deleted,
    )
    await daemon.start(host="127.0.0.1", port=0)
    try:
        client = await ControllerClient.connect()
        try:
            attached = await client.new_session(
                title="a1-delete-test", mode="chat", origin="cli"
            )
            session_id = attached["session"]["session_id"]
            # Detach so the daemon doesn't refuse with session_attached.
            await client.detach(session_id)
            await client.delete_session(session_id)
            # Give the daemon a tick to invoke the callback.
            await asyncio.sleep(0.05)
        finally:
            await client.close()
    finally:
        await daemon.stop()

    assert seen == [session_id], (
        f"A1 regression: on_session_deleted hook not invoked; "
        f"seen={seen}, expected=[{session_id}]"
    )


@pytest.mark.asyncio
async def test_delete_session_cancels_pending_approvals(
    isolated_home: Path, live_daemon: ControllerDaemon,
) -> None:
    """Audit-2 A1 follow-up — deleting a session must cancel any
    in-flight ``request_permission`` future for that session. Without
    this an operator who deletes a session mid-ASK leaves the future
    dangling until its 300s default timeout, growing the daemon's
    ``_pending_approvals`` dict across many sessions."""
    operator = await ControllerClient.connect()
    try:
        attached = await operator.new_session(
            title="orphan-future-test", mode="chat", origin="cli"
        )
        session_id = attached["session"]["session_id"]

        # Inject a pending approval directly — we don't need a real
        # tool call to verify the eviction contract.
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        live_daemon._pending_approvals[(session_id, "tooluse-orphan")] = fut

        # Detach so delete is accepted, then issue the delete.
        await operator.detach(session_id)
        await operator.delete_session(session_id)
        await asyncio.sleep(0.05)
    finally:
        await operator.close()

    assert (session_id, "tooluse-orphan") not in live_daemon._pending_approvals, (
        "A1 follow-up regression: delete_session left the pending "
        "approval future in _pending_approvals"
    )
    assert fut.cancelled(), (
        "A1 follow-up regression: delete_session evicted the future "
        "without cancelling it — any awaiter would hang forever"
    )


# ── M4 — approval authorization + resolution events ───────────────────


@pytest.mark.asyncio
async def test_unauthorized_approval_is_rejected(
    isolated_home: Path, live_daemon: ControllerDaemon,
) -> None:
    """M4: an authenticated client that is NOT attached as an
    interactive client to the target session must not be able to
    resolve another session's pending ASK. The daemon must surface an
    ``unauthorized_approval`` error and leave the future pending."""
    # Mint a session via one client (the "operator" channel), but do
    # NOT keep that client attached — instead simulate a *different*
    # client trying to approve.
    operator = await ControllerClient.connect()
    try:
        attached = await operator.new_session(
            title="auth-test", mode="chat", origin="cli"
        )
        session_id = attached["session"]["session_id"]
        # Detach the operator so there's no interactive attachment.
        await operator.detach(session_id)
    finally:
        await operator.close()

    # Register a pending approval directly on the daemon (no real chat
    # brain involved). We start it as a task so it sits waiting on the
    # future while we send the unauthorized approval.
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    live_daemon._pending_approvals[(session_id, "tooluse-1")] = fut

    bystander = await ControllerClient.connect()
    try:
        await bystander.approval(session_id, "tooluse-1", approved=True)
        # Wait briefly for the daemon to push the error back.
        async def _read_error() -> dict[str, Any]:
            async for push in bystander.pushes():
                if push.get("event") == "error":
                    return push
                if push.get("event") == "ack":
                    pytest.fail(
                        "M4 regression: daemon ack'd an unauthorized approval"
                    )
            pytest.fail("daemon closed without responding to approval")

        push = await asyncio.wait_for(_read_error(), timeout=2.0)
        assert push["code"] == "unauthorized_approval"
    finally:
        await bystander.close()

    assert not fut.done(), (
        "M4 regression: unauthorized approval still resolved the pending "
        "future — any token-holder could approve another session's ASKs"
    )
    # Clean up so live_daemon.stop() doesn't have a dangling future.
    fut.cancel()
    live_daemon._pending_approvals.pop((session_id, "tooluse-1"), None)


@pytest.mark.asyncio
async def test_approval_appends_resolution_event(
    isolated_home: Path, live_daemon: ControllerDaemon,
) -> None:
    """M4: an approve / deny resolution must persist a closing
    ``PermissionRequestEvent`` row with ``resolved=True`` and the
    appropriate ``resolution`` string. Without it the transcript shows
    a permanently pending ASK even after the operator decided."""
    operator = await ControllerClient.connect()
    try:
        attached = await operator.new_session(
            title="resolve-test", mode="chat", origin="cli"
        )
        session_id = attached["session"]["session_id"]
        # Operator stays attached as interactive — this is the
        # canonical "TUI in the foreground" shape.

        ask_task = asyncio.create_task(
            live_daemon.request_permission(
                session_id,
                tool="file_write",
                summary="write /tmp/x",
                tool_use_id="tooluse-resolve-1",
                timeout_seconds=5.0,
            )
        )
        # Give the daemon a beat to enqueue the pending row.
        await asyncio.sleep(0.05)
        await operator.approval(
            session_id, "tooluse-resolve-1", approved=True
        )
        approved = await asyncio.wait_for(ask_task, timeout=3.0)
        assert approved is True
    finally:
        await operator.close()

    # Read the transcript off disk — that's the audit source of truth.
    events = list(TranscriptReader(session_id).read_from(0))
    permission_rows = [
        ev for ev, _ in events
        if isinstance(ev, PermissionRequestEvent)
        and ev.tool_use_id == "tooluse-resolve-1"  # type: ignore[attr-defined]
    ]
    assert len(permission_rows) >= 2, (
        f"M4 regression: expected pending + resolved permission rows, "
        f"got {[r.model_dump() for r in permission_rows]}"
    )
    closing = permission_rows[-1]
    assert closing.resolved is True
    assert closing.resolution == "approved"


@pytest.mark.asyncio
async def test_deny_appends_denied_resolution(
    isolated_home: Path, live_daemon: ControllerDaemon,
) -> None:
    """M4 corollary (audit-2 T1): an operator denial must persist a
    closing row marked ``resolution=denied``. The approve branch is
    already covered; this pins the else branch so a bug in
    ``"approved" if approved else "denied"`` can't ship undetected."""
    operator = await ControllerClient.connect()
    try:
        attached = await operator.new_session(
            title="deny-test", mode="chat", origin="cli"
        )
        session_id = attached["session"]["session_id"]

        ask_task = asyncio.create_task(
            live_daemon.request_permission(
                session_id,
                tool="file_write",
                summary="write /tmp/y",
                tool_use_id="tooluse-deny-1",
                timeout_seconds=5.0,
            )
        )
        await asyncio.sleep(0.05)
        await operator.approval(
            session_id, "tooluse-deny-1", approved=False
        )
        approved = await asyncio.wait_for(ask_task, timeout=3.0)
        assert approved is False
    finally:
        await operator.close()

    events = list(TranscriptReader(session_id).read_from(0))
    permission_rows = [
        ev for ev, _ in events
        if isinstance(ev, PermissionRequestEvent)
        and ev.tool_use_id == "tooluse-deny-1"  # type: ignore[attr-defined]
    ]
    assert len(permission_rows) >= 2
    closing = permission_rows[-1]
    assert closing.resolved is True
    assert closing.resolution == "denied"


@pytest.mark.asyncio
async def test_timeout_appends_timeout_resolution(
    isolated_home: Path, live_daemon: ControllerDaemon,
) -> None:
    """M4 corollary: a timeout (no operator decision) must also land a
    resolution row, marked ``timeout``. Without this the transcript
    looks the same as 'still waiting'."""
    operator = await ControllerClient.connect()
    try:
        attached = await operator.new_session(
            title="timeout-test", mode="chat", origin="cli"
        )
        session_id = attached["session"]["session_id"]
        approved = await live_daemon.request_permission(
            session_id,
            tool="bash_tool",
            summary="rm -rf /nope",
            tool_use_id="tooluse-timeout-1",
            timeout_seconds=0.1,
        )
        assert approved is False
    finally:
        await operator.close()

    events = list(TranscriptReader(session_id).read_from(0))
    permission_rows = [
        ev for ev, _ in events
        if isinstance(ev, PermissionRequestEvent)
        and ev.tool_use_id == "tooluse-timeout-1"  # type: ignore[attr-defined]
    ]
    assert len(permission_rows) >= 2
    closing = permission_rows[-1]
    assert closing.resolved is True
    assert closing.resolution == "timeout"


# ── M5 — tool metadata forwarded in TOOL_RESULT envelopes ─────────────


def test_tool_result_envelope_forwards_metadata() -> None:
    """M5: the Mirror chat envelope for a TOOL_RESULT chunk must
    forward ``raw['metadata']`` — otherwise frontend consumers (the
    chat hand-off card for ``start_controller_session``) only see
    plain text and can't deep-link into the child transcript."""
    from tesseract.kernel.adapters.base import ChunkType, StreamChunk
    from tesseract.mirror.server.envelope import chunk_to_envelope

    chunk = StreamChunk(
        type=ChunkType.TOOL_RESULT,
        text="started controller session sess-xyz",
        tool_call_id="call-1",
        raw={
            "metadata": {
                "kind": "child_transcript_ref",
                "session_id": "sess-xyz",
                "mode": "chat",
                "ws_path": "/ws/controller/sess-xyz",
            }
        },
    )
    envelope = chunk_to_envelope(chunk, session_id="op-session-1")
    assert envelope is not None
    data = envelope["data"]
    assert data["call_id"] == "call-1"
    metadata = data.get("metadata")
    assert isinstance(metadata, dict)
    assert metadata["session_id"] == "sess-xyz"
    assert metadata["ws_path"] == "/ws/controller/sess-xyz"


def test_tool_result_envelope_omits_metadata_when_absent() -> None:
    """M5: the metadata field is forwarded only when present. Existing
    tools that don't set metadata must not gain an empty ``metadata``
    key on the wire (frontend consumers key off presence)."""
    from tesseract.kernel.adapters.base import ChunkType, StreamChunk
    from tesseract.mirror.server.envelope import chunk_to_envelope

    chunk = StreamChunk(
        type=ChunkType.TOOL_RESULT,
        text="ok",
        tool_call_id="call-2",
        raw=None,
    )
    envelope = chunk_to_envelope(chunk, session_id="op-session-1")
    assert envelope is not None
    assert "metadata" not in envelope["data"]


# ── M6 — bounded outbound queue + overflow detach ─────────────────────


@pytest.mark.asyncio
async def test_outbound_queue_is_bounded(
    isolated_home: Path, live_daemon: ControllerDaemon,
) -> None:
    """M6: every per-client outbound queue is constructed with a finite
    ``maxsize``. Without this the QueueFull handler is dead code and a
    stuck client grows daemon memory without bound."""
    client = await ControllerClient.connect()
    try:
        # Give the daemon time to register the connection.
        await asyncio.sleep(0.05)
        conn = next(iter(live_daemon._clients.values()))
        assert conn.outbound.maxsize == _OUTBOUND_QUEUE_MAX
        assert conn.outbound.maxsize > 0
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_cli_sink_emits_cli_chunk_event_with_phase(
    isolated_home: Path, live_daemon: ControllerDaemon,
) -> None:
    """Audit-2 T3 — exercise the controller's ``cli_sink`` end-to-end.

    The chat brain calls ``ctx.cli_sink(kind, call_id, payload)`` with
    ``kind`` in ``{"cli_start", "cli_output", "cli_end"}``; the sink
    must map those to ``CliChunkEvent`` rows with ``phase`` in
    ``{"start", "chunk", "end"}`` and forward ``text`` (chunk) +
    ``exit_code`` (end) into the controller transcript. Without this
    test the phase mapping or the exception-swallowing branch could
    regress silently."""
    from tesseract.scripts.tars_controller import _make_controller_cli_sink

    # Mint a session via raw client so the transcript file exists.
    operator = await ControllerClient.connect()
    try:
        attached = await operator.new_session(
            title="cli-sink-test", mode="chat", origin="cli"
        )
        session_id = attached["session"]["session_id"]
    finally:
        await operator.close()

    sink = _make_controller_cli_sink(live_daemon, session_id)

    await sink("cli_start", "call-1", {"tool": "delegate_claude"})
    await sink(
        "cli_output", "call-1",
        {"tool": "delegate_claude", "text": "hello from claude"},
    )
    await sink(
        "cli_end", "call-1",
        {"tool": "delegate_claude", "exit_code": 0},
    )

    events = list(TranscriptReader(session_id).read_from(0))
    cli_rows = [
        ev for ev, _ in events
        if getattr(ev, "kind", None) == "cli_chunk"
    ]
    assert len(cli_rows) == 3, (
        f"T3 regression: expected 3 cli_chunk rows, got {len(cli_rows)}"
    )
    phases = [getattr(r, "phase", None) for r in cli_rows]
    assert phases == ["start", "chunk", "end"], (
        f"T3 regression: phase mapping wrong, got {phases}"
    )
    chunk_row = cli_rows[1]
    assert getattr(chunk_row, "text", "") == "hello from claude"
    end_row = cli_rows[2]
    assert getattr(end_row, "exit_code", None) == 0


@pytest.mark.asyncio
async def test_overflow_does_not_starve_sibling_clients(
    isolated_home: Path, live_daemon: ControllerDaemon,
) -> None:
    """Audit-2 T2 — multi-client isolation. When one attached client
    overflows its outbound queue, the fan-out loop must keep delivering
    to siblings rather than short-circuiting. Without this test a
    regression that returns early on the first failed enqueue would
    silently break every observer attached alongside a stuck peer."""
    # Mint a session via a third "operator" client so both A and B
    # attach to an existing session.
    operator = await ControllerClient.connect()
    try:
        attached = await operator.new_session(
            title="multi-client-test", mode="chat", origin="cli"
        )
        session_id = attached["session"]["session_id"]
    finally:
        await operator.close()

    client_a = await ControllerClient.connect()
    client_b = await ControllerClient.connect()
    try:
        await client_a.attach(session_id, mode="observer", from_offset=0)
        await client_b.attach(session_id, mode="observer", from_offset=0)
        # Give the daemon time to register both attachments before we
        # poke its private state.
        await asyncio.sleep(0.1)

        # Locate the two _ClientConn objects (writer_ids assigned in
        # connect order — but a fresh attach also enqueues the AttachedPush
        # replay so we drain those queues first).
        conns = [
            c for c in live_daemon._clients.values()
            if session_id in c.sessions
        ]
        assert len(conns) == 2, (
            f"expected 2 attached conns, got {len(conns)}"
        )
        slow_conn, healthy_conn = conns[0], conns[1]

        # Drain healthy_conn's queue so we can cleanly assert what
        # arrives after the fan-out.
        while not healthy_conn.outbound.empty():
            healthy_conn.outbound.get_nowait()

        # Saturate slow_conn so the next put_nowait raises QueueFull.
        for _ in range(_OUTBOUND_QUEUE_MAX):
            try:
                slow_conn.outbound.put_nowait({"push": True, "event": "noop"})
            except asyncio.QueueFull:
                break
        assert slow_conn.outbound.full()

        # Now fan out a transcript event the natural way. Both conns are
        # attached so both should receive (or in slow_conn's case, get
        # detached with a sentinel) — but healthy_conn must NOT be
        # starved by slow_conn's failure.
        from tesseract.orchestrator.tars_controller.events import (
            AssistantTextEvent,
        )

        await live_daemon.append_event(
            session_id,
            AssistantTextEvent(
                session_id=session_id,
                origin="chat",
                text="multi-client probe",
            ),
        )

        # Healthy client must have received the transcript_event push.
        # Drain its queue and look for a transcript_event with our text.
        delivered: list[dict[str, Any]] = []
        while not healthy_conn.outbound.empty():
            item = healthy_conn.outbound.get_nowait()
            if isinstance(item, dict):
                delivered.append(item)
        assert any(
            d.get("event") == "transcript_event"
            and d.get("transcript_event", {}).get("kind") == "assistant_text"
            and d["transcript_event"].get("text") == "multi-client probe"
            for d in delivered
        ), (
            f"T2 regression: healthy client starved by slow peer; "
            f"queue contents: {delivered}"
        )
    finally:
        await client_a.close()
        await client_b.close()


@pytest.mark.asyncio
async def test_overflow_detaches_slow_client(
    isolated_home: Path, live_daemon: ControllerDaemon,
) -> None:
    """M6: when an outbound push hits ``QueueFull`` the daemon must
    detach the offending writer (sentinel ``None`` ends the writer
    task) — not silently drop the event and keep growing."""
    client = await ControllerClient.connect()
    try:
        await asyncio.sleep(0.05)
        conn = next(iter(live_daemon._clients.values()))

        # Saturate the queue past its bound without draining it.
        for _ in range(_OUTBOUND_QUEUE_MAX):
            try:
                conn.outbound.put_nowait({"push": True, "event": "noop"})
            except asyncio.QueueFull:
                break
        assert conn.outbound.full()

        # Force a fan-out via _push_or_disconnect. It must swallow the
        # QueueFull, drain a slot, and enqueue the sentinel ``None`` so
        # the writer task exits cleanly.
        live_daemon._push_or_disconnect(
            conn,
            {"push": True, "event": "overflow_probe"},
            source="test-overflow",
        )
        # Drain the queue and confirm a sentinel landed in it.
        drained: list[Any] = []
        while not conn.outbound.empty():
            drained.append(conn.outbound.get_nowait())
        assert any(item is None for item in drained), (
            "M6 regression: overflow path didn't enqueue the writer-stop "
            "sentinel — slow clients would not be torn down"
        )
    finally:
        await client.close()

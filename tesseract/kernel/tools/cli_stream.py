"""Shared subprocess streaming helper for delegate_* tools.

When a `cli_sink` is wired (Mirror backend), runs the subprocess and emits
cli_start/cli_output/cli_end events through the sink, returning the buffered
output as a `ToolResult` so the chat timeline still gets a normal
`tool_result` envelope at the end.

Stdout is consumed in raw chunks (not lines) because CLIs like `claude -p`
buffer their entire response and only flush at exit when stdout isn't a TTY.
A real PTY is not needed: the Chat-side DelegateCard wants
bytes-as-they-arrive, with no terminal semantics.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Awaitable, Coroutine, Mapping, Sequence, TypedDict

from tesseract.kernel.tools import _process_containment as containment
from tesseract.kernel.tools.base import CliSink, ToolResult

log = logging.getLogger(__name__)


class CliSinkStartPayload(TypedDict, total=False):
    """``cli_start`` payload — fires once before any stdout reads.

    ``tool`` is the originating tool name (``delegate_coder`` etc.) so
    sinks that fan out to multiple consumers can label the stream.
    """

    tool: str
    argv: list[str]


class CliSinkChunkPayload(TypedDict, total=False):
    """``cli_output`` payload — fires per decoded stdout chunk.

    Canonical key is ``delta``. Sinks MUST read ``delta`` to obtain the
    chunk text; older audits found a controller-side sink reading
    ``text`` / ``output`` (which the producer never sets) so every
    chunk arrived empty. The typed envelope makes the contract
    explicit and is the single source of truth for consumers.
    """

    delta: str
    tool: str


class CliSinkEndPayload(TypedDict, total=False):
    """``cli_end`` payload — fires once after the process exits.

    ``exit_code`` is the OS-level exit code; -1 means the process
    failed to spawn or was cancelled before exit. ``stderr`` carries
    any captured stderr text (currently empty — stderr is merged into
    stdout via ``stderr=STDOUT`` at spawn).
    """

    exit_code: int
    stderr: str
    tool: str


async def emit_cli_event(
    sink: CliSink | None,
    call_id: str,
    event: str,
    payload: dict[str, Any],
    *,
    shielded: bool = False,
) -> None:
    """Push one event to the operator's view. Never load-bearing.

    `shielded` is for the terminal event: it is emitted from a `finally`
    whose usual trigger is a cancellation, and an unshielded await there
    would be cancelled before the sink saw it — leaving the card open,
    which is the bug the finally exists to close. The shielded branch goes
    through `_shield_cleanup` (see invariant 8 above `_reap_all`), the same
    module-level strong reference the post-spawn cleanup uses, so a SECOND
    cancellation landing on this await cannot let the emit be garbage
    collected out from under it."""
    if sink is None:
        return
    try:
        call = sink(event, call_id, payload)
        await (asyncio.shield(_shield_cleanup(call)) if shielded else call)
    except Exception:  # noqa: BLE001 — the operator's view is never load-bearing
        log.debug("cli sink %s failed", event, exc_info=True)
    except asyncio.CancelledError:
        # The shielded emit is already on its way; don't let the card's
        # terminal event swallow the cancellation itself.
        raise


# SGR colour codes — stripped so streamed CLI output renders as plain
# text in the TUI / transcript.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
# OSC sequences (ESC ] ... BEL  or  ESC ] ... ESC \). `claude` / `codex`
# emit `ESC]0;<title>BEL` to set the terminal title — if we don't strip
# it the operator's REAL terminal gets renamed to "claude" mid-stream
# (operator-reported 2026-05-24). Also strip other CSI sequences (cursor
# moves, clears) that are meaningless in a coalesced log view.
_OSC_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_CSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_READ_CHUNK_BYTES = 4096
_MAX_BUFFER_CHARS = 200_000
# On timeout, this much of the already-streamed transcript tail rides along in
# the error ToolResult so the model can see what the subprocess was doing when
# it was killed.
_TIMEOUT_TAIL_CHARS = 1_500
# After the subprocess itself exits, keep draining stdout this long before
# abandoning the pipe. A task that spawns a longer-lived grandchild (e.g. a
# dev server) leaves the inherited stdout write-end open, so reading to EOF
# would block until `timeout` — the spawn would read as `running` for minutes
# after `codex`/`claude` actually finished (operator-observed 2026-05-27).
# Completion is driven by process exit; this grace only salvages output
# buffered between exit and the final read.
DRAIN_GRACE_SECONDS = 2.0


def _strip_control_sequences(text: str) -> str:
    """Remove OSC (title-set etc.), SGR colour, and other CSI control
    sequences from streamed subprocess output. Leaves printable text +
    newlines intact."""
    text = _OSC_RE.sub("", text)
    text = _CSI_RE.sub("", text)
    return text


# Shared cancellation-cleanup shape for `race_communicate` and
# `run_subprocess_with_sink`. Both wrap their spawn-to-reap region in ONE
# `try`/`finally`, and the `finally` runs the reap (plus, for the sink
# variant, the `cli_end` emit) as ONE `asyncio.shield`ed call. Every exit —
# normal completion, timeout, the watcher observing cancellation, a
# `CancelledError` delivered to the call itself, and a SECOND cancellation
# landing while that cleanup is still running — goes through that one path.
# These properties hold at once, not one at a time:
#
# 1. The process tree is killed/reaped on every exit, and that happens
#    first, before the tasks that fed it are torn down.
# 2. No orphaned tasks (stdout/stderr drain, pump, wait, watch) survive.
# 3. `cli_end` is emitted exactly once on every exit of the sink variant,
#    carrying the right exit code — PRE-spawn exits included: the
#    `cli_start`-cancellation handler and the `FileNotFoundError`/`OSError`
#    spawn-failure handlers all emit `cli_end` through `emit_cli_event(...,
#    shielded=True)`, which routes through the same `_shield_cleanup` as
#    property 8 below, not a bare `asyncio.shield`.
# 4. A cancellation cannot interrupt the reap into an unbounded/hung state
#    (`containment.reap` carries its own timeout; cancelling and gathering
#    an already-finished task is a no-op) — but a cancellation landing
#    DURING cleanup must not skip `cli_end` either. One shielded call, with
#    no unshielded gap inside it, is what makes both true together: a
#    second cancellation can only interrupt the CALLER's await of the
#    shield, never the shielded work itself, which keeps running until it
#    has reaped the tree and (for the sink variant) emitted `cli_end`.
# 5. The cancellation itself still reaches the caller; cleanup never
#    swallows it.
# 6. Timeout and pipe-stays-open behaviour (`DRAIN_GRACE_SECONDS`) are
#    unchanged.
# 7. The watcher (`cancel_event`) is never load-bearing: a call with none
#    takes the same path through the same `finally`.
# 8. `asyncio.shield` on a bare coroutine wraps it in an inner Task that the
#    event loop itself holds only a WEAK reference to (CPython's own
#    `asyncio.shield` docstring); nothing else keeps that inner task alive.
#    A SECOND cancellation landing on the `await asyncio.shield(...)` tears
#    down the caller's frame, and if that was the only strong reference,
#    garbage collection (an explicit `gc.collect()`, or the interpreter's
#    cycle collector at shutdown) can collect the still-running cleanup or
#    leave it destroyed pending — the reap, and the sink variant's
#    `cli_end`, then never finish. `_shield_cleanup` is what keeps property
#    4 true under that: it wraps the cleanup coroutine in a Task held in the
#    module-level `_pending_cleanup_tasks` set BEFORE the task ever reaches
#    `asyncio.shield`, and the task discards itself via its own done
#    callback the moment it finishes. The set is the strong reference
#    `asyncio.shield` does not provide; `shield` still does only what it
#    always did, protect the awaiting caller, never the shielded work.
#    `emit_cli_event`'s own shielded branch goes through `_shield_cleanup`
#    too, so every PRE-spawn shielded `cli_end` (the `cli_start`-cancellation
#    handler, the spawn-failure handlers) is covered by the same mechanism
#    as the post-spawn `_finish` reap+emit, not a separate weaker one.
async def _reap_all(
    process: asyncio.subprocess.Process, tasks: Sequence[asyncio.Task | None]
) -> None:
    """Kill the process tree, then cancel and gather every helper task.

    Bounded and idempotent: `containment.reap` carries its own timeout and
    returns at once on a process that already exited, and cancelling +
    gathering a task that is already done is a no-op. Never raises.
    """
    await containment.reap(process)
    live = [t for t in tasks if t is not None]
    for t in live:
        t.cancel()
    if live:
        await asyncio.gather(*live, return_exceptions=True)


# Strong references for cleanup tasks in flight under `asyncio.shield` (see
# invariant 8 above `_reap_all`). Each task removes itself the moment it
# finishes; a non-empty set at rest means a leak.
_pending_cleanup_tasks: set[asyncio.Task[None]] = set()


def _shield_cleanup(
    coro: Coroutine[Any, Any, None] | Awaitable[None],
) -> asyncio.Task[None]:
    """Wrap a cancellation-cleanup coroutine in a Task the module keeps a
    strong reference to, so `await asyncio.shield(_shield_cleanup(...))`
    survives a second cancellation. See invariant 8 above `_reap_all`. Used
    for both the post-spawn reap+emit (`_finish`) and every shielded
    `emit_cli_event` call, pre-spawn exits included."""
    task = asyncio.ensure_future(coro)
    _pending_cleanup_tasks.add(task)
    task.add_done_callback(_pending_cleanup_tasks.discard)
    return task


async def race_communicate(
    process: asyncio.subprocess.Process,
    cancel_event: asyncio.Event | None,
    timeout: float,
    tool_name: str,
) -> tuple[bytes, bytes] | None:
    """Run ``process.communicate()`` raced against ``cancel_event`` and ``timeout``.

    Returns ``(stdout, stderr)`` on success, or ``None`` when the call was
    cancelled (process has already been killed and waited by the time None is
    returned).  Raises ``asyncio.TimeoutError`` on timeout (process killed),
    ``FileNotFoundError`` / ``OSError`` propagate from the caller's spawn.

    Completion is driven by ``process.wait()`` (process exit), NOT by stdout
    EOF: a surviving grandchild that inherited the pipe must not keep the read
    blocked past the child's exit. After exit, the pipe readers get
    ``DRAIN_GRACE_SECONDS`` to finish, then they are abandoned and whatever was
    buffered is returned.
    """
    out_buf = bytearray()
    err_buf = bytearray()

    async def _drain(stream, buf: bytearray) -> None:
        if stream is None:
            return
        while True:
            chunk = await stream.read(_READ_CHUNK_BYTES)
            if not chunk:
                return
            buf.extend(chunk)

    out_task = asyncio.create_task(_drain(process.stdout, out_buf))
    err_task = asyncio.create_task(_drain(process.stderr, err_buf))
    wait_task = asyncio.create_task(process.wait())
    watch_task = (
        asyncio.create_task(cancel_event.wait()) if cancel_event is not None else None
    )
    tasks: list[asyncio.Task | None] = [out_task, err_task, wait_task, watch_task]

    #: The one await between spawning the process and reaping it. A
    #: cancellation delivered HERE used to leave the subprocess running with
    #: nobody holding it: `claude` or `codex` went on working after the
    #: operator pressed stop.
    #:
    #: Not the same thing as the cooperative branch below. `_cancel_turn` sets
    #: the shared event AND cancels the task in the same breath, so the watcher
    #: does not reliably observe the event first, and `delegate_second_opinion`
    #: passes no event at all — for that tool this was never a race, only the
    #: outcome. Killing here is the backstop; the branch below is still what
    #: reports a clean cancellation when it wins.
    try:
        waiters = {wait_task} | ({watch_task} if watch_task is not None else set())
        done, _ = await asyncio.wait(
            waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
        )

        if watch_task is not None and watch_task in done and wait_task not in done:
            # Cancel fired before the process exited.
            return None  # caller should return a "cancelled" ToolResult
        if wait_task not in done:
            # Timeout — process never exited.
            raise asyncio.TimeoutError

        # Process exited. Give the readers a short grace to finish before
        # `finally` abandons a pipe a grandchild may still hold open.
        # `return_exceptions=True` so a transport error in a drain task
        # (abrupt pipe close → ConnectionResetError) becomes a return value
        # rather than escaping and leaving the other task unreaped.
        try:
            await asyncio.wait_for(
                asyncio.gather(out_task, err_task, return_exceptions=True),
                timeout=DRAIN_GRACE_SECONDS,
            )
        except asyncio.TimeoutError:
            pass
        return bytes(out_buf), bytes(err_buf)
    finally:
        await asyncio.shield(_shield_cleanup(_reap_all(process, tasks)))


async def run_subprocess_with_sink(
    *,
    tool_name: str,
    argv: Sequence[str],
    cwd: str,
    timeout: float,
    sink: CliSink | None,
    call_id: str,
    empty_message: str,
    missing_message: str,
    env: Mapping[str, str] | None = None,
    cancel_event: asyncio.Event | None = None,
    output_parser=None,
) -> ToolResult:
    # `output_parser`: optional
    # object with `feed(chunk) -> str`, `flush() -> str`, and
    # `final_output() -> str | None` (see ClaudeDelegateStreamParser). When
    # set, raw stdout is machine framing (NDJSON) — the parser converts each
    # chunk to readable transcript text for the sink/buffer, and
    # `final_output()` supplies the ToolResult text instead of the buffer.
    async def _emit(kind: str, payload: dict, *, shielded: bool = False) -> None:
        # `emit_cli_event`, not a bare await. The operator's view is never
        # load-bearing, and this one was: `cli_start` fires before the
        # subprocess is spawned and `cli_end` after it has exited cleanly, so
        # a sink that raised anything the socket wrapper does not catch would
        # abort a delegate call before it began, or throw away a result the
        # subprocess had already produced. The other two streaming paths were
        # moved onto the shared helper and this call site was missed.
        await emit_cli_event(sink, call_id, kind, payload, shielded=shielded)

    # The card opens, and from here every exit closes it. A cancellation
    # landing on this emit used to leave a card open for a call that never
    # spawned anything, because nothing above the spawn had a `finally`.
    try:
        await _emit("cli_start", {"tool": tool_name})
    except asyncio.CancelledError:
        await _emit("cli_end", {"exit_code": -1}, shielded=True)
        raise

    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=cwd,
            env=dict(env) if env is not None else None,
        )
    except FileNotFoundError:
        await _emit("cli_end", {"exit_code": -1}, shielded=True)
        return ToolResult(output=missing_message, is_error=True)
    except OSError as e:
        await _emit("cli_end", {"exit_code": -1}, shielded=True)
        return ToolResult(output=f"{tool_name} failed to start: {e}", is_error=True)
    except Exception as e:  # noqa: BLE001 - a bad argument (a NUL byte) is ValueError, and the card must still close
        await _emit("cli_end", {"exit_code": -1}, shielded=True)
        return ToolResult(output=f"{tool_name} failed to start: {e}", is_error=True)

    assert process.stdout is not None
    buffer: list[str] = []
    buffer_len = 0

    async def _pump_chunks() -> None:
        nonlocal buffer_len
        while True:
            raw = await process.stdout.read(_READ_CHUNK_BYTES)
            if not raw:
                return
            chunk = _strip_control_sequences(
                raw.decode("utf-8", errors="replace")
            )
            if output_parser is not None:
                chunk = output_parser.feed(chunk)
            if not chunk:
                continue
            await _emit("cli_output", {"delta": chunk})
            if buffer_len < _MAX_BUFFER_CHARS:
                buffer.append(chunk)
                buffer_len += len(chunk)

    # Completion is driven by `process.wait()` (process exit), NOT by the pump
    # reaching stdout EOF. A task that spawns a longer-lived grandchild (a dev
    # server etc.) leaves the inherited stdout write-end open, so the pump
    # would block until `timeout` even though the child already exited — the
    # spawn would read as `running` for minutes. After exit the pump gets a
    # short grace to drain, then it is abandoned.
    pump_task = asyncio.create_task(_pump_chunks())
    wait_task = asyncio.create_task(process.wait())
    watch_task = (
        asyncio.create_task(cancel_event.wait()) if cancel_event is not None else None
    )
    tasks: list[asyncio.Task | None] = [pump_task, wait_task, watch_task]
    # Set once the process is known to have exited; -1 covers every other
    # exit (cancelled, timed out) and is what `cli_end` reports for them.
    exit_code = -1

    async def _finish() -> None:
        # Reap the tree first, THEN the terminal event — as one shielded
        # unit (see the invariant comment above `_reap_all`), so a second
        # cancellation landing here can interrupt the CALLER's await of
        # this coroutine but never split the two apart.
        await _reap_all(process, tasks)
        await _emit("cli_end", {"exit_code": exit_code})

    #: The one await between spawning the process and reaping it. A
    #: cancellation delivered HERE used to leave the subprocess running with
    #: nobody holding it: `claude` or `codex` went on working after the
    #: operator pressed stop.
    #:
    #: Not the same thing as the cooperative branch below. `_cancel_turn` sets
    #: the shared event AND cancels the task in the same breath, so the watcher
    #: does not reliably observe the event first, and `delegate_second_opinion`
    #: passes no event at all — for that tool this was never a race, only the
    #: outcome. Killing here is the backstop; the branch below is still what
    #: reports a clean cancellation when it wins.
    try:
        waiters = {wait_task} | ({watch_task} if watch_task is not None else set())
        done, _ = await asyncio.wait(
            waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
        )

        if watch_task is not None and watch_task in done and wait_task not in done:
            # Cancel fired before the process exited.
            return ToolResult(output=f"{tool_name} cancelled", is_error=True)
        if wait_task not in done:
            # Timeout — process never exited. Report it WITH the tail of
            # whatever it already streamed, so the model can see the run was
            # productive rather than assuming nothing happened. A bare
            # "timed out" string costs a full re-delegation.
            output = f"{tool_name} timed out after {timeout}s"
            tail = "".join(buffer).strip()[-_TIMEOUT_TAIL_CHARS:]
            if tail:
                output += f"\n\nTranscript tail before the kill:\n{tail}"
            return ToolResult(output=output, is_error=True, timed_out=True)

        # Process exited. Give the pump a short grace to drain remaining
        # output before `finally` abandons a pipe a grandchild may hold.
        exit_code = wait_task.result()
        # `return_exceptions=True` so a transport error in the pump (abrupt
        # pipe close → ConnectionResetError) becomes a return value rather
        # than escaping; buffered output so far is still returned below.
        try:
            await asyncio.wait_for(
                asyncio.gather(pump_task, return_exceptions=True),
                timeout=DRAIN_GRACE_SECONDS,
            )
        except asyncio.TimeoutError:
            pass

        if output_parser is not None:
            trailing = output_parser.flush()
            if trailing:
                await _emit("cli_output", {"delta": trailing})
                if buffer_len < _MAX_BUFFER_CHARS:
                    buffer.append(trailing)
                    buffer_len += len(trailing)

        out = "".join(buffer).strip()
        if output_parser is not None:
            final = output_parser.final_output()
            if final:
                out = final

        if exit_code != 0:
            combined = f"Exit code: {exit_code}"
            if out:
                combined += f"\noutput:\n{out}"
            return ToolResult(output=combined, is_error=True)

        if not out:
            return ToolResult(output=empty_message, is_error=True)

        # The CLI can exit 0 while the turn itself failed (claude stream-json
        # `result` events with subtype error_max_turns / error_during_execution).
        # Honor the parser's turn-level verdict so the model sees the failure.
        if output_parser is not None and getattr(output_parser, "is_error", False):
            return ToolResult(output=out, is_error=True)

        return ToolResult(output=out, metadata={"tool": tool_name, "exit_code": 0})
    finally:
        await asyncio.shield(_shield_cleanup(_finish()))

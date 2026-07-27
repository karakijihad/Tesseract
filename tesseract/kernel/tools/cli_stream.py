"""Shared subprocess streaming helper for delegate_* tools.

When a `cli_sink` is wired (Mirror backend), runs the subprocess and emits
cli_start/cli_output/cli_end events through the sink, returning the buffered
output as a `ToolResult` so the chat timeline still gets a normal
`tool_result` envelope at the end.

Stdout is consumed in raw chunks (not lines) because CLIs like `claude -p`
buffer their entire response and only flush at exit when stdout isn't a TTY.
A real PTY belongs in Phase 9; for the Chat-side DelegateCard we just want
bytes-as-they-arrive, no terminal semantics needed.
"""

from __future__ import annotations

import asyncio
import re
from typing import Mapping, Sequence, TypedDict

from tesseract.kernel.tools.base import CliSink, ToolResult


class CliSinkStartPayload(TypedDict, total=False):
    """``cli_start`` payload — fires once before any stdout reads.

    ``tool`` is the originating tool name (``delegate_claude`` etc.) so
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
# it was killed (delegate visibility fix-pass 2026-07-10).
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

    async def _reap(*tasks) -> None:
        live = [t for t in tasks if t is not None]
        for t in live:
            t.cancel()
        await asyncio.gather(*live, return_exceptions=True)

    waiters = {wait_task} | ({watch_task} if watch_task is not None else set())
    done, _ = await asyncio.wait(
        waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
    )

    if watch_task is not None and watch_task in done and wait_task not in done:
        # Cancel fired before the process exited — kill and drain everything.
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await process.wait()
        await _reap(out_task, err_task, wait_task, watch_task)
        return None  # caller should return a "cancelled" ToolResult
    if wait_task not in done:
        # Timeout — process never exited. Kill and reap before raising.
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await process.wait()
        await _reap(out_task, err_task, wait_task, watch_task)
        raise asyncio.TimeoutError
    # Process exited. Reap the watcher, then give the readers a short grace to
    # finish before abandoning a pipe a grandchild may still hold open.
    if watch_task is not None:
        await _reap(watch_task)
    # `return_exceptions=True` so a transport error in a drain task (abrupt
    # pipe close → ConnectionResetError) becomes a return value rather than
    # escaping the TimeoutError handler and leaving the other task unreaped.
    try:
        await asyncio.wait_for(
            asyncio.gather(out_task, err_task, return_exceptions=True),
            timeout=DRAIN_GRACE_SECONDS,
        )
    except asyncio.TimeoutError:
        await _reap(out_task, err_task)
    return bytes(out_buf), bytes(err_buf)


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
    # `output_parser` (delegate visibility fix-pass 2026-07-10): optional
    # object with `feed(chunk) -> str`, `flush() -> str`, and
    # `final_output() -> str | None` (see ClaudeDelegateStreamParser). When
    # set, raw stdout is machine framing (NDJSON) — the parser converts each
    # chunk to readable transcript text for the sink/buffer, and
    # `final_output()` supplies the ToolResult text instead of the buffer.
    async def _emit(kind: str, payload: dict) -> None:
        if sink is not None:
            await sink(kind, call_id, payload)

    await _emit("cli_start", {"tool": tool_name})

    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=cwd,
            env=dict(env) if env is not None else None,
        )
    except FileNotFoundError:
        await _emit("cli_end", {"exit_code": -1})
        return ToolResult(output=missing_message, is_error=True)
    except OSError as e:
        await _emit("cli_end", {"exit_code": -1})
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

    async def _reap(*tasks) -> None:
        live = [t for t in tasks if t is not None]
        for t in live:
            t.cancel()
        await asyncio.gather(*live, return_exceptions=True)

    waiters = {wait_task} | ({watch_task} if watch_task is not None else set())
    done, _ = await asyncio.wait(
        waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
    )

    if watch_task is not None and watch_task in done and wait_task not in done:
        # Cancel fired before the process exited — kill and drain.
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await process.wait()
        await _reap(pump_task, wait_task, watch_task)
        await _emit("cli_end", {"exit_code": -1})
        return ToolResult(output=f"{tool_name} cancelled", is_error=True)
    if wait_task not in done:
        # Timeout — process never exited. Kill, reap, report — WITH the tail
        # of whatever it already streamed, so the model can see the run was
        # productive rather than assuming nothing happened (fix-pass
        # 2026-07-10; the bare "timed out" string cost a full re-delegation).
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await process.wait()
        await _reap(pump_task, wait_task, watch_task)
        await _emit("cli_end", {"exit_code": -1})
        output = f"{tool_name} timed out after {timeout}s"
        tail = "".join(buffer).strip()[-_TIMEOUT_TAIL_CHARS:]
        if tail:
            output += f"\n\nTranscript tail before the kill:\n{tail}"
        return ToolResult(output=output, is_error=True, timed_out=True)

    # Process exited. Reap the watcher, then give the pump a short grace to
    # drain remaining output before abandoning a pipe a grandchild may hold.
    if watch_task is not None:
        await _reap(watch_task)
    rc = wait_task.result()
    # `return_exceptions=True` so a transport error in the pump (abrupt pipe
    # close → ConnectionResetError) becomes a return value rather than escaping
    # the TimeoutError handler; buffered output so far is still returned below.
    try:
        await asyncio.wait_for(
            asyncio.gather(pump_task, return_exceptions=True),
            timeout=DRAIN_GRACE_SECONDS,
        )
    except asyncio.TimeoutError:
        await _reap(pump_task)

    if output_parser is not None:
        trailing = output_parser.flush()
        if trailing:
            await _emit("cli_output", {"delta": trailing})
            if buffer_len < _MAX_BUFFER_CHARS:
                buffer.append(trailing)
                buffer_len += len(trailing)

    await _emit("cli_end", {"exit_code": rc})
    out = "".join(buffer).strip()
    if output_parser is not None:
        final = output_parser.final_output()
        if final:
            out = final

    if rc != 0:
        combined = f"Exit code: {rc}"
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

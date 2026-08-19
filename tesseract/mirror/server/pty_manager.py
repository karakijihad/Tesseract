"""PTY channel for the Mirror Terminal tab.

PTY is operator-direct: the assistant's permission engine does NOT gate terminal I/O.
Keystrokes go straight into the PTY and its output streams back verbatim.
Terminal messages bypass the Envelope wrapper — they are emitted as flat
top-level JSON, matching the client-side `useTerminalStore.handleRawMessage`
contract in `tesseract/mirror/src/stores/terminal.ts`.

Backend: `winpty.PtyProcess` (ConPTY wrapper). Gives a real Windows PTY —
per-keystroke echo, cursor addressing, ANSI escapes, SIGWINCH-equivalent
resize. winpty is blocking I/O, so `spawn` / `read` / `write` / `resize` /
`terminate` all run on the thread executor via `asyncio.to_thread`.
"""

from __future__ import annotations

import asyncio
import collections
import logging
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from aiohttp import web

# A missing pywinpty must degrade the terminal panel, never kill the whole
# backend: this module is imported by app.py at boot, and an unguarded
# import crashed every provisioned install on 2026-07-29 (pywinpty was an
# optional extra the provisioner never installed).
try:
    from winpty import PtyProcess
except ImportError as _exc:
    PtyProcess = None  # type: ignore[assignment, misc]
    _WINPTY_IMPORT_ERROR: str | None = str(_exc)
else:
    _WINPTY_IMPORT_ERROR = None

from tesseract.mirror.server.config import ShellProfile, TerminalServerConfig
from tesseract.orchestrator.terminal.end_of_turn import scrub_secrets, strip_ansi

log = logging.getLogger(__name__)

# Output-consumer signature.  Sync callable invoked on the event loop after
# every PTY read.  Implementations must be non-blocking — schedule any heavy
# work via ``asyncio.create_task`` / ``call_later``.
OutputConsumer = Callable[[str], None]

READ_CHUNK_CHARS = 4096
# CR-3 (2026-05-22) — WebSocket coalescing for high-throughput PTY output.
# `winpty.read` returns whatever is available, often tiny chunks (ANSI
# escapes during CLI streaming). Without coalescing, each chunk → one
# `ws.send_json` → one TCP frame → one xterm.js render frame. The
# coalescer flushes a single WS frame when *any* of these trips:
#   - the pane's config `coalesce_flush_chars` have accumulated, OR
#   - the config `coalesce_flush_ms` window has elapsed since the first
#     pending byte (sustained-output backstop), OR
#   - Phase 5 (2026-07-05, terminal daily-driver) — the asyncio queue is
#     empty right after a chunk is processed and the pending payload is
#     still small. This is the interactive-echo case (a lone keystroke's
#     output): flushing immediately instead of waiting for the window
#     keeps typing-echo latency near zero while sustained/bursty output
#     (queue kept non-empty by the reader thread) still coalesces via
#     the size/window triggers above.
# Per-chunk fidelity is preserved for output consumers and the observer
# push — they run synchronously on every raw read, before coalescing.
# Thresholds are config-driven (`TerminalServerConfig.coalesce_flush_*`,
# sourced from `permissions.yaml::pty_thresholds`) — no module-level
# defaults here.
SHUTDOWN_TERM_GRACE = 1.5
DEFAULT_COLS = 80
DEFAULT_ROWS = 24
# Per-pane observer-push backpressure cap. A high-throughput PTY would
# otherwise pile up `asyncio.Task`s faster than the observer can consume
# them. Once the cap is hit, the oldest in-flight push is cancelled and
# replaced with the new one — observation is best-effort, terminal output
# never blocks on it.
OBSERVER_PUSH_CAP_PER_PANE = 16

# Phase 2 (terminal-control 2026-05-16): per-pane output ring buffer. Cap
# total chars across all chunks; oldest evicted when full. 64 KB is enough
# for ~800 lines of 80-col text — comfortably covers any terminal's
# visible scrollback. Configurable later via permissions.yaml::pty.
OUTPUT_BUFFER_CHAR_CAP = 65_536
# wait_idle poll cadence floor. We never sleep less than this to avoid
# burning CPU on tight quiescence checks (e.g. quiescence_ms=10).
WAIT_IDLE_MIN_POLL_MS = 25.0


@dataclass
class PTYEntry:
    pane_id: str
    shell: str
    proc: PtyProcess
    ws: web.WebSocketResponse
    owner: str = "user"
    observer_enabled: bool = True
    reader_task: asyncio.Task[None] | None = field(default=None)
    # Per-pane observer-push tasks. Capped at OBSERVER_PUSH_CAP_PER_PANE so
    # one chatty pane cannot starve the asyncio loop. See `_forward_to_observer`.
    observer_tasks: set[asyncio.Task[None]] = field(default_factory=set)
    # Per-pane output consumers (MO-6).  ``_reader_loop`` invokes each one
    # synchronously after the WebSocket forward + observer push; consumers
    # may not raise (errors are logged and swallowed) and must be
    # non-blocking.
    output_consumers: list[OutputConsumer] = field(default_factory=list)
    # Phase 2 (terminal-control 2026-05-16): unconditional output ring
    # buffer giving `read_buffer_for_pane` a scrollback to read and
    # `wait_idle_for_pane` a monotonic clock to compute quiescence
    # against. Updated in _reader_loop after every successful read; bounded by
    # OUTPUT_BUFFER_CHAR_CAP. output_total_chars is monotonic across the
    # pane's lifetime — callers use it as an opaque "since_token" cursor.
    output_buffer: collections.deque[str] = field(default_factory=collections.deque)
    output_buffer_chars: int = 0
    output_total_chars: int = 0
    spawned_at: float = field(default_factory=time.monotonic)
    last_output_at: float = field(default_factory=time.monotonic)
    closed_at: float | None = None
    # F2 (terminal daily-driver 2026-07-05) — client-driven flow control.
    # Set = draining allowed; cleared by a `terminal_pause` dispatch, set
    # again by `terminal_resume`. The reader loop awaits this before every
    # queue drain so a slow client (xterm.js write buffer over HIGH
    # watermark) can halt PTY→WS forwarding without killing the read
    # thread. Starts set (not paused) — see `_new_resume_event`.
    resume_event: asyncio.Event = field(default_factory=lambda: _new_resume_event())
    # F6 — set when the owning WS disconnects; cleared on a successful
    # `terminal_reattach`. A grace-expiry task keyed to this timestamp
    # kills the pane if no client reattaches in time (see `_grace_expire`).
    detached_at: float | None = None


def _new_resume_event() -> asyncio.Event:
    event = asyncio.Event()
    event.set()
    return event


class PTYManager:
    """Owns every PTY spawned by the Mirror. One instance per app."""

    def __init__(self, cfg: TerminalServerConfig) -> None:
        self._cfg = cfg
        self._max_ptys = cfg.max_tabs * cfg.max_panes_per_tab
        self._ptys: dict[str, PTYEntry] = {}
        self._lock = asyncio.Lock()
        self._app: web.Application | None = None

    @property
    def max_ptys(self) -> int:
        return self._max_ptys

    def bind_app(self, app: web.Application) -> None:
        """Attach the aiohttp app so the reader loop can reach the
        observer instance + consent set without threading a reference
        through every dispatch call."""
        self._app = app

    def _spawn_tracked(self, coro, name: str) -> asyncio.Task:
        """Route fire-and-forget work through `scheduler.spawn_tracked_task`
        so engine shutdown can join/cancel cleanly. Falls back to bare
        `asyncio.create_task` when the scheduler isn't bound (tests, partial
        boot)."""
        scheduler = self._app.get("scheduler") if self._app is not None else None
        if scheduler is not None:
            return scheduler.spawn_tracked_task(coro, name=name)
        return asyncio.create_task(coro, name=name)

    # ── public routing ────────────────────────────────────────────────

    async def dispatch(self, msg: dict[str, Any], ws: web.WebSocketResponse) -> None:
        # Track the most recent operator WS so an agent-spawned viewer
        # pane (``start_controller_session`` / boot-time reattach) has
        # a real renderer to bind the new pane to. ``cleanup_for_ws``
        # clears stale references when the WS disconnects so we don't
        # keep a dead reference around.
        if self._app is not None:
            self._app["primary_ws"] = ws
        kind = msg.get("type")
        pane_id = msg.get("pane_id")
        if not isinstance(pane_id, str) or not pane_id:
            log.debug("pty: %s missing pane_id — ignoring", kind)
            return
        if kind == "terminal_start":
            shell = msg.get("shell") or self._cfg.default_shell
            await self._start(pane_id, str(shell), ws)
        elif kind == "terminal_keystroke":
            await self._keystroke(pane_id, str(msg.get("bytes", "")))
        elif kind == "terminal_resize":
            try:
                cols, rows = int(msg.get("cols", 0)), int(msg.get("rows", 0))
            except (TypeError, ValueError):
                log.debug("pty: bad resize payload %r — ignoring", msg)
                return
            await self._resize(pane_id, cols, rows)
        elif kind == "terminal_stop":
            await self._stop(pane_id)
        elif kind == "terminal_observer_toggle":
            await self._observer_toggle(pane_id, bool(msg.get("enabled", True)))
        elif kind == "terminal_pause":
            await self._pause(pane_id)
        elif kind == "terminal_resume":
            await self._resume(pane_id)
        elif kind == "terminal_reattach":
            # `fresh: true` means the client's xterm has no content (a
            # page bootstrap/reload — Finding 1, live-gate 2026-07-05):
            # any since_token it sent reflects a previous page lifetime
            # and must NOT suppress replay into the brand-new terminal.
            # Force a full-buffer replay regardless of what since_token
            # carries. `read_buffer_for_pane` also clamps a stray
            # out-of-range/negative cursor to the buffer start as a
            # second line of defense.
            fresh = bool(msg.get("fresh"))
            since_token = None if fresh else msg.get("since_token")
            await self._reattach(pane_id, str(since_token) if since_token is not None else None, ws)
        else:
            log.debug("pty: unknown terminal type %r", kind)

    def peek_pane_owner(self, pane_id: str) -> str | None:
        """Read-only probe used by agent-side callers to refuse early when
        the operator still owns the pane. Returns ``"user"`` / ``"entity"`` /
        ``None`` (pane not found).
        """
        entry = self._ptys.get(pane_id)
        return entry.owner if entry is not None else None

    async def cleanup_for_ws(self, ws: web.WebSocketResponse) -> None:
        if self._app is not None and self._app.get("primary_ws") is ws:
            # MO-9-4: agent-spawned panes use ``primary_ws`` as their
            # render target. When that WS disconnects, drop the ref so
            # the next operator-side message can repopulate it.
            self._app["primary_ws"] = None
        # F6 (terminal daily-driver 2026-07-05) — a dropped WS no longer
        # kills the pane outright (that turned every page reload into a
        # dead shell). Mark it detached and start a grace timer; a
        # reconnecting client that claims the pane within the grace
        # window reattaches to the still-running process (`_reattach`).
        detached = [
            pid for pid, p in self._ptys.items() if p.ws is ws and p.detached_at is None
        ]
        for pid in detached:
            self._detach(pid)
        if detached:
            log.info("pty: cleanup_for_ws detached %d pty(s) pending reattach", len(detached))
        if self._app is not None and not any(p.detached_at is None for p in self._ptys.values()):
            # The panes are gone, so the consent that covered them is too and
            # the buffered terminal context is stale. The subscriber is NOT
            # disarmed: a channel conversation is still live and still being
            # observed after the cockpit's socket closes. Owner request
            # 2026-04-29 follow-up — DO NOT reset `observer_state` to "off"
            # here either. The boot-armed default (`_on_startup`) only fires
            # once per backend boot; resetting on WS disconnect meant every
            # browser refresh dropped the operator's implicit consent until
            # they manually re-armed.
            self._app["observer_consented_panes"] = set()
            observer = self._app.get("observer")
            if observer is not None:
                try:
                    observer.reset()
                except Exception:
                    log.exception("observer.reset on ws cleanup failed")

    async def cleanup_all(self) -> None:
        await asyncio.gather(
            *(self._stop(pid, notify=False) for pid in list(self._ptys)),
            return_exceptions=True,
        )

    # ── output-consumer registry (MO-6 transcript readback) ──────────

    def register_output_consumer(self, pane_id: str, consumer: OutputConsumer) -> bool:
        """Register a sync callback that receives every PTY read for ``pane_id``.

        Returns True on success, False if no PTY is open for ``pane_id``.
        Callbacks must be non-blocking — see :class:`OutputConsumer`.
        """
        entry = self._ptys.get(pane_id)
        if entry is None:
            return False
        entry.output_consumers.append(consumer)
        return True

    def unregister_output_consumer(self, pane_id: str, consumer: OutputConsumer) -> None:
        """Unregister a previously registered output consumer.

        No-op if the pane was already torn down or the consumer was never
        registered (idempotent — safe to call from worker cleanup).
        """
        entry = self._ptys.get(pane_id)
        if entry is None:
            return
        try:
            entry.output_consumers.remove(consumer)
        except ValueError:
            pass

    # ── agent-side dispatcher ──────────────────────────────────────────

    async def dispatch_for_agent(self, op: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Entry point for the dual-use "open" verb.

        This is the agent-facing counterpart to `dispatch` (which
        serves the operator's keyboard). agent-driven verbs (typing
        into panes, reading their screen, closing them) were retired
        in the P4 PTY prune — ``open`` is the only survivor because it
        also serves the operator-visible viewer path:
        `start_controller_session.py` (``launch_terminal=True``) and
        boot-time `reattach_operator_panes` both spawn a viewer pane
        through this method.

        MO-9-4: ``op == "open"`` spawns a brand new pane bound to the
        most recently active operator WS (``app["primary_ws"]``). The
        renderer envelope is the same ``terminal_started`` shape the
        operator-side ``terminal_start`` emits, so the existing Mirror
        Terminal tab picks it up without frontend changes.
        """
        if op == "open":
            return await self._open_for_agent(payload)
        return {"ok": False, "error": f"unknown_op:{op}"}

    # ── output ring buffer + idle/read/list (Phase 2/3/5) ────────────

    def _append_to_buffer(self, entry: PTYEntry, chunk: str) -> None:
        """Feed a raw chunk into the per-pane ring buffer + bump clocks.

        Called from the reader loop on every successful read. The
        buffer caps at OUTPUT_BUFFER_CHAR_CAP chars across all chunks;
        oldest chunks are evicted from the left until the total fits.
        output_total_chars is monotonic across the pane's lifetime
        (never decreases on eviction) so callers can use it as an
        opaque since-token cursor for delta reads.
        """
        if not chunk:
            return
        entry.output_buffer.append(chunk)
        entry.output_buffer_chars += len(chunk)
        entry.output_total_chars += len(chunk)
        entry.last_output_at = time.monotonic()
        # Evict oldest while over cap. Whole chunks are cheap to drop;
        # leave at least one chunk so a single giant payload can be
        # tail-trimmed below rather than evicted to nothing.
        while entry.output_buffer_chars > OUTPUT_BUFFER_CHAR_CAP and len(entry.output_buffer) > 1:
            oldest = entry.output_buffer.popleft()
            entry.output_buffer_chars -= len(oldest)
        if entry.output_buffer_chars > OUTPUT_BUFFER_CHAR_CAP and entry.output_buffer:
            head = entry.output_buffer[0]
            overflow = entry.output_buffer_chars - OUTPUT_BUFFER_CHAR_CAP
            entry.output_buffer[0] = head[overflow:]
            entry.output_buffer_chars -= overflow

    @staticmethod
    def _join_buffer(entry: PTYEntry) -> str:
        if not entry.output_buffer:
            return ""
        return "".join(entry.output_buffer)

    def read_buffer_for_pane(
        self,
        pane_id: str,
        *,
        tail_chars: int,
        since_token: str | None,
        raw: bool,
    ) -> dict[str, Any]:
        """Read recent output for ``pane_id`` from the ring buffer.

        - ``tail_chars``: max chars to return when ``since_token`` is None.
        - ``since_token``: opaque cursor from a previous call; returns
          only chars produced after that point. If the buffer has
          rotated past the cursor, ``truncated`` is True and the oldest
          available chars are returned instead. A cursor that predates
          the buffer, or is otherwise out of range (negative, or ahead
          of what this pane has produced — e.g. a stale cursor a client
          carried over from a previous lifetime of the pane), is
          clamped into range rather than erroring: a fresh reattach must
          always get a replay, never nothing.
        - ``raw``: when False, ANSI escapes are stripped from the
          returned text. The cursor (``next_token``) is always in raw
          char count terms so a delta-read round-trip is exact.

        Returns ``{ok, pane_id, text, next_token, truncated, alive}`` —
        an opaque protocol; the kernel tool reshapes it for the assistant.
        """
        entry = self._ptys.get(pane_id)
        if entry is None:
            return {"ok": False, "error": "pane_not_found"}
        total = entry.output_total_chars
        joined = self._join_buffer(entry)
        if since_token is not None:
            try:
                cursor = int(since_token)
            except (TypeError, ValueError):
                return {"ok": False, "error": "bad_since_token"}
            cursor = max(0, min(cursor, total))
            # buffer holds the last `len(joined)` chars; cursor maps to
            # offset `cursor - (total - len(joined))` inside the buffer.
            buffer_start = total - len(joined)
            if cursor < buffer_start:
                # Cursor predates the buffer — caller missed bytes.
                slice_text = joined
                truncated = True
            else:
                slice_text = joined[cursor - buffer_start :]
                truncated = False
        else:
            if tail_chars <= 0 or tail_chars >= len(joined):
                slice_text = joined
            else:
                slice_text = joined[-tail_chars:]
            truncated = tail_chars > 0 and tail_chars < len(joined)
        text = slice_text if raw else strip_ansi(slice_text)
        return {
            "ok": True,
            "pane_id": pane_id,
            "text": text,
            "next_token": str(total),
            "truncated": truncated,
            "alive": entry.proc.isalive(),
            "byte_count": total,
        }

    async def wait_idle_for_pane(
        self,
        pane_id: str,
        *,
        quiescence_ms: float,
        pattern: str | None,
        timeout_ms: float,
        tail_chars: int,
    ) -> dict[str, Any]:
        """Block until the pane has been quiet for ``quiescence_ms``,
        until ``pattern`` matches the recent tail, or until
        ``timeout_ms`` elapses. The pattern is matched against the
        ANSI-stripped tail of length ``tail_chars`` so the caller can
        anchor to operator-readable text.
        """
        entry = self._ptys.get(pane_id)
        if entry is None:
            return {"ok": False, "error": "pane_not_found"}
        try:
            compiled = re.compile(pattern) if pattern else None
        except re.error as exc:
            return {"ok": False, "error": f"bad_pattern:{exc}"}
        start = time.monotonic()
        deadline = start + max(timeout_ms, 0.0) / 1000.0
        poll_interval = max(quiescence_ms / 4.0, WAIT_IDLE_MIN_POLL_MS) / 1000.0

        def _tail() -> str:
            joined = self._join_buffer(entry)
            stripped = strip_ansi(joined)
            return stripped if tail_chars <= 0 else stripped[-tail_chars:]

        while True:
            # Dead-pane short-circuit. A crashed subprocess stops
            # updating last_output_at, so the quiescence check would
            # eventually return status='idle' and trick the assistant into
            # proceeding as if the CLI was simply done. Surface 'closed'
            # so the caller knows to re-spawn / report failure.
            if not entry.proc.isalive():
                return {
                    "ok": True,
                    "status": "closed",
                    "alive": False,
                    "waited_ms": (time.monotonic() - start) * 1000.0,
                    "tail": _tail(),
                }
            if compiled is not None:
                match = compiled.search(_tail())
                if match:
                    return {
                        "ok": True,
                        "status": "matched",
                        "waited_ms": (time.monotonic() - start) * 1000.0,
                        "tail": _tail(),
                        "match": match.group(0),
                    }
            now = time.monotonic()
            idle_for_ms = (now - entry.last_output_at) * 1000.0
            if idle_for_ms >= quiescence_ms:
                return {
                    "ok": True,
                    "status": "idle",
                    "waited_ms": (now - start) * 1000.0,
                    "tail": _tail(),
                    "idle_ms": idle_for_ms,
                }
            if now >= deadline:
                return {
                    "ok": True,
                    "status": "timeout",
                    "waited_ms": (now - start) * 1000.0,
                    "tail": _tail(),
                }
            # Sleep at most until the deadline; don't oversleep past it.
            await asyncio.sleep(min(poll_interval, max(deadline - now, 0.0)))

    async def _provision_terminal_mcp(self, cwd: str | None = None) -> None:
        """Wire the operator's claude/codex config to the embedded MCP hub, so
        a hand-launched CLI in this pane wakes up already connected — from
        whatever directory they end up in, not only the one the pane opened.

        The pane inherits this process's environment, which carries the hub
        bearer token; a shell the operator opened themselves does not. That is
        what separates "launched inside TESSERACT" from any other terminal.

        `cwd` is no longer where the config is written — it is where a stale
        project-scope `.mcp.json` from the previous scheme may still sit,
        shadowing what we just provisioned.

        Terminal panes are general-purpose shells (cmd/bash/powershell
        too), not committed to running an MCP-aware CLI at open time — a
        missing/misconfigured MCP token must not block basic terminal use,
        so failures are logged, never raised into the spawn path."""
        try:
            from tesseract.config.mcp import load_mcp_config
            from tesseract.orchestrator.agent_controller.lanes import (
                mcp_provision,
            )

            await asyncio.to_thread(
                lambda: mcp_provision.provision(
                    "terminal",
                    load_mcp_config(),
                    cleanup_dirs=[Path(cwd) if cwd else Path.cwd()],
                )
            )
        except Exception:  # noqa: BLE001 — best-effort, must not block terminal open
            log.warning("pty: mcp_provision(terminal) failed", exc_info=True)

    def list_panes_for_agent(self) -> list[dict[str, Any]]:
        """Snapshot of all live panes — read-only pane-viewer substrate."""
        rows: list[dict[str, Any]] = []
        now = time.monotonic()
        for pid, entry in self._ptys.items():
            rows.append({
                "pane_id": pid,
                "shell": entry.shell,
                "owner": entry.owner,
                "alive": entry.proc.isalive(),
                "observer_enabled": entry.observer_enabled,
                "age_ms": (now - entry.spawned_at) * 1000.0,
                "idle_ms": (now - entry.last_output_at) * 1000.0,
                "byte_count": entry.output_total_chars,
                "buffer_chars": entry.output_buffer_chars,
            })
        return rows

    async def _open_for_agent(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Spawn a brand new PTY for a dual-use ``open`` call.

        Requires the Mirror to be up and an operator WS to have sent at
        least one message (so ``app["primary_ws"]`` is non-None). Without
        a renderer the pane would spawn but be invisible to the
        operator — operator-visibility is a contract requirement
        (``_shared/pty-protocol.md`` § Lifecycle), so a missing WS is a
        hard refusal, not a silent fallback.

        Callers pass only ``name`` + ``command`` (+ optional ``cwd``):
        `start_controller_session.py` (``launch_terminal=True``) and
        boot-time `reattach_operator_panes` both spawn a `agent --session
        <id>` viewer pane through this path.
        """
        command = payload.get("command")
        if not isinstance(command, list) or not command:
            return {"ok": False, "error": "command_required"}
        if not all(isinstance(part, str) and part for part in command):
            return {"ok": False, "error": "command_must_be_list_of_nonempty_strings"}
        name = payload.get("name") or command[0]
        cwd = payload.get("cwd")
        if cwd is not None and not isinstance(cwd, str):
            return {"ok": False, "error": "cwd_must_be_string"}
        if cwd:
            # A pane is where a hand-launched claude/codex actually runs, and
            # nothing downstream can see what it edits. Refuse the cwd rather
            # than the writes — this is the last moment the runtime controls.
            # Returns the pane API's error shape; it is not exception-based.
            from tesseract.orchestrator.seal_guard import (
                SealViolation,
                assert_cwd_outside_seal,
            )

            try:
                assert_cwd_outside_seal(cwd)
            except SealViolation as exc:
                log.warning("pty: refused sealed cwd %r", cwd)
                return {"ok": False, "error": f"sealed_tree:{exc}"}

        ws = self._app.get("primary_ws") if self._app is not None else None
        if ws is None or ws.closed:
            return {"ok": False, "error": "no_primary_ws"}
        if PtyProcess is None:
            return {"ok": False, "error": f"winpty_unavailable:{_WINPTY_IMPORT_ERROR}"}

        pane_id = f"pty_{uuid.uuid4().hex[:12]}"
        async with self._lock:
            if pane_id in self._ptys:  # vanishingly unlikely, but guard anyway
                return {"ok": False, "error": "pane_id_collision"}
            if len(self._ptys) >= self._max_ptys:
                return {"ok": False, "error": f"pty_cap_reached:{self._max_ptys}"}
            try:
                spawn_kwargs: dict[str, Any] = {"dimensions": (DEFAULT_ROWS, DEFAULT_COLS)}
                if cwd:
                    spawn_kwargs["cwd"] = cwd
                proc = await asyncio.to_thread(PtyProcess.spawn, list(command), **spawn_kwargs)
            except Exception as exc:
                log.exception("pty: agent open failed for command %r", command)
                return {"ok": False, "error": f"spawn_failed:{exc}"}
            entry = PTYEntry(
                pane_id=pane_id,
                shell=str(name),
                proc=proc,
                ws=ws,
                owner="entity",  # the assistant/viewer-spawned — no operator handoff needed
            )
            entry.reader_task = self._spawn_tracked(
                self._reader_loop(entry),
                f"pty_reader:{pane_id}",
            )
            self._ptys[pane_id] = entry

        # Phase 6 — auto-grant observer consent on agent-spawned panes too.
        self._maybe_auto_grant_consent(pane_id)
        # A hand-launched claude/codex in this pane should wake up already
        # connected to the hub.
        await self._provision_terminal_mcp(cwd)

        opened_at = datetime.now(timezone.utc).isoformat()
        await self._send(ws, {
            "type": "terminal_started",
            "pane_id": pane_id,
            "backend": "winpty",
            "observer_enabled": entry.observer_enabled,
            "agent_spawned": True,
            "name": str(name),
        })
        return {"ok": True, "pane_id": pane_id, "opened_at": opened_at}

    # ── command handlers ──────────────────────────────────────────────

    async def _start(self, pane_id: str, shell: str, ws: web.WebSocketResponse) -> None:
        async with self._lock:
            if pane_id in self._ptys:
                log.debug("pty: start for existing pane_id %s — ignoring", pane_id)
                return
            if len(self._ptys) >= self._max_ptys:
                await self._send(ws, {
                    "type": "terminal_error",
                    "pane_id": pane_id,
                    "message": f"pty cap reached ({self._max_ptys})",
                })
                return
            profile = self._cfg.shell_profiles.get(shell)
            if profile is None:
                await self._send(ws, {
                    "type": "terminal_error",
                    "pane_id": pane_id,
                    "message": f"unknown shell: {shell}",
                })
                return
            if PtyProcess is None:
                await self._send(ws, {
                    "type": "terminal_error",
                    "pane_id": pane_id,
                    "message": f"terminal backend unavailable (pywinpty not installed: {_WINPTY_IMPORT_ERROR})",
                })
                return
            try:
                proc = await asyncio.to_thread(
                    PtyProcess.spawn,
                    list(profile.argv),
                    dimensions=(DEFAULT_ROWS, DEFAULT_COLS),
                )
            except Exception as exc:
                log.exception("pty: spawn failed for %s", shell)
                await self._send(ws, {
                    "type": "terminal_error",
                    "pane_id": pane_id,
                    "message": f"spawn failed: {exc}",
                })
                return
            entry = PTYEntry(pane_id=pane_id, shell=shell, proc=proc, ws=ws)
            entry.reader_task = self._spawn_tracked(
                self._reader_loop(entry),
                f"pty_reader:{pane_id}",
            )
            self._ptys[pane_id] = entry

        # Phase 6 — observer always-on. Per operator (2026-05-16): "remove
        # the observer asking if he can see the terminal." When the
        # global observer is armed, every new pane is auto-consented so
        # The assistant sees terminal activity without an extra confirmation.
        # Operator can still toggle the whole observer off via the
        # right-panel arm/disarm control — that path clears all consents.
        self._maybe_auto_grant_consent(pane_id)
        # A hand-launched claude/codex in this pane should wake up already
        # connected to the hub.
        await self._provision_terminal_mcp()

        await self._send(ws, {
            "type": "terminal_started",
            "pane_id": pane_id,
            "backend": "winpty",
            "observer_enabled": entry.observer_enabled,
        })

    async def _keystroke(self, pane_id: str, data: str) -> None:
        entry = self._ptys.get(pane_id)
        if entry is None or not entry.proc.isalive():
            return
        try:
            await asyncio.to_thread(entry.proc.write, data)
        except (OSError, EOFError):
            log.debug("pty: write failed for %s", pane_id)

    async def _resize(self, pane_id: str, cols: int, rows: int) -> None:
        entry = self._ptys.get(pane_id)
        if entry is None or cols <= 0 or rows <= 0:
            return
        try:
            await asyncio.to_thread(entry.proc.setwinsize, rows, cols)
        except Exception:
            log.exception("pty: resize failed for %s", pane_id)

    async def _stop(self, pane_id: str, *, notify: bool = True) -> None:
        entry = self._ptys.pop(pane_id, None)
        if entry is None:
            return
        entry.closed_at = time.monotonic()
        self.revoke_consent(pane_id)
        try:
            if entry.proc.isalive():
                await asyncio.to_thread(entry.proc.terminate, True)
        except Exception:
            log.debug("pty: terminate raised for %s", pane_id)
        if entry.reader_task and not entry.reader_task.done():
            entry.reader_task.cancel()
        if notify:
            await self._send(entry.ws, {"type": "terminal_stopped", "pane_id": pane_id})

    async def _pause(self, pane_id: str) -> None:
        entry = self._ptys.get(pane_id)
        if entry is None:
            return
        entry.resume_event.clear()

    async def _resume(self, pane_id: str) -> None:
        entry = self._ptys.get(pane_id)
        if entry is None:
            return
        entry.resume_event.set()

    def _detach(self, pane_id: str) -> None:
        """Mark ``pane_id`` detached (owning WS dropped) and start its
        grace timer. The process keeps running; `_reader_loop`'s WS sends
        just no-op against the now-closed ``entry.ws`` (`_send` already
        guards on `ws.closed`) until a client reattaches."""
        entry = self._ptys.get(pane_id)
        if entry is None or entry.detached_at is not None:
            return
        entry.detached_at = time.monotonic()
        self._spawn_tracked(
            self._grace_expire(pane_id, entry.detached_at),
            f"pty_grace:{pane_id}",
        )

    async def _grace_expire(self, pane_id: str, detached_at: float) -> None:
        await asyncio.sleep(self._cfg.reattach_grace_s)
        entry = self._ptys.get(pane_id)
        # `detached_at` mismatch means the pane was reattached (or torn
        # down and re-spawned under the same id, vanishingly unlikely)
        # since this timer was scheduled — don't kill a live reattach.
        if entry is None or entry.detached_at != detached_at:
            return
        log.info("pty: reattach grace period expired for %s — killing", pane_id)
        await self._stop(pane_id, notify=False)

    async def _reattach(
        self, pane_id: str, since_token: str | None, ws: web.WebSocketResponse,
    ) -> None:
        """Client-initiated reattach after a WS reconnect (page reload,
        brief network drop). Replays only the gap since the client's
        last-seen cursor — reuses `output_total_chars` / `read_buffer_
        for_pane`, no second buffer. A second WS claiming an already-
        attached pane simply repoints `entry.ws` (last-claimer-wins,
        the same implicit semantic `dispatch` already applies to
        `primary_ws` — no new exclusivity lock invented here)."""
        entry = self._ptys.get(pane_id)
        if entry is None or not entry.proc.isalive():
            await self._send(ws, {"type": "terminal_reattach_failed", "pane_id": pane_id})
            return
        entry.ws = ws
        entry.detached_at = None
        entry.resume_event.set()  # pause state resets on reattach
        # Snapshot the replay synchronously, before the first `await`
        # below. `entry.ws` is already repointed at this point, so if the
        # snapshot were taken after an `await` (e.g. after sending
        # `terminal_reattached`), the reader loop could flush live output
        # to the new ws during that await AND have those same bytes
        # included in the replay computed afterward — a double-echo. Fixing
        # the snapshot boundary here, before any interleaving is possible,
        # closes that race.
        replay = self.read_buffer_for_pane(
            pane_id, tail_chars=0, since_token=since_token, raw=True,
        )
        await self._send(ws, {
            "type": "terminal_reattached",
            "pane_id": pane_id,
            "backend": "winpty",
            "observer_enabled": entry.observer_enabled,
        })
        if replay.get("ok") and replay.get("text"):
            # `replay: True` — live-gate fix pass (2026-07-05). The
            # replayed bytes can contain terminal query sequences the
            # shell/ConPTY emitted live (device-attributes ESC[c, DSR
            # ESC[6n) that xterm.js auto-answers via onData when it
            # re-parses them. Without this flag the client can't tell
            # this chunk apart from a live stream, so those synthetic
            # answers flowed into the shell's stdin as garbage ahead of
            # the operator's first typed command. The client suppresses
            # `sendKeystroke` for the pane while writing a flagged chunk.
            await self._send(ws, {
                "type": "terminal_output_chunk",
                "pane_id": pane_id,
                "bytes": replay["text"],
                "replay": True,
            })

    async def _observer_toggle(self, pane_id: str, enabled: bool) -> None:
        entry = self._ptys.get(pane_id)
        if entry is None:
            return
        entry.observer_enabled = enabled
        if not enabled:
            self.revoke_consent(pane_id)
        await self._send(entry.ws, {
            "type": "terminal_observer_status",
            "pane_id": pane_id,
            "enabled": enabled,
        })

    # ── observer consent integration ──────────────────────────────────

    def _maybe_auto_grant_consent(self, pane_id: str) -> None:
        """Phase 6 helper — grant observer consent for ``pane_id`` if the
        global observer is armed. Called on every pane spawn (operator-
        side ``_start`` + agent-side ``_open_for_agent``).

        Operator semantics: armed = "I'm OK with the observer seeing
        anything that happens in the Mirror." Per-pane confirmations
        added noise without adding safety — disarm is the single
        operator control.
        """
        if self._app is None:
            return
        state = self._app.get("observer_state")
        if state not in {"armed", "observing"}:
            return
        self.grant_consent(pane_id)
        # Spawning the first pane while merely armed promotes the state
        # to observing — mirrors the WS observer_pane_ack flow.
        if state == "armed":
            self._app["observer_state"] = "observing"

    def grant_consent_for_all_live(self) -> int:
        """Auto-grant consent for every currently-live pane. Used by
        ``routes/observer_consent.arm()`` so re-arming after a disarm
        immediately re-attaches to existing panes without waiting for
        the operator to spawn new ones.
        """
        granted = 0
        for pane_id in list(self._ptys):
            if self._app is None:
                break
            consented = self._app.get("observer_consented_panes")
            if consented is None or pane_id in consented:
                continue
            self.grant_consent(pane_id)
            granted += 1
        return granted

    def grant_consent(self, pane_id: str) -> None:
        """Grant observer consent for a specific live pane.

        Silently drops grants for pane_ids that aren't currently live —
        the frontend can otherwise trick the backend into storing
        consent for arbitrary strings (pr-review SEC-4). Real panes
        sit in `self._ptys` once `_start` succeeds.
        """
        if self._app is None:
            return
        if pane_id not in self._ptys:
            log.debug("pty: grant_consent skipped — no live pane %s", pane_id)
            return
        self._app["observer_consented_panes"].add(pane_id)

    def revoke_consent(self, pane_id: str) -> None:
        if self._app is None:
            return
        self._app["observer_consented_panes"].discard(pane_id)
        observer = self._app.get("observer")
        if observer is not None:
            try:
                observer.drop_pty_for_pane(pane_id)
            except Exception:
                log.exception("observer.drop_pty_for_pane failed for %s", pane_id)

    def _forward_to_observer(self, pane_id: str, text: str) -> None:
        """Fire-and-forget push of a PTY chunk into the observer when
        the consent gate is open. Never awaits — terminal latency must
        not depend on the observer. The scheduled task is registered on
        `app['observer_pty_tasks']` so disarm / WS-cleanup can cancel
        in-flight PTY pushes cleanly. Per-pane queue is capped at
        OBSERVER_PUSH_CAP_PER_PANE to prevent runaway accumulation when
        the observer is slower than the PTY."""
        if self._app is None or not text:
            return
        if self._app.get("observer_state") != "observing":
            return
        if pane_id not in self._app["observer_consented_panes"]:
            return
        observer = self._app.get("observer")
        if observer is None:
            return
        # Per-pane cap is a best-effort soft limit. When there is no PTYEntry
        # (test paths fire _forward_to_observer without a real pane), we
        # skip the cap but still track on app-level so disarm can cancel.
        entry = self._ptys.get(pane_id)
        if entry is not None:
            while len(entry.observer_tasks) >= OBSERVER_PUSH_CAP_PER_PANE:
                try:
                    oldest = next(iter(entry.observer_tasks))
                except StopIteration:
                    break
                oldest.cancel()
                entry.observer_tasks.discard(oldest)
                log.warning(
                    "pty: observer-push cap %d hit for pane %s — dropping oldest",
                    OBSERVER_PUSH_CAP_PER_PANE, pane_id,
                )
        # Phase 5 Task 3 — scrub common secret shapes before the chunk
        # leaves pty_manager. This is the CAPTURE point: the observer
        # (and its transcript, and anything a future suggestion quotes)
        # only ever sees the scrubbed text.
        line = {
            "role": "pty",
            "pane_id": pane_id,
            "text": scrub_secrets(text),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        tasks = self._app.setdefault("observer_pty_tasks", set())
        task = self._spawn_tracked(
            self._observer_push(self._app, pane_id, observer, line),
            f"observer_push:{pane_id}",
        )
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        if entry is not None:
            entry.observer_tasks.add(task)
            task.add_done_callback(entry.observer_tasks.discard)

    @staticmethod
    async def _observer_push(
        app: web.Application,
        pane_id: str,
        observer: Any,
        line: dict[str, Any],
    ) -> None:
        # Re-check consent + observer state inside the task body — closes
        # the TOCTOU window between the synchronous gate in
        # _forward_to_observer and the asyncio tick when this task runs.
        if app.get("observer_state") != "observing":
            return
        if pane_id not in app.get("observer_consented_panes", set()):
            return
        try:
            await observer.feed_pty([line])
        except Exception:
            log.exception("observer.feed_pty failed for PTY line")

    # ── reader loop ───────────────────────────────────────────────────

    async def _reader_loop(self, entry: PTYEntry) -> None:
        # CR-3 — read on a dedicated thread that pushes chunks into an
        # asyncio.Queue. The async side drains the queue with a
        # `wait_for` timeout so we can flush coalesced output without
        # cancelling an in-flight `winpty.read` (which is uncancellable
        # and would lose the chunk it had already consumed).
        #
        # Per-chunk consumers + observer still fire synchronously on
        # every chunk; only the WS frame is batched.
        loop = asyncio.get_running_loop()
        # Sentinel pushed by the reader thread when it terminates.
        # Using object() so it can't collide with any real chunk.
        eof_sentinel: object = object()
        queue: asyncio.Queue[str | object] = asyncio.Queue()
        # Review follow-up (F2, 2026-07-05) — the queue has no maxsize and
        # the reader thread enqueues via `put_nowait`, so a chatty child
        # process during a sustained pause (client crossed WATERMARK_HIGH,
        # e.g. a throttled background tab) would otherwise grow it without
        # bound. Track chars enqueued while paused; once they exceed the
        # config cap, force the drain to resume (memory stays bounded, the
        # client-side xterm write-buffer absorbs the resulting burst —
        # xterm discards beyond its own 50MB cap).
        paused_queued_chars = 0

        def _enqueue(chunk: str) -> None:
            """Scheduled via ``call_soon_threadsafe`` — runs on the event
            loop thread, so touching ``paused_queued_chars`` and
            ``entry.resume_event`` here (unlike from the reader thread
            itself) is safe."""
            nonlocal paused_queued_chars
            queue.put_nowait(chunk)
            if entry.resume_event.is_set():
                paused_queued_chars = 0
                return
            paused_queued_chars += len(chunk)
            if paused_queued_chars > self._cfg.pause_buffer_cap_chars:
                log.debug(
                    "pty: pause buffer cap %d exceeded for %s — forcing resume",
                    self._cfg.pause_buffer_cap_chars, entry.pane_id,
                )
                entry.resume_event.set()
                paused_queued_chars = 0

        def _reader_thread() -> None:
            """Blocking-read loop. Pushes chunks to ``queue`` via
            ``call_soon_threadsafe``; pushes ``eof_sentinel`` and exits
            on EOF / OS error / dead proc."""
            try:
                while True:
                    try:
                        chunk = entry.proc.read(READ_CHUNK_CHARS)
                    except EOFError:
                        break
                    except OSError:
                        log.exception("pty: read failed for %s", entry.pane_id)
                        break
                    if not chunk:
                        if not entry.proc.isalive():
                            break
                        continue
                    try:
                        loop.call_soon_threadsafe(_enqueue, chunk)
                    except RuntimeError:
                        # Loop closed mid-stream — async side has gone away.
                        return
            finally:
                # Loop may already be closed by the time this thread exits
                # (process shutdown sequence: terminate proc → winpty.read
                # raises → finally runs after web.run_app's loop.close()).
                # Guard the cross-thread schedule to avoid noisy
                # `Event loop is closed` stderr on clean shutdown.
                if not loop.is_closed():
                    try:
                        loop.call_soon_threadsafe(queue.put_nowait, eof_sentinel)
                    except RuntimeError:
                        pass

        reader = threading.Thread(
            target=_reader_thread,
            name=f"pty_reader_thread:{entry.pane_id}",
            daemon=True,
        )
        reader.start()

        pending: list[str] = []
        pending_chars = 0
        first_pending_at: float | None = None
        coalesce_window_s = self._cfg.coalesce_flush_ms / 1000.0
        coalesce_flush_chars = self._cfg.coalesce_flush_chars

        async def _flush_pending() -> None:
            nonlocal pending_chars, first_pending_at
            if not pending:
                return
            payload = "".join(pending)
            pending.clear()
            pending_chars = 0
            first_pending_at = None
            await self._send(entry.ws, {
                "type": "terminal_output_chunk",
                "pane_id": entry.pane_id,
                "bytes": payload,
            })

        try:
            while True:
                # F2 (terminal daily-driver 2026-07-05) — flow control.
                # While paused, don't drain the queue at all; the reader
                # thread keeps pushing chunks into (unbounded) Python
                # memory rather than being killed, per the brief's
                # "simplest correct" pause primitive.
                await entry.resume_event.wait()

                if first_pending_at is not None:
                    remaining = (first_pending_at + coalesce_window_s) - time.monotonic()
                    wait_timeout: float | None = max(0.001, remaining)
                else:
                    wait_timeout = None

                try:
                    if wait_timeout is None:
                        item = await queue.get()
                    else:
                        item = await asyncio.wait_for(queue.get(), timeout=wait_timeout)
                except asyncio.TimeoutError:
                    # Coalesce window elapsed — flush whatever's pending,
                    # unless a pause raced in after this wait started (the
                    # top-of-loop `resume_event.wait()` gate only guards
                    # the NEXT iteration, not a wait already in flight).
                    # The reader thread keeps reading; queued chunks are
                    # preserved, no data loss possible.
                    if entry.resume_event.is_set():
                        await _flush_pending()
                    continue

                if item is eof_sentinel:
                    break
                chunk = item  # type: ignore[assignment]
                if not isinstance(chunk, str) or not chunk:
                    continue

                # Phase 2 — feed the per-pane ring buffer + quiescence
                # clock. Done before any WS work so a slow operator
                # browser can't starve the assistant's reads of recent output.
                self._append_to_buffer(entry, chunk)

                # Per-chunk side-effects fire BEFORE coalescing so
                # end-of-turn detectors and the observer see chunks at
                # their true granularity.
                if entry.observer_enabled:
                    self._forward_to_observer(entry.pane_id, chunk)
                # MO-6 output consumers.
                # Iterate over a snapshot so a consumer that unregisters
                # itself (e.g. on close) doesn't mutate the list mid-loop.
                for consumer in list(entry.output_consumers):
                    try:
                        consumer(chunk)
                    except Exception:
                        log.exception(
                            "pty: output consumer raised for pane %s — "
                            "continuing", entry.pane_id,
                        )

                # Coalesce buffer.
                pending.append(chunk)
                pending_chars += len(chunk)
                if first_pending_at is None:
                    first_pending_at = time.monotonic()
                # F2 — a chunk can arrive here from a `queue.get()` that
                # was already in flight when a pause landed (the
                # top-of-loop gate only blocks the NEXT iteration). Guard
                # the flush itself so a paused pane never forwards to the
                # WS; the chunk stays buffered in `pending` until resumed
                # (or the final drain on loop exit, whichever is first).
                if not entry.resume_event.is_set():
                    continue
                if pending_chars >= coalesce_flush_chars:
                    # Sustained/bursty output hit the size cap — flush.
                    await _flush_pending()
                elif queue.empty():
                    # F1 — nothing else is immediately available and the
                    # payload is still small: this is the interactive-echo
                    # shape (a keystroke's output, or a lull between
                    # bursts). Flush now instead of holding for the
                    # coalesce window so typing echo doesn't sit in the
                    # buffer for up to `coalesce_flush_ms`.
                    await _flush_pending()
        except asyncio.CancelledError:
            raise
        finally:
            # Final drain — any buffered bytes left when the loop exits
            # still need to reach the operator. Swallow send failures;
            # if WS is also dead there is nothing actionable.
            if pending:
                try:
                    await _flush_pending()
                except Exception:
                    log.debug("pty: final drain send failed", exc_info=True)
            # pop() is the atomic gate — if _stop ran first, it already popped and notified.
            if self._ptys.pop(entry.pane_id, None) is not None:
                await self._send(entry.ws, {
                    "type": "terminal_stopped",
                    "pane_id": entry.pane_id,
                })

    # ── raw send (NOT enveloped) ──────────────────────────────────────

    @staticmethod
    async def _send(ws: web.WebSocketResponse, payload: dict[str, Any]) -> None:
        if ws.closed:
            return
        try:
            await ws.send_json(payload)
        except ConnectionResetError:
            log.debug("pty: ws closed mid-send")

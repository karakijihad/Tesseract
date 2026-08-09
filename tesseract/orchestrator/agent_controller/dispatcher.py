"""Shared dispatcher — the single entry point every surface uses to
hand work to a agent controller session.

One primitive, many callers:

* Autonomy kernel runner (via ``delegate_agent_controller`` tool)
* Mirror chat brain (via ``start_controller_session`` tool)
* Scheduler / workspace cards / anything else with work to dispatch
* The ``agent`` CLI itself (uses :func:`ensure_daemon_running` to
  self-bootstrap when no daemon is on disk)

Lifecycle the dispatcher owns:

1. ``ensure_daemon_running()`` — probes ``controller.port`` + a TCP
   connect; if dead, spawns ``python -m tesseract.scripts.agent_controller``
   as a detached sibling (``CREATE_NEW_PROCESS_GROUP`` on Windows /
   ``start_new_session`` on POSIX) and waits for the port file +
   handshake to come up. Returns silently when the daemon was already
   alive (the supervisor or another ``agent`` window already booted it).
2. :func:`dispatch_to_controller` — opens an IPC client, mints a session
   via ``new_session(origin, mode, title)``, sends ``user_input(prompt)``,
   then either:

   * ``wait_for_completion=True`` — tails ``transcript_event`` pushes
     until an ``assistant_text`` event closes (``partial=False``),
     accumulates text, returns a :class:`DispatchResult`.
   * ``wait_for_completion=False`` — returns the session_id immediately
     so the caller can write a ``child_transcript_ref`` event on its
     own parent transcript and an operator can ``agent --session <id>``
     to attach later.

The dispatcher is deliberately transport-light: every caller speaks the
same IPC contract, the same ``origin`` taxonomy, and the same
"wait-or-detach" boundary. Adding a new surface (workspace card,
telegram channel, ...) means writing a thin wrapper that calls
:func:`dispatch_to_controller` with the right ``origin`` literal —
not re-implementing the IPC dance.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
from dataclasses import dataclass, field, replace
from typing import Any, Awaitable, Callable, Literal

from .lanes.principals import OPERATOR_PRINCIPAL
from .ipc_client import ControllerClient, ControllerClientError
from .paths import port_file_path

log = logging.getLogger(__name__)


# ── public types ──────────────────────────────────────────────────────


DispatchOrigin = Literal[
    "cli", "mirror", "autonomy", "scheduler", "telegram"
]
"""Provenance tag carried on every ``new_session``. Mirrors
:data:`agent_controller.sessions.SessionOrigin`. Each surface owns its
own literal so the operator's session list / journal can group by
"who dispatched this"."""


DispatchMode = Literal[
    "chat", "autonomy", "scheduler"
]
"""Session mode — see :data:`agent_controller.sessions.SessionMode`."""


@dataclass(frozen=True)
class DispatchResult:
    """Return shape of :func:`dispatch_to_controller`.

    ``session_id`` is always populated (every dispatch mints a session).
    ``assistant_text`` is the accumulated reply when
    ``wait_for_completion=True``; empty string in fire-and-forget mode.
    ``saw_assistant_text`` records whether a closed (``partial=False``)
    assistant_text event was actually observed — callers that need
    "did the controller respond?" should check this flag, not the text
    length (an empty closed assistant_text still counts as a response).
    """

    session_id: str
    assistant_text: str = ""
    saw_assistant_text: bool = False
    timed_out: bool = False
    cancelled: bool = False
    error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def success(self) -> bool:
        return (
            self.saw_assistant_text
            and not self.timed_out
            and not self.cancelled
            and not self.error
        )


class DispatcherError(RuntimeError):
    """Raised when the dispatcher cannot reach a usable daemon —
    spawn failed, port file never appeared, handshake rejected."""


def _fallback_title(prompt: str, *, limit: int = 200) -> str:
    """First line of ``prompt``, bounded — same precedent as the delegate
    spawn's goal snippet (``brain/spawns.py::_bounded_one_line``). Task 6.1:
    without this, a caller that omits ``title`` gets a session whose
    ActivityRecord label falls back to the bare ``mode`` string
    (``sessions.py::create_session``: ``label=record.title or record.mode``)
    instead of anything goal-derived."""
    first = prompt.strip().splitlines()[0] if prompt.strip() else ""
    return first[:limit]


# ── daemon bootstrap ──────────────────────────────────────────────────


# Controller cold start is dominated by ``ControllerRuntime.initial_build``
# (adapter chain + ~100-tool registry + system prompt assemble) plus
# Windows CreateProcess + DLL-load cost. Measured 5-15s on healthy
# hardware. The earlier 5s budget surfaced false "daemon spawned but did
# not become reachable" errors when the daemon was still booting. 25s is
# generous without making genuine spawn failures painful to wait through.
_DEFAULT_SPAWN_WAIT_SECONDS = 25.0
_PORT_FILE_POLL_INTERVAL = 0.1


def _is_daemon_alive(timeout: float = 0.5) -> bool:
    """Cheap check: port file on disk + TCP connect succeeds.

    Uses the same primitive as ``controller_port_alive`` but without
    the TTL cache — the dispatcher only calls this once per dispatch
    and the kernel's TTL cache is keyed by autonomy-tick semantics
    that don't apply here.
    """
    import socket

    path = port_file_path()
    if not path.exists():
        return False
    try:
        port = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    if port <= 0 or port > 65535:
        return False
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        try:
            sock.close()
        except OSError:
            pass


def _spawn_log_path() -> "Path":
    """Where the spawned daemon's stderr lands so the dispatcher can
    quote the boot error if the spawn fails. Resolved at call time so
    monkeypatched ``TESSERACT_HOME`` is honored.
    """
    from pathlib import Path as _Path

    return port_file_path().parent / "controller-spawn.log"


def _spawn_daemon_subprocess() -> subprocess.Popen:
    """Spawn ``python -m tesseract.scripts.agent_controller`` as a
    detached sibling. Matches the supervisor's spawn pattern so the
    daemon survives the spawning process exiting (the ``agent`` CLI
    window closes; the daemon keeps running).

    stderr is captured to ``<TESSERACT_HOME>/run/controller-spawn.log``
    so a boot crash surfaces with a real traceback instead of vanishing
    into DEVNULL.
    """
    cmd = [sys.executable, "-m", "tesseract.scripts.agent_controller"]
    env = os.environ.copy()
    log_path = _spawn_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # Truncate the prior boot's log so the dispatcher's error message
    # quotes only THIS spawn's stderr.
    log_fh = open(log_path, "wb", buffering=0)
    kwargs: dict[str, Any] = {
        "env": env,
        "stdout": log_fh,
        "stderr": subprocess.STDOUT,
        "stdin": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
        )
    else:
        kwargs["start_new_session"] = True
    log.info("dispatcher: spawning controller daemon cmd=%s", cmd)
    return subprocess.Popen(cmd, **kwargs)  # noqa: S603 — args list, no shell


def _spawn_lock_path() -> "Path":
    """Path of the spawn-lock file. Resolved at call time so a
    monkeypatched ``TESSERACT_HOME`` is honored."""
    from pathlib import Path as _Path

    return port_file_path().parent / "controller-spawn.lock"


def _try_acquire_spawn_lock() -> bool:
    """Reviewer Bug 3 mitigation: only one process spawns a daemon at
    a time. Uses ``O_CREAT | O_EXCL`` so two concurrent ``agent``
    invocations land on a single spawn (the second observes the lock
    + waits for the first's daemon to come up via the normal
    poll-port-file loop, instead of starting a parallel daemon that
    would overwrite the port file).

    Returns ``True`` if this caller holds the lock and should proceed
    with spawn. ``False`` means another process is already spawning —
    the caller should just keep polling the port file.

    Stale-lock cleanup: a lock older than ``_SPAWN_LOCK_TTL_SECONDS``
    is removed before the exclusive-create attempt. Protects against a
    crashed prior spawn leaving an orphan lock.
    """
    lock = _spawn_lock_path()
    lock.parent.mkdir(parents=True, exist_ok=True)
    if lock.exists():
        try:
            age = (
                __import__("time").time() - lock.stat().st_mtime
            )
            if age > _SPAWN_LOCK_TTL_SECONDS:
                lock.unlink(missing_ok=True)
        except OSError:
            pass
    try:
        fd = os.open(
            str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
        )
    except FileExistsError:
        return False
    try:
        os.write(fd, str(os.getpid()).encode("utf-8"))
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
    return True


def _release_spawn_lock() -> None:
    """Remove the spawn lock. Idempotent — never raises."""
    try:
        _spawn_lock_path().unlink(missing_ok=True)
    except OSError:
        log.debug("dispatcher: spawn-lock unlink raised", exc_info=True)


_SPAWN_LOCK_TTL_SECONDS = 30.0


async def ensure_daemon_running(
    *,
    spawn_wait_seconds: float = _DEFAULT_SPAWN_WAIT_SECONDS,
    spawn_if_missing: bool = True,
) -> bool:
    """Guarantee a controller daemon is reachable.

    Returns ``True`` if a daemon was already alive (no-op), or if one
    was spawned and came up within ``spawn_wait_seconds``. Raises
    :class:`DispatcherError` if spawning failed or the spawned daemon
    never wrote its port file in time.

    Callers that want "attach-or-nothing" semantics (e.g. an autonomy
    runner that prefers to skip the dispatch when the daemon is down)
    pass ``spawn_if_missing=False`` — the function returns ``False``
    without raising.

    Concurrent invocations are guarded by a spawn-lock file under
    ``<TESSERACT_HOME>/run/controller-spawn.lock`` so two simultaneous
    ``agent`` calls + a supervisor boot don't all try to spawn three
    daemons that race for the port file (reviewer Bug 3).
    """
    # Off the loop: `_is_daemon_alive` is a blocking socket connect, and this
    # is now on the delegation path, which fans out. A stalled loop is a
    # stalled heartbeat and a stalled inbound turn.
    if await asyncio.to_thread(_is_daemon_alive):
        return True
    if not spawn_if_missing:
        return False

    we_hold_lock = _try_acquire_spawn_lock()
    if we_hold_lock:
        try:
            _spawn_daemon_subprocess()
        except OSError as exc:
            _release_spawn_lock()
            raise DispatcherError(
                f"dispatcher: spawn failed: {exc}"
            ) from exc

    # Poll port file + TCP connect until alive or budget elapsed.
    # Non-lock-holders also wait here — once the lock-holder's daemon
    # writes its port file, every waiter observes it on the next probe.
    loop = asyncio.get_running_loop()
    deadline = loop.time() + spawn_wait_seconds
    try:
        while loop.time() < deadline:
            if await asyncio.to_thread(_is_daemon_alive):
                return True
            await asyncio.sleep(_PORT_FILE_POLL_INTERVAL)
    finally:
        if we_hold_lock:
            _release_spawn_lock()

    raise DispatcherError(
        f"dispatcher: daemon spawned but did not become reachable "
        f"within {spawn_wait_seconds:.1f}s"
    )


# ── dispatch ──────────────────────────────────────────────────────────


_DEFAULT_IDLE_TIMEOUT_SECONDS = 60.0
_DEFAULT_CONNECT_TIMEOUT_SECONDS = 5.0


async def dispatch_to_controller(
    prompt: str,
    *,
    origin: DispatchOrigin,
    title: str | None = None,
    mode: DispatchMode = "chat",
    preferred_seat: str | None = None,
    wait_for_completion: bool = True,
    idle_timeout_seconds: float = _DEFAULT_IDLE_TIMEOUT_SECONDS,
    connect_timeout_seconds: float = _DEFAULT_CONNECT_TIMEOUT_SECONDS,
    spawn_if_missing: bool = True,
    cancel_event: asyncio.Event | None = None,
    on_session_started: Callable[[str], Awaitable[None]] | None = None,
    owner_principal: str = OPERATOR_PRINCIPAL,
) -> DispatchResult:
    """Send ``prompt`` to a freshly-minted controller session.

    Single primitive every surface uses:

    * Autonomy: ``origin="autonomy"``, ``wait_for_completion=True``,
      ``idle_timeout_seconds`` = the kernel's per-item budget.
    * Mirror chat: ``origin="mirror"``, ``wait_for_completion=False``
      — chat gets back the session_id immediately and writes a
      ``child_transcript_ref`` event so the operator can attach later.
    * Scheduler: ``origin="scheduler"``, ``wait_for_completion`` per
      job semantics.
    * Workspace card: ``origin="mirror"`` (closest semantic — the card
      lives in the Mirror UI), ``wait_for_completion=False``.

    ``on_session_started`` (M-3) fires once, with the minted session_id, the
    instant the session exists and before any await — a caller passes a hook
    that persists the id so a backend restart can reattach (see
    :func:`reattach_to_controller`). The hook is best-effort: a raised
    exception is logged and swallowed so a persistence failure cannot fail an
    otherwise-working dispatch.
    """
    if not isinstance(prompt, str) or not prompt.strip():
        raise DispatcherError("dispatch_to_controller: prompt must be non-empty")

    # Honor the attach-or-nothing contract: with spawn_if_missing=False a
    # down daemon returns False (not raise). Gate on it so we don't attempt
    # a doomed TCP connect against a known-down socket. (With the default
    # spawn_if_missing=True this returns True or raises, so the guard is a
    # no-op for existing callers.)
    if not await ensure_daemon_running(spawn_if_missing=spawn_if_missing):
        raise DispatcherError(
            "controller daemon not running and spawn_if_missing=False"
        )

    try:
        client = await ControllerClient.connect(
            connect_timeout=connect_timeout_seconds
        )
    except ControllerClientError as exc:
        raise DispatcherError(str(exc)) from exc

    async with client:
        try:
            attached = await client.new_session(
                title=title or _fallback_title(prompt), mode=mode, origin=origin,
                preferred_seat=preferred_seat,
                owner_principal=owner_principal,
            )
        except ControllerClientError as exc:
            raise DispatcherError(
                f"new_session failed: {exc}"
            ) from exc
        session = attached.get("session") or {}
        session_id = session.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise DispatcherError(
                f"controller refused new_session: {attached}"
            )

        # M-3: hand the session_id to the caller before the await so it can be
        # persisted for restart-resume. Best-effort — a failing hook must not
        # fail a working dispatch.
        if on_session_started is not None:
            try:
                await on_session_started(session_id)
            except Exception:  # noqa: BLE001
                log.warning(
                    "dispatch_to_controller: on_session_started hook failed for "
                    "session %s — step will not be resumable",
                    session_id,
                    exc_info=True,
                )

        try:
            await client.user_input(session_id, prompt)
        except ControllerClientError as exc:
            raise DispatcherError(
                f"user_input failed: {exc}"
            ) from exc

        if not wait_for_completion:
            return DispatchResult(
                session_id=session_id,
                metadata={"detached": True},
            )

        return await tail_until_assistant_text(
            client=client,
            session_id=session_id,
            idle_timeout_seconds=idle_timeout_seconds,
            cancel_event=cancel_event,
        )


def _scan_replay_for_assistant(
    replay_events: list[dict[str, Any]], session_id: str
) -> tuple[list[str], bool]:
    """Walk an attach reply's ``replay_events`` (model-dumped transcript
    events) and pull the assistant_text deltas for ``session_id`` in order.

    Returns ``(deltas, saw_closed)`` — ``saw_closed`` True when a
    ``partial=False`` assistant_text event was already on the transcript
    (the session finished before we reattached, so no live tail is needed).
    """
    deltas: list[str] = []
    saw_closed = False
    for event in replay_events:
        if not isinstance(event, dict):
            continue
        if event.get("session_id") not in (session_id, None):
            continue
        if event.get("kind") != "assistant_text":
            continue
        text = event.get("text")
        if isinstance(text, str):
            deltas.append(text)
        if not event.get("partial"):
            saw_closed = True
            break
    return deltas, saw_closed


async def reattach_to_controller(
    session_id: str,
    *,
    idle_timeout_seconds: float = _DEFAULT_IDLE_TIMEOUT_SECONDS,
    connect_timeout_seconds: float = _DEFAULT_CONNECT_TIMEOUT_SECONDS,
    cancel_event: asyncio.Event | None = None,
) -> DispatchResult:
    """Rejoin an EXISTING controller session and recover its reply (M-3).

    Unlike :func:`dispatch_to_controller` this never mints a session and never
    spawns a daemon — reattach only makes sense against the surviving daemon
    that still owns the live session. If the daemon is down (or refuses the
    attach) the function raises :class:`DispatcherError`; callers should
    surface a failure rather than fresh-running a non-idempotent LLM step
    (Contract #3).

    The reply is recovered from the attach transcript replay when the session
    already produced a closed ``assistant_text``; otherwise the function tails
    live pushes for the remainder, joining the replay deltas with the live
    ones (no overlap — replay covers events up to the attach offset, live
    pushes cover events after it).
    """
    if not await ensure_daemon_running(spawn_if_missing=False):
        raise DispatcherError(
            f"controller daemon not running; cannot reattach to {session_id}"
        )
    try:
        client = await ControllerClient.connect(
            connect_timeout=connect_timeout_seconds
        )
    except ControllerClientError as exc:
        raise DispatcherError(str(exc)) from exc

    async with client:
        try:
            # mode is the CLIENT role (interactive|observer), not the session
            # mode — reattach as a plain interactive client and replay from 0.
            attached = await client.attach(session_id, from_offset=0)
        except ControllerClientError as exc:
            raise DispatcherError(
                f"attach failed for session {session_id}: {exc}"
            ) from exc
        sess = attached.get("session") or {}
        if sess.get("session_id") != session_id:
            raise DispatcherError(
                f"controller refused attach for {session_id}: {attached}"
            )

        replay = attached.get("replay_events") or []
        replay_deltas, saw_closed = _scan_replay_for_assistant(replay, session_id)
        if saw_closed:
            return DispatchResult(
                session_id=session_id,
                assistant_text="".join(replay_deltas),
                saw_assistant_text=True,
                metadata={"session_id": session_id, "reattached": True},
            )

        dr = await tail_until_assistant_text(
            client=client,
            session_id=session_id,
            idle_timeout_seconds=idle_timeout_seconds,
            cancel_event=cancel_event,
        )
        return replace(
            dr,
            assistant_text="".join(replay_deltas) + dr.assistant_text,
            metadata={**dr.metadata, "reattached": True},
        )


async def tail_until_assistant_text(
    *,
    client: ControllerClient,
    session_id: str,
    idle_timeout_seconds: float = _DEFAULT_IDLE_TIMEOUT_SECONDS,
    cancel_event: asyncio.Event | None = None,
) -> DispatchResult:
    """Public, timeout-aware variant of the dispatch tail.

    Pulls pushes from the client's inbox one at a time, applying
    ``idle_timeout_seconds`` between pushes. The previous push resets
    the budget. Stops on the first closed (``partial=False``)
    assistant_text event OR on disconnect OR on cancel.

    Reviewer Bug 2 fix: cancel responsiveness was previously limited
    to ``idle_timeout_seconds`` because the loop only checked
    ``cancel_event`` between pushes. Now we race the inbox future
    against ``cancel_event.wait()`` so a cancel fires within one
    event-loop turn no matter how long the controller has been silent.
    """
    assistant_text: list[str] = []
    saw_assistant = False
    timed_out = False
    cancelled = False

    while True:
        # Inbox-get future is created OUTSIDE wait_for so a cancel
        # winning the race can ``inbox_task.cancel()`` it without
        # leaving an orphan get coroutine in the queue.
        inbox_task = asyncio.ensure_future(
            client._inbox.get()  # noqa: SLF001 — single-consumer-per-call
        )
        cancel_task: asyncio.Task[bool] | None = None
        waitset: set[asyncio.Future] = {inbox_task}
        if cancel_event is not None:
            cancel_task = asyncio.ensure_future(cancel_event.wait())
            waitset.add(cancel_task)

        done, _pending = await asyncio.wait(
            waitset,
            timeout=idle_timeout_seconds,
            return_when=asyncio.FIRST_COMPLETED,
        )

        if not done:
            inbox_task.cancel()
            if cancel_task is not None:
                cancel_task.cancel()
            timed_out = True
            break

        if cancel_task is not None and cancel_task in done:
            inbox_task.cancel()
            cancelled = True
            break

        # Cancel future (if any) didn't fire; tear it down before next
        # iteration so we don't leak a pending wait.
        if cancel_task is not None and not cancel_task.done():
            cancel_task.cancel()

        push = inbox_task.result()
        if push.get("event") == "_disconnected":
            break
        if push.get("event") != "transcript_event":
            continue
        transcript = push.get("transcript_event") or {}
        if transcript.get("session_id") not in (session_id, None):
            continue
        if transcript.get("kind") != "assistant_text":
            continue
        text = transcript.get("text")
        if isinstance(text, str):
            assistant_text.append(text)
        if not transcript.get("partial"):
            saw_assistant = True
            break

    return DispatchResult(
        session_id=session_id,
        assistant_text="".join(assistant_text),
        saw_assistant_text=saw_assistant,
        timed_out=timed_out,
        cancelled=cancelled,
        metadata={"session_id": session_id},
    )


__all__ = [
    "DispatchMode",
    "DispatchOrigin",
    "DispatchResult",
    "DispatcherError",
    "dispatch_to_controller",
    "ensure_daemon_running",
    "reattach_to_controller",
    "tail_until_assistant_text",
]

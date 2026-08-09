"""`LaneManager` — six-method `lane.*` contract from
`_shared/lane-contract.md` v1.

`assistant_text` and `tool_result` are DISTINCT event kinds — never
conflate them into one event, and never drop one in favor of the other,
at the wire. Each lane owns one `asyncio.Lock`; cross-lane work runs
concurrently."""

from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Sequence
from typing import Any, Awaitable, Callable, Protocol

from tesseract.config.mcp import load_mcp_config
from tesseract.orchestrator.activity.hooks import register_lane, update_lane_state
from tesseract.orchestrator.seal_guard import SealViolation, assert_cwd_outside_seal

from . import mcp_provision
from .events_log import (
    LaneEventsCursor,
    append_event,
    read_events_since,
)
from .models import (
    Lane,
    LaneEvent,
    LaneKind,
    LaneLifecycle,
    LaneMode,
    LaneSendResult,
    LaneSnapshot,
    LaneStatus,
    TurnOutcome,
)
from .principals import OPERATOR_PRINCIPAL, is_known_principal, may_reach
from .turn_wait import TurnAccumulator, TurnPoll, scope_to_turn
from .store import (
    archive_lane,
    lane_dir,
    list_lane_ids,
    read_lane,
    validate_lane_id,
    write_lane,
)

log = logging.getLogger(__name__)


class LaneManagerError(Exception):
    """Base for lane manager errors so callers can catch with one type."""


class LaneNotFoundError(LaneManagerError):
    """Raised on operations against an unknown `lane_id`."""


class LaneAccessDeniedError(LaneManagerError):
    """Raised when a principal operates on a lane it does not own.

    Distinct from `LaneNotFoundError` because the caller has to be able to
    tell "no such lane" from "not yours" — a client that retries an
    unauthorized close forever is worse than one told to stop. The reach
    itself is already hidden at `list_ids`, so the distinction costs nothing:
    a principal that cannot enumerate a lane has to guess its id before it can
    learn the lane exists."""


class LaneTurnNotFoundError(LaneManagerError):
    """Raised when a waiter names a `turn_id` the lane never issued (or has
    aged out of `_TURN_CURSOR_MEMORY`).

    Fail-closed on purpose. The alternative — falling back to "resolve on
    the first `turn_ended` you see" — is the exact defect this primitive
    exists to remove, and it fails silently by returning another turn's
    reply as yours."""


class LaneAdapter(Protocol):
    """Transport-level driver for one lane.

    `run_turn` emits each adapter event via `on_event` and returns a
    result dict ``{"session_id", "is_error", "usage"}`` when the turn
    completes. Tests inject stubs; production uses
    `ClaudeStreamAdapter` / `CodexStreamAdapter` wrappers."""

    async def run_turn(
        self,
        *,
        message: str,
        on_event: Callable[[dict[str, Any]], None],
        cancel_event: asyncio.Event | None,
    ) -> dict[str, Any]:
        ...


AdapterFactory = Callable[[Lane, "LaneRuntime"], LaneAdapter]
"""Returns the adapter that will drive a lane's turns."""

EventCallback = Callable[[list[LaneEvent]], Awaitable[None]]
"""`await_turn`'s live tap — one call per poll that carried this turn's
events, so a caller can show work in progress from inside the single wait."""


def _mint_lane_id(kind: LaneKind) -> str:
    return f"lane-{kind}-{secrets.token_hex(6)}"


def _mint_turn_id() -> str:
    return secrets.token_hex(4)


# How many recent turns per lane stay addressable by `await_turn`. Past this,
# the oldest submission cursors are forgotten and `await_turn` fails closed on
# their ids — honest, since a caller that has not waited in 256 turns is not
# waiting. Bounds a long-lived lane's runtime memory.
_TURN_CURSOR_MEMORY = 256


def _forget_oldest_turns(runtime: LaneRuntime) -> None:
    """Evict oldest-first, but only turns that have ENDED.

    A turn still queued behind a deep backlog is the oldest AND the one most
    likely to still have a waiter — evicting it would fail that waiter closed
    on a turn the lane is about to run. So the bound is a target, not a
    guarantee: an unfinished turn keeps its cursor however old it is."""
    cursors = runtime.turn_start_cursors
    if len(cursors) <= _TURN_CURSOR_MEMORY:
        return
    # dicts preserve insertion order; the oldest submission comes first.
    for turn_id in list(cursors):
        if len(cursors) <= _TURN_CURSOR_MEMORY:
            return
        if runtime.ended_turn_ids.get(turn_id):
            cursors.pop(turn_id, None)
            runtime.ended_turn_ids.pop(turn_id, None)
            runtime.cancelled_turn_ids.discard(turn_id)


def _rebuild_turn_identity(
    runtime: LaneRuntime, events: list[LaneEvent]
) -> None:
    """Recover `turn_start_cursors` / `ended_turn_ids` from the event log.

    A turn's `turn_started` cursor is the byte offset of that line, so a
    waiter resuming from it re-reads the turn from its own beginning — near
    enough to the submission cursor to be exact for replay, since nothing
    between submission and start belongs to the turn."""
    for event in events:
        turn_id = event.payload.get("turn_id")
        if not isinstance(turn_id, str) or not turn_id:
            continue
        if event.kind == "turn_started":
            runtime.turn_start_cursors.setdefault(turn_id, event.cursor or "0")
        elif event.kind == "turn_ended":
            runtime.turn_start_cursors.setdefault(turn_id, event.cursor or "0")
            runtime.ended_turn_ids[turn_id] = True


@dataclass
class LaneRuntime:
    """Per-lane mutable state the manager owns. Not persisted — recovered
    on `attach` by re-reading `lane.json` + tailing `events.jsonl`."""

    lane: Lane
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    busy: bool = False
    queue_depth: int = 0
    last_activity_utc: str = ""
    current_turn_id: str | None = None
    end_of_turn_at_utc: str | None = None
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    turn_tasks: set[asyncio.Task] = field(default_factory=set)
    # turn_id -> the events.jsonl cursor as of that turn's SUBMISSION, so
    # `await_turn` can replay a turn from before it started running without
    # the caller having captured a tail. Doubles as the known-turn set that
    # makes an unknown id fail closed; bounded by `_TURN_CURSOR_MEMORY`.
    turn_start_cursors: dict[str, str] = field(default_factory=dict)
    # turn_id -> has this turn emitted its `turn_ended` yet. Only ended turns
    # are eligible for cursor eviction — evicting a turn that is still queued
    # would fail its own live waiter closed on a turn about to run.
    ended_turn_ids: dict[str, bool] = field(default_factory=dict)
    # Turns cancelled by id before they reached the lane lock. `_run_queued_turn`
    # ends them under their own id instead of running them.
    cancelled_turn_ids: set[str] = field(default_factory=set)


class LaneManager:
    """Owner of every lane the controller daemon hosts.

    The manager is stateless w.r.t. the brain — every lane's authority
    lives in `lane.json` + `events.jsonl` on disk. The in-memory
    `LaneRuntime` map is a cache that the manager rebuilds on `attach`
    after a daemon restart."""

    def __init__(
        self,
        *,
        adapter_factory: AdapterFactory | None = None,
        root: Path | None = None,
    ) -> None:
        # `root` override is for tests; production resolves at call time
        # from `<TESSERACT_HOME>/controller/lanes/` via store.py helpers.
        self._root_override = root
        self._adapter_factory = adapter_factory or _default_adapter_factory
        self._runtimes: dict[str, LaneRuntime] = {}
        # Headless adapters are stateless; caching them is benign.
        self._adapters: dict[str, LaneAdapter] = {}

    # ------------------------------------------------------------------ paths

    def _lane_dir(self, lane_id: str) -> Path:
        if self._root_override is not None:
            return self._root_override / validate_lane_id(lane_id)
        return lane_dir(lane_id)

    def _events_path(self, lane_id: str) -> Path:
        return self._lane_dir(lane_id) / "events.jsonl"

    def _transcript_path(self, lane_id: str) -> Path:
        return self._lane_dir(lane_id) / "transcript.txt"

    def _last_cursor_path(self, lane_id: str) -> Path:
        return self._lane_dir(lane_id) / "last_cursor.txt"

    def _tail_cursor(self, lane_id: str) -> str:
        """Current end-of-file cursor for a lane, without parsing the log.

        The cursor IS the byte offset (`events_log.LaneEventsCursor`), so
        the file size is the tail — O(1) where a `read(lane_id, None)`
        would re-parse every event ever written to the lane."""
        try:
            return str(self._events_path(lane_id).stat().st_size)
        except OSError:
            return "0"

    # ------------------------------------------------------------------ open

    async def open(
        self,
        *,
        kind: LaneKind,
        mode: LaneMode = "headless",
        model: str,
        working_dir: str,
        env: dict[str, str] | None = None,
        read_only: bool = False,
        owner_principal: str = OPERATOR_PRINCIPAL,
        shared_with: Sequence[str] = (),
    ) -> str:
        """Create a new lane. Returns the lane id; status starts at
        ``spawning`` and flips to ``ready`` once the runtime cache is
        populated. `headless` is the only mode wired.

        `read_only` opens the lane under the CLI's own read-only sandbox —
        it inspects the tree and never modifies it.

        `owner_principal` is the MCP client identity the lane answers to;
        `shared_with` names the identities it was deliberately opened to
        collaborate with. Every later operation authorizes against the pair.

        The working directory is checked against the seal here rather than at
        one of the callers: `open` is the single chokepoint every lane and,
        since the delegation paths collapsed, every delegation passes through.
        A CLI started inside `app/` runs with the sandbox bypassed and edits a
        tree the next update replaces without a diff. The caller named this
        directory deliberately, so it is refused rather than relocated."""
        try:
            assert_cwd_outside_seal(working_dir)
        except SealViolation as exc:
            raise LaneManagerError(str(exc)) from exc
        # A collaborator has to be somebody. Unvalidated, a model could share
        # a lane with a name no client answers to today — and the grant is
        # persisted, so the day that name IS configured it inherits access to
        # a lane opened before it existed. The roster is the same one the
        # daemon attests callers against.
        for principal in shared_with:
            if not is_known_principal(principal):
                raise LaneManagerError(
                    f"cannot share a lane with {principal!r}: no MCP client "
                    f"is configured under that name"
                )
        lane_id = _mint_lane_id(kind)
        lane = Lane(
            lane_id=lane_id,
            kind=kind,
            mode=mode,
            model=model,
            working_dir=working_dir,
            env=dict(env or {}),
            lifecycle="ready",
            read_only=read_only,
            owner_principal=owner_principal,
            shared_with=list(shared_with),
        )
        write_lane(lane)
        runtime = LaneRuntime(lane=lane)
        self._runtimes[lane_id] = runtime
        # Always-on opening event so a cold reader sees the lane existed.
        self._append(
            lane_id,
            LaneEvent(
                lane_id=lane_id,
                kind="status_change",
                payload={"lifecycle": "ready", "kind": kind, "mode": mode},
            ),
        )
        # AS-1 — project the lane into the activity registry. Bare label (the
        # lane id); NamedLaneManager.ensure upserts the human name on top.
        register_lane(
            lane_id,
            label=lane_id,
            provider=kind,
            lifecycle="ready",
            owner_principal=owner_principal,
            shared_with=tuple(shared_with),
        )
        return lane_id

    # ------------------------------------------------------------------ send

    async def send(
        self, lane_id: str, message: str, *, caller: str | None = None
    ) -> LaneSendResult:
        """Fire-and-queue: accept the message, run the turn on a
        background task, return immediately.

        The ack means "queued", never "completed" — completion is the
        lane's `turn_ended` event carrying the SAME `turn_id` this returns
        (every accepted turn is guaranteed one, even when the adapter
        raises). This mirrors the CLI-agent contract: submit → immediate
        accept → stream events → terminal result. `queue_depth` in the
        result includes the turn just queued; it decrements when the turn
        acquires the lane lock.

        The id is minted HERE, not when the queued job acquires the lock:
        a caller that cannot name its turn until the turn starts cannot
        wait for its own turn, and falls back to "first turn_ended wins"."""
        self._authorize(lane_id, caller)
        runtime = self._require_runtime(lane_id)
        if runtime.lane.lifecycle in ("closed", "closing"):
            return LaneSendResult(
                accepted=False,
                queue_depth=runtime.queue_depth,
                reason=f"lane is {runtime.lane.lifecycle}",
            )
        turn_id = _mint_turn_id()
        # Where a waiter for THIS turn starts reading. Captured before the
        # turn task can append anything, so `await_turn` never needs the
        # caller to have captured a tail cursor of its own.
        runtime.turn_start_cursors[turn_id] = self._tail_cursor(lane_id)
        _forget_oldest_turns(runtime)
        runtime.queue_depth += 1
        task = asyncio.create_task(
            self._run_queued_turn(runtime, message, turn_id)
        )
        runtime.turn_tasks.add(task)
        task.add_done_callback(runtime.turn_tasks.discard)
        return LaneSendResult(
            accepted=True, queue_depth=runtime.queue_depth, turn_id=turn_id
        )

    async def await_turn(
        self,
        lane_id: str,
        turn_id: str,
        *,
        timeout: float,
        poll_s: float = 0.5,
        since_cursor: str | None = None,
        on_events: EventCallback | None = None,
        caller: str | None = None,
    ) -> TurnOutcome:
        """Wait for ONE named turn and return its own result.

        The single wait primitive — `send_and_await` and the `lane_turn`
        tool both route through it, so there is one notion of "this turn is
        done" rather than three that each stopped at the first `turn_ended`
        they saw.

        Resolves only on the `turn_ended` stamped with `turn_id`. Reads from
        that turn's SUBMISSION cursor by default, so a turn that finished
        before the caller got round to waiting is still recoverable;
        `since_cursor` resumes a previously-stalled wait without replaying.

        `timeout` bounds LANE silence, not this turn's own — see
        `turn_wait.py`. On stall the outcome carries `completed=False` with
        the partial events and a resumable cursor.

        `on_events` is called with each poll's share of this turn as it
        lands, for callers that show the work in progress rather than only
        its result. It runs inside the wait so the caller does not need a
        second read loop (and, over IPC, a second connection).

        Raises `LaneTurnNotFoundError` for an id this lane never issued."""
        self._authorize(lane_id, caller)
        acc = TurnAccumulator(lane_id=lane_id, turn_id=turn_id)
        cursor = since_cursor
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            poll = self.poll_turn(lane_id, turn_id, cursor, caller=caller)
            cursor = poll.cursor
            if acc.absorb(poll):
                deadline = loop.time() + timeout
            if poll.events and on_events is not None:
                await on_events(poll.events)
            if acc.completed:
                break
            if loop.time() >= deadline:
                break
            await asyncio.sleep(poll_s)
        return acc.outcome()

    def poll_turn(
        self,
        lane_id: str,
        turn_id: str,
        since_cursor: str | None = None,
        *,
        caller: str | None = None,
    ) -> TurnPoll:
        """One turn-scoped read — the step `await_turn` and the daemon's
        `lane_turn_read` handler both advance on, so remote and in-process
        waiters share one notion of "mine" and "done".

        `since_cursor=None` starts at the turn's SUBMISSION cursor, so a
        turn that finished before anyone got round to waiting is still
        recoverable in full.

        Raises `LaneTurnNotFoundError` for an id this lane never issued."""
        self._authorize(lane_id, caller)
        runtime = self._require_runtime(lane_id)
        if turn_id not in runtime.turn_start_cursors:
            raise LaneTurnNotFoundError(
                f"lane {lane_id} never issued turn {turn_id!r}; "
                f"a waiter must name the turn its own send returned"
            )
        start = (
            since_cursor
            if since_cursor is not None
            else runtime.turn_start_cursors[turn_id]
        )
        events, cursor = self.read(lane_id, start)
        return scope_to_turn(turn_id, events, cursor)

    async def send_and_await(
        self,
        lane_id: str,
        message: str,
        *,
        timeout: float,
        poll_s: float = 0.5,
        caller: str | None = None,
    ) -> LaneSendResult:
        """Send, then wait for THAT turn — not for whichever turn ends first.

        Same contract as `IpcLaneManager.send_and_await` so
        `lane_send(wait=True)` blocks identically in-process and over IPC.
        `timeout` bounds SILENCE (stall), not total turn duration — a
        wall-clock cap on total duration abandons healthy long-running
        turns, which is why silence is what this bounds.

        Returns the send result with the wait's verdict attached, so the
        caller can tell a completion from a stall instead of reading an
        acceptance either way."""
        result = await self.send(lane_id, message, caller=caller)
        if not result.accepted or not result.turn_id:
            return result
        outcome = await self.await_turn(
            lane_id, result.turn_id, timeout=timeout, poll_s=poll_s, caller=caller
        )
        return result.model_copy(update={"outcome": outcome})

    async def drain(self, lane_id: str) -> None:
        """Await every queued/in-flight turn task for the lane. Test and
        shutdown helper — production waiters follow the `turn_ended`
        event stream instead."""
        runtime = self._runtimes.get(lane_id)
        if runtime is None:
            return
        tasks = list(runtime.turn_tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _run_queued_turn(
        self, runtime: LaneRuntime, message: str, turn_id: str
    ) -> None:
        lane_id = runtime.lane.lane_id
        decremented = False
        try:
            async with runtime.lock:
                runtime.queue_depth -= 1
                decremented = True
                # A turn accepted and named has a waiter holding its id, so
                # every way it can fail to run still ends it explicitly —
                # leaving the waiter to time out on a turn that will never
                # run is the silence this phase exists to remove.
                if runtime.lane.lifecycle in ("closed", "closing"):
                    log.debug(
                        "lane.send: dropping queued turn for %s lane",
                        runtime.lane.lifecycle,
                    )
                    self._end_turn_early(
                        runtime,
                        turn_id,
                        f"lane {runtime.lane.lifecycle} before the queued turn ran",
                    )
                    return
                if turn_id in runtime.cancelled_turn_ids:
                    # `interrupt` already ended this turn so its waiter did
                    # not have to outlast the sibling ahead of it; just don't
                    # run it.
                    runtime.cancelled_turn_ids.discard(turn_id)
                    return
                await self._run_one_turn(runtime, message, turn_id)
        except asyncio.CancelledError:
            if not decremented:
                runtime.queue_depth -= 1
            raise
        except Exception:  # noqa: BLE001 — logged; events carry the error
            # _run_one_turn already appended the error + turn_ended events;
            # nothing awaits this task, so swallow after logging.
            log.exception("lane.send: turn raised for %s", lane_id)

    def _end_turn_early(
        self, runtime: LaneRuntime, turn_id: str, reason: str
    ) -> None:
        """Emit `turn_ended` for a turn that never got to run.

        Idempotent: a turn cancelled by id is ended immediately AND skipped
        when it later reaches the lock, and a lane closing under it would
        otherwise end it a second time. Two `turn_ended`s for one id is a
        contradiction in the log a waiter would have to reconcile."""
        if runtime.ended_turn_ids.get(turn_id):
            return
        lane_id = runtime.lane.lane_id
        self._append(
            lane_id,
            LaneEvent(
                lane_id=lane_id,
                kind="turn_ended",
                payload={
                    "turn_id": turn_id,
                    "is_error": True,
                    "error": reason,
                    "usage": {},
                },
            ),
        )
        runtime.ended_turn_ids[turn_id] = True

    async def interrupt(
        self, lane_id: str, turn_id: str | None = None, *, caller: str | None = None
    ) -> bool:
        """Cancel a turn WITHOUT closing the lane, so a follow-up send runs
        immediately. Returns True if something was interrupted.

        Two callers, two meanings:

        - ``turn_id=None`` is the operator's steer — "stop whatever this lane
          is doing now" — and cancels the busy turn, whichever it is.
        - ``turn_id=<id>`` cancels THAT turn and nothing else. A handle-scoped
          cancel must never reach a sibling: with A running and B queued,
          cancelling B used to fire the lane's cancel event and kill A, while
          B went on to run unobserved. A queued turn is marked instead, and
          ends under its own id when it reaches the lock.
        """
        self._authorize(lane_id, caller)
        runtime = self._runtimes.get(lane_id)
        if runtime is None:
            return False
        if turn_id is not None and runtime.current_turn_id != turn_id:
            # Not the running turn. If it is still queued, mark it so
            # `_run_queued_turn` ends it instead of running it; if it is
            # already finished or unknown, there is nothing to cancel.
            if turn_id in runtime.turn_start_cursors and not runtime.ended_turn_ids.get(
                turn_id, False
            ):
                # End it NOW rather than when it reaches the lock. A turn
                # queued behind a long-running sibling would otherwise leave
                # its waiter hanging for the sibling's whole duration on a
                # turn already cancelled. The flag stops `_run_queued_turn`
                # emitting a second end when it gets there.
                runtime.cancelled_turn_ids.add(turn_id)
                self._end_turn_early(
                    runtime, turn_id, "cancelled before the turn ran"
                )
                return True
            return False
        if not runtime.busy:
            return False
        # Fire ONLY the current turn's cancel event. Each turn gets its own
        # event (_run_one_turn), so a turn already queued behind the lock is
        # unaffected — no stale-event kill (audit M2 review). The interrupted
        # turn observes this immediately (cli_adapter races readline vs cancel)
        # and releases the lane lock; the follow-up send runs the correction.
        runtime.cancel_event.set()
        return True

    async def _run_one_turn(
        self, runtime: LaneRuntime, message: str, turn_id: str
    ) -> None:
        lane = runtime.lane
        runtime.busy = True
        # M2 — a fresh cancel event per turn so interrupt() aborts only THIS
        # turn; a subsequently-queued turn gets its own and can't be killed by
        # a stale set() from the turn it was queued behind.
        runtime.cancel_event = asyncio.Event()
        update_lane_state(lane.lane_id, "busy")  # AS-1 — running pulse
        runtime.current_turn_id = turn_id
        self._append(
            lane.lane_id,
            LaneEvent(
                lane_id=lane.lane_id,
                kind="turn_started",
                payload={"turn_id": turn_id, "message": message},
            ),
        )
        def _on_adapter_event(raw: dict[str, Any]) -> None:
            for translated in _translate_adapter_event(
                lane.kind, lane.lane_id, raw
            ):
                # Stamped here rather than inside the translator so the
                # translator stays a pure transport→contract mapping. Without
                # this, content events carry no identity and a waiter can
                # attribute prose only by FIFO inference — which breaks on a
                # partial read or one malformed JSONL line.
                translated.payload["turn_id"] = turn_id
                self._append(lane.lane_id, translated)

        try:
            # Building the adapter is INSIDE the completion contract. The
            # production factory raises for a read-only claude lane, which
            # `open` accepts — constructing above the try left that turn
            # accepted, named, and silently orphaned, so its waiter reported
            # a stall instead of the configuration error that caused it.
            adapter = self._adapters.get(lane.lane_id)
            if adapter is None:
                adapter = self._adapter_factory(lane, runtime)
                self._adapters[lane.lane_id] = adapter
            result = await adapter.run_turn(
                message=message,
                on_event=_on_adapter_event,
                cancel_event=runtime.cancel_event,
            )
        except Exception as exc:
            # Completion contract: every turn_started gets a turn_ended,
            # even when the adapter raises. Stream waiters (lane_turn,
            # send_and_await) key off turn_ended and must never hang on
            # a crashed turn.
            self._append(
                lane.lane_id,
                LaneEvent(
                    lane_id=lane.lane_id,
                    kind="error",
                    payload={
                        "turn_id": turn_id,
                        "message": str(exc),
                        "phase": "turn",
                    },
                ),
            )
            end_event = LaneEvent(
                lane_id=lane.lane_id,
                kind="turn_ended",
                payload={
                    "turn_id": turn_id,
                    "is_error": True,
                    "error": str(exc),
                    "usage": {},
                },
            )
            self._append(lane.lane_id, end_event)
            runtime.ended_turn_ids[turn_id] = True
            runtime.end_of_turn_at_utc = end_event.at_utc
            raise
        finally:
            runtime.busy = False
            # Reset here, not after the post-processing below: a raising
            # run_turn (e.g. provisioning failure) otherwise leaves a stale
            # turn id in lane status while busy=False.
            runtime.current_turn_id = None
            update_lane_state(lane.lane_id, "ready")  # AS-1 — back to idle
        new_session_id = result.get("session_id") if isinstance(result, dict) else None
        if isinstance(new_session_id, str) and new_session_id:
            if lane.cli_session_id != new_session_id:
                lane.cli_session_id = new_session_id
                write_lane(lane)
        is_error = bool(result.get("is_error")) if isinstance(result, dict) else False
        usage = result.get("usage") if isinstance(result, dict) else {}
        end_event = LaneEvent(
            lane_id=lane.lane_id,
            kind="turn_ended",
            payload={
                "turn_id": turn_id,
                "is_error": is_error,
                "usage": usage if isinstance(usage, dict) else {},
            },
        )
        self._append(lane.lane_id, end_event)
        runtime.ended_turn_ids[turn_id] = True
        runtime.end_of_turn_at_utc = end_event.at_utc
        runtime.last_activity_utc = end_event.at_utc

    # ------------------------------------------------------------------ read

    def read(
        self,
        lane_id: str,
        since_cursor: str | None = None,
        *,
        caller: str | None = None,
    ) -> tuple[list[LaneEvent], str]:
        """Pull events since `since_cursor`. Returns `(events,
        next_cursor)`. Idempotent: same cursor → same events until a new
        append lands."""
        self._require_lane_exists(lane_id)
        self._authorize(lane_id, caller)
        cursor = LaneEventsCursor.parse(since_cursor)
        events, next_cursor = read_events_since(self._events_path(lane_id), cursor)
        # Advisory: record the last cursor any reader saw. Not authoritative
        # (the byte offset is); useful for human eyeballing of where a
        # client got to. Failure is non-fatal — read MUST succeed even if
        # the disk is read-only or the file is locked.
        try:
            self._last_cursor_path(lane_id).write_text(
                next_cursor.wire, encoding="utf-8"
            )
        except OSError:
            log.debug(
                "lane %s: last_cursor write failed", lane_id, exc_info=True
            )
        return events, next_cursor.wire

    # ------------------------------------------------------------------ status

    def status(self, lane_id: str, *, caller: str | None = None) -> LaneStatus:
        self._authorize(lane_id, caller)
        runtime = self._runtimes.get(lane_id)
        if runtime is None:
            lane = read_lane(lane_id)
            return LaneStatus(
                alive=False,
                busy=False,
                queue_depth=0,
                last_activity_utc=lane.closed_at_utc or lane.opened_at_utc,
                lifecycle=lane.lifecycle,
            )
        return LaneStatus(
            alive=runtime.lane.lifecycle not in ("closed", "error"),
            busy=runtime.busy,
            queue_depth=runtime.queue_depth,
            last_activity_utc=runtime.last_activity_utc
            or runtime.lane.opened_at_utc,
            current_turn_id=runtime.current_turn_id,
            end_of_turn_at_utc=runtime.end_of_turn_at_utc,
            lifecycle=runtime.lane.lifecycle,
        )

    # ------------------------------------------------------------------ attach

    async def attach(
        self, lane_id: str, *, caller: str | None = None
    ) -> LaneSnapshot:
        """Re-establish visibility on a lane. After a brain restart the
        manager-side runtime cache is empty; this rebuilds it from
        `lane.json` and tails `events.jsonl` from offset 0 so the caller
        has the full history. After a daemon restart the underlying
        process is gone — the lane's `lifecycle` already reflects that
        from a prior `close`, or the caller can re-`open` a fresh lane.

        Turn identities are rebuilt from the event log too. Without that, a
        reattached lane knows no turn ids, so `await_turn` fails closed on
        the very turn whose result is sitting in `events.jsonl` — the log is
        the authority, and identity is in it."""
        self._authorize(lane_id, caller)
        lane = read_lane(lane_id)
        runtime = self._runtimes.get(lane_id)
        if runtime is None:
            runtime = LaneRuntime(lane=lane)
            self._runtimes[lane_id] = runtime
        events, next_cursor = read_events_since(
            self._events_path(lane_id), LaneEventsCursor(0)
        )
        _rebuild_turn_identity(runtime, events)
        status = self.status(lane_id)
        return LaneSnapshot(
            lane=lane,
            status=status,
            recent_events=events,
            next_cursor=next_cursor.wire,
        )

    # ------------------------------------------------------------------ close

    async def close(
        self, lane_id: str, reason: str, *, caller: str | None = None
    ) -> dict[str, Any]:
        """Terminate the lane.

        Sets `cancel_event` to interrupt any active turn, calls the
        adapter's `close()` (SIGTERM → 5 s grace → SIGKILL for PTY;
        no-op for headless), then marks the on-disk record closed,
        emits the `closed` event, and archives the directory."""
        self._authorize(lane_id, caller)
        runtime = self._runtimes.get(lane_id)
        lane = runtime.lane if runtime is not None else read_lane(lane_id)
        if runtime is not None:
            runtime.cancel_event.set()
            runtime.lane.lifecycle = "closing"
            # Every accepted turn is guaranteed a `turn_ended`, and closing
            # the lane is not an exception. Cancelling the turn tasks below
            # raises CancelledError inside `_run_queued_turn`, which emits
            # nothing — so without this, a close under a queued turn left its
            # waiter to sit out the full stall timeout on a turn that had
            # already been abandoned. The events land before the archive move
            # so they travel with the rest of the lane's record.
            for turn_id, ended in list(runtime.ended_turn_ids.items()):
                if not ended:
                    runtime.ended_turn_ids.pop(turn_id, None)
            for turn_id in list(runtime.turn_start_cursors):
                if not runtime.ended_turn_ids.get(turn_id):
                    self._end_turn_early(
                        runtime, turn_id, f"lane closed ({reason})"
                    )
            # Settle queued/in-flight turn tasks BEFORE the closed event +
            # archive move — a straggler appending afterwards would
            # recreate the live lane dir next to the archive.
            pending = [t for t in runtime.turn_tasks if not t.done()]
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        adapter = self._adapters.pop(lane_id, None)
        if adapter is not None:
            adapter_close = getattr(adapter, "close", None)
            if callable(adapter_close):
                try:
                    await adapter_close()
                except Exception:  # noqa: BLE001 — best-effort close
                    log.exception(
                        "lane %s: adapter.close() raised", lane_id
                    )
        closed_at = datetime.now(timezone.utc).isoformat()
        lane.lifecycle = "closed"
        lane.closed_at_utc = closed_at
        lane.close_reason = reason
        write_lane(lane)
        self._append(
            lane_id,
            LaneEvent(
                lane_id=lane_id,
                kind="closed",
                payload={"reason": reason, "closed_at_utc": closed_at},
            ),
        )
        update_lane_state(lane_id, "closed")  # AS-1 — terminal transition
        # Archive after the closed event lands so the events.jsonl row
        # describing the close moves with the rest of the lane payload.
        dest = archive_lane(lane_id)
        self._runtimes.pop(lane_id, None)
        return {
            "transcript_path": str(dest / "transcript.txt"),
            "final_status": "closed",
            "archived_at_utc": closed_at,
            "archive_dir": str(dest),
        }

    # ------------------------------------------------------------------ list

    def list_ids(self, *, caller: str | None = None) -> list[str]:
        """All live lane ids (on-disk + cached) the caller may reach. Archived
        lanes are not included; consumers query the archive directly if they
        want historical state.

        The owner check on each verb is what actually withholds a lane; this
        filter is the layer above it, so another principal's ids are not
        handed out to be guessed at or replayed in the first place. A record
        that will not load is omitted — an unreadable owner is not an absent
        one."""
        ids = list_lane_ids()
        if caller is None or caller == OPERATOR_PRINCIPAL:
            return ids
        visible: list[str] = []
        for lane_id in ids:
            try:
                lane = read_lane(lane_id)
            except (FileNotFoundError, OSError, ValueError):
                continue
            if may_reach(
                caller=caller,
                owner=lane.owner_principal,
                shared_with=lane.shared_with,
            ):
                visible.append(lane_id)
        return visible

    # ------------------------------------------------------------------ helpers

    def _authorize(self, lane_id: str, caller: str | None) -> None:
        """Refuse a principal that neither owns the lane nor was named on it.

        The owner comes from the cached runtime when there is one and from
        `lane.json` otherwise — never from the activity registry, which is
        transient and would let an owner evaporate across a restart. A record
        that cannot be read at all is reported as unknown rather than
        authorized: an unreadable owner is not an absent one.
        """
        if caller is None or caller == OPERATOR_PRINCIPAL:
            return
        runtime = self._runtimes.get(lane_id)
        if runtime is not None:
            lane = runtime.lane
        else:
            try:
                lane = read_lane(lane_id)
            except (FileNotFoundError, OSError, ValueError) as exc:
                raise LaneNotFoundError(f"unknown lane {lane_id}") from exc
        if not may_reach(
            caller=caller,
            owner=lane.owner_principal,
            shared_with=lane.shared_with,
        ):
            raise LaneAccessDeniedError(
                f"lane {lane_id} belongs to {lane.owner_principal!r}; "
                f"{caller!r} may not operate on it"
            )

    def _require_runtime(self, lane_id: str) -> LaneRuntime:
        runtime = self._runtimes.get(lane_id)
        if runtime is None:
            raise LaneNotFoundError(
                f"lane {lane_id} is not attached; call attach() first"
            )
        return runtime

    def _require_lane_exists(self, lane_id: str) -> None:
        if lane_id in self._runtimes:
            return
        if not (self._lane_dir(lane_id) / "lane.json").exists():
            raise LaneNotFoundError(f"unknown lane {lane_id}")

    def _append(self, lane_id: str, event: LaneEvent) -> None:
        append_event(self._events_path(lane_id), event)
        runtime = self._runtimes.get(lane_id)
        if runtime is not None:
            runtime.last_activity_utc = event.at_utc
        self._append_transcript(lane_id, event)

    def _append_transcript(self, lane_id: str, event: LaneEvent) -> None:
        """Append a model-side prose line to `transcript.txt` for the
        events a human reader would care about — assistant text, tool
        calls, tool results, errors, close. Non-renderable kinds
        (status_change, turn_started, turn_ended) are skipped.

        Best-effort: failures are swallowed so `_append` (the load-bearing
        events.jsonl path) is never blocked by a transcript write error."""
        line: str | None
        kind = event.kind
        payload = event.payload
        if kind == "assistant_text":
            text = str(payload.get("text") or "").strip()
            line = f"assistant: {text}" if text else None
        elif kind == "tool_use":
            name = payload.get("name") or "<tool>"
            tool_use_id = payload.get("tool_use_id") or ""
            line = f"tool_use[{tool_use_id}] {name}: {payload.get('input')}"
        elif kind == "tool_result":
            tool_use_id = payload.get("tool_use_id") or ""
            output = payload.get("output")
            is_error = " (error)" if payload.get("is_error") else ""
            line = f"tool_result[{tool_use_id}]{is_error}: {output}"
        elif kind == "error":
            msg = payload.get("message") or payload.get("result") or ""
            line = f"error: {msg}"
        elif kind == "closed":
            line = f"closed: {payload.get('reason') or ''}"
        else:
            line = None
        if line is None:
            return
        try:
            with self._transcript_path(lane_id).open("a", encoding="utf-8") as fh:
                fh.write(line.rstrip() + "\n")
        except OSError:
            # Source-of-truth is events.jsonl (already appended); the
            # human-readable transcript is best-effort. Disk-full /
            # locked-file would otherwise be invisible until the
            # operator notices an empty transcript.
            log.debug("lane %s: transcript write failed", lane_id, exc_info=True)


# ---------------------------------------------------------------- translators


def _translate_adapter_event(
    kind: LaneKind, lane_id: str, raw: dict[str, Any]
) -> list[LaneEvent]:
    """Convert one transport-level adapter event into zero, one, or many
    typed LaneEvents.

    A single Claude assistant message can carry BOTH text blocks and
    tool_use blocks; the contract requires emitting them as separate
    LaneEvents — neither conflate them into one event nor drop one in
    favor of the other. Returning a list makes that explicit. Empty
    list = nothing to log."""
    etype = raw.get("type")
    if kind == "claude":
        if etype == "assistant":
            msg = raw.get("message") or {}
            blocks = msg.get("content") or []
            text_chunks: list[str] = []
            tool_uses: list[dict[str, Any]] = []
            if isinstance(blocks, list):
                for block in blocks:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "text":
                        text_chunks.append(str(block.get("text") or ""))
                    elif btype == "tool_use":
                        tool_uses.append({
                            "tool_use_id": block.get("id"),
                            "name": block.get("name"),
                            "input": block.get("input"),
                        })
            out: list[LaneEvent] = []
            text = "".join(text_chunks).strip()
            if text:
                out.append(
                    LaneEvent(
                        lane_id=lane_id,
                        kind="assistant_text",
                        payload={"text": text},
                    )
                )
            for tu in tool_uses:
                out.append(
                    LaneEvent(lane_id=lane_id, kind="tool_use", payload=tu)
                )
            return out
        if etype == "user":
            # Claude emits tool_result wrapped inside a user message; a
            # single user message can carry multiple tool_result blocks
            # when several tools fan out in one turn.
            msg = raw.get("message") or {}
            blocks = msg.get("content") or []
            out = []
            if isinstance(blocks, list):
                for block in blocks:
                    if (
                        isinstance(block, dict)
                        and block.get("type") == "tool_result"
                    ):
                        out.append(
                            LaneEvent(
                                lane_id=lane_id,
                                kind="tool_result",
                                payload={
                                    "tool_use_id": block.get("tool_use_id"),
                                    "output": block.get("content"),
                                    "is_error": bool(block.get("is_error")),
                                },
                            )
                        )
            return out
        if etype == "system" and raw.get("subtype") == "init":
            sid = raw.get("session_id")
            if isinstance(sid, str):
                return [
                    LaneEvent(
                        lane_id=lane_id,
                        kind="status_change",
                        payload={"cli_session_id": sid},
                    )
                ]
        if etype == "result":
            subtype = raw.get("subtype") or ""
            if isinstance(subtype, str) and subtype.startswith("error"):
                return [
                    LaneEvent(
                        lane_id=lane_id,
                        kind="error",
                        payload={"subtype": subtype, "result": raw.get("result")},
                    )
                ]
            return []
    elif kind == "api":
        # One shape, because an API lane has no tools to report. `error` is
        # translated rather than dropped so a failed turn reads the same as a
        # failed CLI turn to everything downstream.
        if etype == "assistant_text":
            text = raw.get("text")
            if isinstance(text, str) and text:
                return [
                    LaneEvent(
                        lane_id=lane_id,
                        kind="assistant_text",
                        payload={"text": text},
                    )
                ]
            return []
        if etype == "error":
            return [
                LaneEvent(
                    lane_id=lane_id,
                    kind="error",
                    payload={"message": raw.get("message")},
                )
            ]
        return []
    elif kind == "codex":
        if etype == "thread.started":
            tid = raw.get("thread_id")
            if isinstance(tid, str):
                return [
                    LaneEvent(
                        lane_id=lane_id,
                        kind="status_change",
                        payload={"cli_session_id": tid},
                    )
                ]
        if etype == "item.completed":
            item = raw.get("item") or {}
            if not isinstance(item, dict):
                return []
            itype = item.get("type")
            if itype == "agent_message":
                text = item.get("text")
                if isinstance(text, str) and text:
                    return [
                        LaneEvent(
                            lane_id=lane_id,
                            kind="assistant_text",
                            payload={"text": text},
                        )
                    ]
            elif itype == "command_execution":
                # Codex fuses tool_use + tool_result in one `item.completed`;
                # split them back into two distinct LaneEvents so the
                # contract's distinct-kind guarantee holds for Codex too.
                tool_use_id = item.get("id") or item.get("call_id") or ""
                return [
                    LaneEvent(
                        lane_id=lane_id,
                        kind="tool_use",
                        payload={
                            "tool_use_id": tool_use_id,
                            "name": "command_execution",
                            "input": {"command": item.get("command")},
                        },
                    ),
                    LaneEvent(
                        lane_id=lane_id,
                        kind="tool_result",
                        payload={
                            "tool_use_id": tool_use_id,
                            "output": item.get("result"),
                            "is_error": bool(item.get("is_error")),
                        },
                    ),
                ]
        if etype in ("error", "turn.failed"):
            return [
                LaneEvent(
                    lane_id=lane_id,
                    kind="error",
                    payload={"message": raw.get("message") or raw.get("error")},
                )
            ]
    return []


# --------------------------------------------------- default adapter factory


def _default_adapter_factory(lane: Lane, runtime: LaneRuntime) -> LaneAdapter:
    """Build the production adapter for `lane`.

    X-4 Session D introduced a `pty` transport alongside `headless`;
    the P4 PTY prune retired it — `headless` (subprocess +
    stream-JSON) is the only wired mode."""
    if lane.mode == "headless":
        from tesseract.orchestrator.agent_controller.interactive.cli_adapter import (
            ClaudeStreamAdapter,
            CodexStreamAdapter,
        )

        if lane.kind == "claude":
            if lane.read_only:
                # The claude CLI has no read-only counterpart to codex's
                # sandbox flag. Refusing beats spawning a lane that reports
                # itself read-only and can still write.
                raise LaneManagerError(
                    "read_only lanes are not supported for kind 'claude'; "
                    "the CLI has no read-only sandbox flag"
                )
            base = ClaudeStreamAdapter(model=lane.model)
        elif lane.kind == "codex":
            base = CodexStreamAdapter(model=lane.model, read_only=lane.read_only)
        elif lane.kind == "api":
            # An API lane is driven directly, not wrapped in the headless CLI
            # adapter: there is no subprocess, no `--resume` session id and no
            # MCP hub to provision, so the wrapper would have nothing to do.
            from tesseract.config.loader import load_config
            from tesseract.orchestrator.agent_controller.interactive.api_adapter import (
                ApiLaneAdapter,
            )

            try:
                ref = load_config().resolve(lane.model)
            except Exception as exc:  # noqa: BLE001 — config is authoritative
                raise LaneManagerError(
                    f"api lane {lane.lane_id!r} names {lane.model!r}, which does "
                    f"not resolve: {exc}"
                ) from exc
            return ApiLaneAdapter(ref=ref)
        else:  # pragma: no cover — Literal narrows; defensive only
            raise LaneManagerError(f"unknown lane kind {lane.kind!r}")
        return _HeadlessCliLaneAdapter(base=base, lane=lane)
    raise LaneManagerError(f"unknown lane mode {lane.mode!r}")


@dataclass
class _HeadlessCliLaneAdapter:
    """Adapts `ClaudeStreamAdapter` / `CodexStreamAdapter` to the
    `LaneAdapter` Protocol. Threads `lane.cli_session_id` so post-X-3
    multi-turn semantics route to `--resume <id>` on every send."""

    base: Any  # ClaudeStreamAdapter | CodexStreamAdapter
    lane: Lane
    _mcp_provisioned: bool = field(default=False, init=False, repr=False)
    _mcp_warned: bool = field(default=False, init=False, repr=False)

    async def run_turn(
        self,
        *,
        message: str,
        on_event: Callable[[dict[str, Any]], None],
        cancel_event: asyncio.Event | None,
    ) -> dict[str, Any]:
        if not self._mcp_provisioned:
            # Wire the CLI to the embedded MCP hub before its first turn.
            # Config-as-authority: a missing token env var raises here, not on
            # the CLI's own connect attempt.
            #
            # Latch only on success. `provision` returns False when it backed
            # off rather than overwrite a config another process was writing —
            # latching that would leave this lane with no hub for the rest of
            # its life, silently, because nothing else retries per-lane.
            self._mcp_provisioned = await asyncio.to_thread(
                lambda: mcp_provision.provision(
                    self.lane.kind,
                    load_mcp_config(),
                    # Where the project-scope scheme used to write, so where a
                    # stale entry can still shadow the user-scope one.
                    cleanup_dirs=[Path(self.lane.working_dir)],
                )
            )
            if not self._mcp_provisioned:
                # Warn once, then drop to debug. Retrying every turn is correct,
                # but a permanently broken config would otherwise emit one
                # warning per turn forever and bury the signal it exists to
                # carry.
                if self._mcp_warned:
                    log.debug(
                        "lane %s: MCP provisioning still incomplete", self.lane.lane_id
                    )
                else:
                    self._mcp_warned = True
                    log.warning(
                        "lane %s: MCP provisioning did not complete; retrying each turn",
                        self.lane.lane_id,
                    )
        accumulator = await self.base.run_turn(
            task=message,
            session_id=self.lane.cli_session_id,
            cwd=self.lane.working_dir,
            on_event=on_event,
            cancel_event=cancel_event,
        )
        return {
            "session_id": getattr(accumulator, "session_id", None),
            "is_error": getattr(accumulator, "is_error", False),
            "usage": getattr(accumulator, "usage", {}),
            "result_text": getattr(accumulator, "result_text", ""),
        }


__all__ = [
    "AdapterFactory",
    "LaneAdapter",
    "LaneManager",
    "LaneManagerError",
    "LaneNotFoundError",
    "LaneRuntime",
]

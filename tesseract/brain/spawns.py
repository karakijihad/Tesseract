"""Background-spawn registry for delegate_* / invoke_agent tools.

Started as Phase 4 of the TARS reboot CLI-parity plan (2026-05-10). A
delegated/agent task runs as an `asyncio.Task`; the operator/TARS can
`spawn_check` for status, `spawn_await` to block on the result later, or
`spawn_cancel` to terminate.

Background (fire-and-track) is now the DEFAULT for the delegation verbs
(`delegate_claude`, `delegate_codex`, `delegate_tars_controller`,
`invoke_agent`, `lane_turn`); pass `background=false` to await inline.

State lives in `SpawnRegistry`, attached to `ChatSession.spawns`. The
registry is per-session and ephemeral — a session reset wipes it,
running spawns are cancelled. Handles use a stable id format so the
operator can correlate Mirror DelegateCards with the chat-side handle
returned from the tool call. The spawn_* control tools are tool-name-
agnostic, so new spawn kinds pick them up without modification.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import weakref
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine, Optional

from tesseract.kernel.tools.base import (
    SpawnCapExceeded,
    SpawnDepthExceeded,
    ToolResult,
)

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _delegate_provider(kind: str) -> str | None:
    """Map a spawn ``kind`` (``delegate_claude`` / ``delegate_codex`` /
    ``agent:<name>``) to the activity-registry ``provider`` field."""
    if "claude" in kind:
        return "claude"
    if "codex" in kind:
        return "codex"
    return None


def _bounded_one_line(text: str | None, *, limit: int = 200) -> str | None:
    """First line of ``text``, bounded to ``limit`` chars. Keeps the Activity
    registry + its WS snapshots light — a delegate task/goal can be multi-KB."""
    if not text:
        return None
    first = text.strip().splitlines()[0] if text.strip() else ""
    return first[:limit] or None


def _spawn_result_summary(handle: "SpawnHandle") -> str | None:
    """One-line outcome summary for a finished spawn's ActivityRecord ``result``.

    ``None`` while still running or when cancelled (no meaningful product).
    A raised task surfaces its exception type; otherwise the first line of the
    ``ToolResult`` output, bounded so the registry stays light."""
    if not handle.task.done() or handle.cancelled or handle.task.cancelled():
        return None
    try:
        res = handle.task.result()
    except Exception as exc:  # noqa: BLE001 — task raised
        return f"failed: {type(exc).__name__}"
    return _bounded_one_line(getattr(res, "output", "")) or "(no output)"


def _spawn_record(
    handle: "SpawnHandle", *, state: str, result: str | None = None
) -> "ActivityRecord":
    from tesseract.orchestrator.activity import ActivityRecord

    # Task 6.1 — the Mirror renders `label` directly (ActivityMap /
    # RunningSpawnsChip never read `goal`), so a bare `handle.kind` left the
    # operator seeing "delegate_claude" instead of what the spawn is doing.
    # Fall back to `handle.kind` only when no goal text was supplied.
    goal_snippet = _bounded_one_line(handle.goal)
    return ActivityRecord(
        activity_id=f"delegate:{handle.handle_id}",
        kind="delegate",
        label=goal_snippet or handle.kind,
        state=state,  # type: ignore[arg-type]
        durability="ephemeral",
        provider=_delegate_provider(handle.kind),
        goal=goal_snippet,
        result=result,
        started_at=handle.started_at,
    )


def _activity_register_spawn(handle: "SpawnHandle") -> None:
    """AS-1 — project a background spawn into the Unified Activity Registry
    so the Mirror reflects it as live work. Delegates are ``ephemeral`` —
    they die with the process and are never rebuilt from disk.

    Best-effort: a registry/publish failure must NEVER propagate into spawn
    dispatch (the chat loop must not break because the reflection layer
    hiccuped). Runs on the event loop (``register`` is called from the chat
    turn), so the loop-thread-only bus publish is safe without marshaling.
    """
    try:
        from tesseract.orchestrator.activity import get_activity_registry

        get_activity_registry().register(_spawn_record(handle, state="running"))
    except Exception:  # noqa: BLE001 — reflection is best-effort
        logger.warning(
            "activity: register spawn %s failed", handle.handle_id, exc_info=True
        )


def _activity_update_spawn(handle: "SpawnHandle") -> None:
    """Transition a spawn's activity record to its terminal state
    (``done`` / ``failed`` / ``cancelled``), carrying the outcome ``result``.

    Upserts via ``register`` (not ``update_state``) so the same call can set
    ``result`` alongside the new state; ``register`` preserves ``started_at``
    and publishes ``activity_updated`` for the existing id, same as a bare
    state transition. If the record was already swept (can't happen for a
    still-``running`` delegate — the sweep only evicts terminal records), this
    re-adds it as ``activity_registered`` rather than no-op'ing."""
    try:
        from tesseract.orchestrator.activity import get_activity_registry

        get_activity_registry().register(
            _spawn_record(
                handle,
                state=handle.status(),
                result=_spawn_result_summary(handle),
            )
        )
    except Exception:  # noqa: BLE001 — reflection is best-effort
        logger.warning(
            "activity: update spawn %s failed", handle.handle_id, exc_info=True
        )


@dataclass
class SpawnHandle:
    """Per-spawn record. The Task isolates the work; the result is
    surfaced via `await_result()` once it completes (or cancelled).
    """
    handle_id: str
    kind: str            # 'delegate_claude' | 'delegate_codex' | 'agent:<name>' | future spawn kinds
    started_at: str
    task: asyncio.Task[ToolResult] = field(repr=False)
    cancel_fn: Optional[Callable[[], None]] = field(default=None, repr=False)
    finished_at: Optional[str] = None
    cancelled: bool = False
    # The intent the spawn was launched with (the delegate/agent task text),
    # surfaced as the ActivityRecord ``goal`` so the Mirror shows WHAT each
    # background unit is doing, not just its kind.
    goal: Optional[str] = None
    # trio W4 — True while the spawn is PARKED on an operator question
    # (ask-instead-of-die): its ASK timed out unattended and now waits in
    # the Mirror approvals pane. Set/cleared by the Mirror ask_fn via
    # ``mark_input_required``; projects as ActivityState "input_required".
    input_required: bool = False

    def is_running(self) -> bool:
        return not self.task.done()

    def status(self) -> str:
        if not self.task.done():
            return "input_required" if self.input_required else "running"
        if self.cancelled or self.task.cancelled():
            return "cancelled"
        if self.task.exception() is not None:
            return "failed"
        return "done"


# trio W4 — process-global handle index, registry-independent. The Mirror
# ask_fn resolves a parked ask's SpawnHandle from its task name alone; a
# sub-agent's ASK fires with the CHILD session's registry on its context,
# so a per-registry lookup would miss the minting registry's handle.
# WeakValue: entries vanish with their handles, no lifecycle bookkeeping.
_ALL_HANDLES: "weakref.WeakValueDictionary[str, SpawnHandle]" = (
    weakref.WeakValueDictionary()
)


def find_handle(handle_id: str) -> Optional[SpawnHandle]:
    """Resolve a spawn handle by id across ALL registries in this process."""
    return _ALL_HANDLES.get(handle_id)


async def cancel_handle(h: SpawnHandle) -> bool:
    """Cancel a handle directly — registry-independent, so the spawn_cancel
    tool can act on handles resolved via `find_handle` (a reconnected chat's
    own registry is empty; the surviving spawn lives in the orphaned one).
    Returns True if the handle was running, False if already done."""
    if h.task.done():
        return False
    h.cancelled = True
    if h.cancel_fn is not None:
        try:
            h.cancel_fn()
        except Exception:
            logger.exception("spawn_cancel cancel_fn raised for %s", h.handle_id)
    h.task.cancel()
    try:
        await h.task
    except (asyncio.CancelledError, Exception):
        pass
    return True


def mark_input_required(handle: SpawnHandle, flag: bool) -> None:
    """Flip a spawn's parked-on-operator state and push the activity update
    so the Mirror reflects `input_required` ⇄ `running` live (trio W4)."""
    handle.input_required = flag
    _activity_update_spawn(handle)


class SpawnReservation:
    """A claimed-but-not-yet-registered spawn slot (trio W3 / audit M5).

    Callers that must launch work *before* they have the coroutine to register
    (e.g. ``start_controller_session`` awaits the dispatcher to learn the
    session id) ``reserve()`` a slot first so the admission check happens
    before the launch, then either ``register(reservation=...)`` to convert it
    into a live handle or ``release()`` it if the launch fails.
    """

    def __init__(self, registry: "SpawnRegistry") -> None:
        self._registry = registry
        self.active = True

    def release(self) -> None:
        if self.active:
            self.active = False
            self._registry._reservations -= 1


class SpawnRegistry:
    """In-memory per-session registry of background spawns.

    Single-threaded asyncio loop assumption — no locking. The owning
    `ChatSession` constructs one and exposes it via `self.spawns`.
    """

    def __init__(self) -> None:
        self._handles: dict[str, SpawnHandle] = {}
        # Per-handle: True once the chat loop's tool-boundary drain has
        # surfaced this completion as a SPAWN_DONE chunk. Prevents
        # re-emitting the same notice every iteration.
        self._harvested: set[str] = set()
        # Stage 2B halt-watchdog: handle_ids already flagged stalled, so a
        # repeated sweep doesn't re-enqueue the same `[spawn_stalled]` note.
        self._stalled: set[str] = set()
        # Push-on-completion hook, set by the owning ChatSession
        # (`ingest_spawn_completion`). Fired exactly once per spawn from the
        # task done-callback so a finished background spawn reaches TARS's
        # context at its next turn — SPAWN_DONE only ever reached the UI, so
        # the LLM never saw completions and "forgot" them (2026-06-30).
        self.completion_notifier: Optional[Callable[["SpawnHandle"], None]] = None
        # M4-p2 — spawn-start hook, mirroring `completion_notifier`'s shape.
        # `spawn_wake.wire_chat` sets this so the app-level
        # `SpawnOwnershipIndex` learns which chat_id owns a handle the
        # moment it registers — the reconnect rebind needs that mapping to
        # find still-running handles by chat_id, not just completed ones.
        # `None` (default) disables it (REPL / sub-agent / bare registries).
        self.on_register: Optional[Callable[["SpawnHandle"], None]] = None
        # P6 Task 3 §G5 — identity for the spawn journal
        # (`logs/sessions/<session_id>/spawns.jsonl`). `None` (default)
        # disables journaling entirely (REPL / sub-agent / synthetic
        # sessions never carry a Mirror session_id). Set additively by
        # `ChatSession.__post_init__` from `tool_context.session_id`.
        self.session_id: Optional[str] = None
        # parallel-tars P4 — OpenClaw-style numeric backstop against runaway
        # fan-out. `None` (default) = uncapped (REPL / bare test registries);
        # Mirror sessions set it from
        # `runtime.yaml::max_concurrent_spawns_per_session` via ChatSession.
        self.max_concurrent: Optional[int] = None
        # trio W3 — spawn NESTING backstop (OpenClaw maxSpawnDepth analog).
        # `depth` is the owning session's nesting level (root = 0; sub-agent
        # sessions +1 per level, stamped by ChatSession.__post_init__ from
        # ToolContext.spawn_depth). Registering a spawn from a session at or
        # past `max_depth` raises SpawnDepthExceeded. `None` = uncapped.
        self.depth: int = 0
        self.max_depth: Optional[int] = None
        # trio W3 / M5 — slots claimed by reserve() but not yet registered.
        # Counted toward the concurrency total so an in-flight admission can't
        # be double-spent between the reserve() check and register().
        self._reservations: int = 0

    def _running_total(self) -> int:
        return (
            sum(1 for h in self._handles.values() if h.is_running())
            + self._reservations
        )

    def _check_admission(self) -> None:
        if self.max_depth is not None and self.depth >= self.max_depth:
            raise SpawnDepthExceeded(self.depth, self.max_depth)
        if self.max_concurrent is not None:
            running = self._running_total()
            if running >= self.max_concurrent:
                raise SpawnCapExceeded(running, self.max_concurrent)

    def reserve(self) -> SpawnReservation:
        """Admit a spawn slot before its coroutine exists (M5). Raises
        SpawnDepthExceeded / SpawnCapExceeded if the session is at a cap, so a
        rejected launch never starts untracked work."""
        self._check_admission()
        self._reservations += 1
        return SpawnReservation(self)

    def register(
        self,
        *,
        kind: str,
        coro: Coroutine[Any, Any, ToolResult],
        cancel_fn: Optional[Callable[[], None]] = None,
        goal: Optional[str] = None,
        reservation: Optional[SpawnReservation] = None,
    ) -> SpawnHandle:
        if reservation is not None and reservation.active:
            # Slot already admitted by reserve(); consume it (the new handle
            # now counts as running) without re-checking the cap.
            reservation.release()
        else:
            try:
                self._check_admission()
            except SpawnCapExceeded:
                coro.close()  # never-awaited coroutine would warn at GC
                raise
        handle_id = self._mint_id(kind)
        # The `spawn:<handle_id>` task name is a LOAD-BEARING contract (trio
        # W4): the Mirror ask_fn detects background-spawn origin from
        # `asyncio.current_task().get_name()` to park unattended ASKs
        # instead of denying them. Rename only with that call site.
        task = asyncio.create_task(coro, name=f"spawn:{handle_id}")
        started_at = _now_iso()
        handle = SpawnHandle(
            handle_id=handle_id,
            kind=kind,
            started_at=started_at,
            task=task,
            cancel_fn=cancel_fn,
            goal=goal,
        )
        self._handles[handle_id] = handle
        _ALL_HANDLES[handle_id] = handle

        def _on_done(t: asyncio.Task[ToolResult], h: SpawnHandle = handle) -> None:
            h.finished_at = _now_iso()
            _activity_update_spawn(h)
            if self.session_id:
                try:
                    from tesseract.brain import spawn_journal

                    spawn_journal.record_terminal(self.session_id, h.handle_id, h.status())
                except Exception:  # noqa: BLE001 — journal is best-effort
                    logger.warning(
                        "spawn journal terminal-write failed for %s", h.handle_id, exc_info=True
                    )
            # Skip operator-cancelled spawns: a cancel (via `/reset`'s
            # cancel_all or an explicit spawn_cancel) is not a completion TARS
            # needs to be nudged about. This also closes the reset race — a
            # deferred cancel's done-callback can't re-populate the deque that
            # `reset()` just cleared (reviewer finding, 2026-06-30).
            if (
                self.completion_notifier is not None
                and not h.cancelled
                and not t.cancelled()
            ):
                try:
                    self.completion_notifier(h)
                except Exception:
                    logger.exception(
                        "spawn completion_notifier raised for %s", h.handle_id
                    )

        task.add_done_callback(_on_done)
        _activity_register_spawn(handle)
        if self.on_register is not None:
            try:
                self.on_register(handle)
            except Exception:  # noqa: BLE001 — ownership bookkeeping is best-effort
                logger.warning(
                    "spawn on_register hook failed for %s", handle_id, exc_info=True
                )
        if self.session_id:
            try:
                from tesseract.brain import spawn_journal

                spawn_journal.record_start(self.session_id, handle_id, kind, started_at)
            except Exception:  # noqa: BLE001 — journal is best-effort
                logger.warning(
                    "spawn journal start-write failed for %s", handle_id, exc_info=True
                )
        return handle

    def get(self, handle_id: str) -> SpawnHandle | None:
        return self._handles.get(handle_id)

    def list_handles(self) -> list[SpawnHandle]:
        return list(self._handles.values())

    def drain_completed(self) -> list[SpawnHandle]:
        """Return all completed handles that haven't been surfaced yet.

        Called by the chat loop at each tool boundary. Marks each
        returned handle as harvested so the next call doesn't re-emit.
        """
        out: list[SpawnHandle] = []
        for hid, h in self._handles.items():
            if hid in self._harvested:
                continue
            if h.task.done():
                self._harvested.add(hid)
                out.append(h)
        return out

    def sweep_stalled(
        self, max_age_seconds: float, *, now: Optional[datetime] = None
    ) -> list[SpawnHandle]:
        """Return running handles older than ``max_age_seconds``, newly flagged.

        Stage 2B halt-watchdog. A hung *subprocess* is already killed by
        ``cli_stream.race_communicate``'s own timeout (its task then completes
        normally) — this catches the rarer case of a spawn task that is neither
        done nor killed, sitting ``running`` far past any sane bound. Each
        stalled handle is returned exactly once (``_stalled`` dedup) so the
        floor note isn't re-enqueued on every sweep.
        """
        ref = now or datetime.now(timezone.utc)
        out: list[SpawnHandle] = []
        for hid, h in self._handles.items():
            if hid in self._stalled or not h.is_running():
                continue
            if h.input_required:
                # trio W4 — parked on an operator question, not wedged. A
                # stall nudge here would invite spawn_cancel on work that is
                # deliberately waiting in the approvals pane.
                continue
            try:
                started = datetime.fromisoformat(h.started_at)
            except ValueError:
                continue
            if (ref - started).total_seconds() >= max_age_seconds:
                self._stalled.add(hid)
                out.append(h)
        return out

    async def cancel(self, handle_id: str) -> bool:
        """Mark cancelled, fire cancel_fn (subprocess SIGTERM etc.),
        cancel the Task. Returns True if the handle existed and was
        running, False otherwise."""
        h = self._handles.get(handle_id)
        if h is None:
            return False
        return await cancel_handle(h)

    async def cancel_all(self) -> int:
        """Used by `ChatSession.reset()` to make `/reset` clean up
        running background work. Returns count cancelled."""
        ids = list(self._handles)
        n = 0
        for hid in ids:
            if await self.cancel(hid):
                n += 1
        return n

    def _mint_id(self, kind: str) -> str:
        # `<kind-slug>-<YYYYMMDD-HHMMSS>-<rand>` — operator-readable +
        # collision-free. Kind slug strips any prefix for tool grouping
        # in the UI.
        slug = kind.replace("delegate_", "del-").replace("invoke_agent:", "agent-")
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        suffix = secrets.token_hex(3)
        return f"{slug}-{ts}-{suffix}"

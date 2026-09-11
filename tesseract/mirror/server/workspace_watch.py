"""Watch the workspace record, not the writers.

Twenty-odd places write a `WorkspaceEvent`, and about half of them never
called `broadcast_workspace_event` — so the operator's open inbox showed a row
only after they thought to press Refresh. Asking each writer to remember is
the wrong mechanism for the same reason the watchman does not ask each
subsystem to report itself: the ones that forget are exactly the ones you
never hear from. This watches the file instead, so a new writer cannot forget
and a writer in ANOTHER PROCESS — the agent controller, a REPL, a scheduler
run outside the backend — is covered for the first time. The old in-process
call could not reach any of them: it needed an `app` handle to walk.

**A decision is an event too.** Status is not appended, it is a rewrite of
`events.jsonl`, so a byte-offset tail would see the file change under it and
either miss the change or replay the file. This diffs the CURRENT state of
every row against the last one it broadcast, which makes an append and a
decision the same observation — and a decision taken on Telegram or in a
second window stops leaving the first window showing a row that is settled.
The frontend needs nothing new for it: `workspace_event_appended` lands in
`upsertEvent`, which is an upsert.

The file is watched, never polled — `watchdog` is already how the config tree
is watched, and a poll would need an interval nobody chose. The whole
in-memory state is one dict of `event_id -> serialised row`; the store is
curated and holds tens of rows, which is why re-reading it beats an index that
has to stay coherent with a rewrite.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from pathlib import Path
from typing import Any

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

log = logging.getLogger(__name__)

# Long enough that a rewrite's tmp-file dance settles into one read, short
# enough to be invisible to someone watching the pane. The config watcher
# debounces the same way and for the same reason.
DEBOUNCE_SECONDS = 0.25

_WATCHED_NAME = "events.jsonl"


class WorkspaceWatcher:
    """Broadcasts what appears in — or changes in — the workspace event log.

    Lifecycle mirrors `ConfigWatcher`: `await start()` seeds the snapshot and
    starts the observer thread, `await stop()` cancels pending timers and
    joins it.
    """

    def __init__(self, app: Any, store: Any) -> None:
        self._app = app
        self._store = store
        self._observer: Observer | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._timer: threading.Timer | None = None
        self._timer_lock = threading.Lock()
        # event_id -> the row as last broadcast. Seeded at start, so a backend
        # restart does not re-announce everything the inbox already holds.
        self._seen: dict[str, str] = {}

    async def start(self) -> None:
        if self._observer is not None:
            return
        self._loop = asyncio.get_running_loop()
        self._seen = await asyncio.to_thread(self._snapshot)
        directory = Path(self._store.events_path).parent
        directory.mkdir(parents=True, exist_ok=True)
        self._observer = Observer()
        self._observer.schedule(_Handler(self), str(directory), recursive=False)
        self._observer.start()
        log.info("workspace_watch: observing %s (%d rows known)", directory, len(self._seen))

    async def stop(self) -> None:
        if self._observer is None:
            return
        with self._timer_lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
        try:
            self._observer.stop()
            self._observer.join(timeout=2.0)
        except Exception:
            log.exception("workspace_watch: observer stop failed")
        self._observer = None
        log.info("workspace_watch: stopped")

    # ── observer thread ──────────────────────────────────────────

    def _on_change(self) -> None:
        with self._timer_lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(DEBOUNCE_SECONDS, self._fire)
            self._timer.daemon = True
            self._timer.start()

    def _fire(self) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            asyncio.run_coroutine_threadsafe(self.sync(), loop)
        except RuntimeError:
            # Loop is shutting down — drop quietly.
            return

    # ── the loop side ────────────────────────────────────────────

    async def sync(self) -> int:
        """Broadcast every row that is new or has changed. Returns the count.

        Public because it is also the honest way to test this: the observer
        thread is the transport, and what it triggers is this.
        """
        from tesseract.workspace_events.broadcast import broadcast_workspace_event

        try:
            rows = await asyncio.to_thread(self._read_rows)
        except Exception:
            log.exception("workspace_watch: read failed")
            return 0
        changed = [
            event
            for event_id, serialised, event in rows
            if self._seen.get(event_id) != serialised
        ]
        for event_id, serialised, _event in rows:
            self._seen[event_id] = serialised
        for event in changed:
            try:
                await broadcast_workspace_event(self._app, event)
            except Exception:
                log.exception(
                    "workspace_watch: broadcast failed for %s", event.event_id
                )
            settled = await self._settle_if_the_mode_says_so(event)
            await self._tell_them_it_applied(settled or event)
        return len(changed)

    async def _settle_if_the_mode_says_so(self, event: Any) -> Any | None:
        """Answer a card the operator's policy says needs no answering.

        **This is the one place that decides, and that is the whole design.**
        A dozen producers file cards and more will exist; asking each to read
        a posture and settle itself is how three of them end up with three
        answers, which is the fork this replaces. They file; this decides.

        `permissions.yaml::workspace_cards` is the statement, keyed by kind,
        the same shape `workspace_documents` uses for the files. `auto` means
        the runtime approves it HERE, through `apply_decision` — the same
        function the operator's own Approve button calls, with the same
        side effects and the same ledger row, marked `mode` rather than
        `operator` because the ledger is the record of who allowed what.

        Cards about a document never reach this: they are settled at propose
        time by `settle_proposal`, because the tool that raised one has to
        tell the model what happened before its turn ends. Nothing waits on
        these, so watching is the cheaper place.

        A card filed while the backend was down is NOT settled on the next
        boot: `start()` seeds the snapshot, so only cards that arrive or
        change while this is running are seen. Approving a backlog in bulk
        because a process restarted is not a decision anyone made.
        """
        if getattr(event, "status", "") != "pending":
            return None
        kind = getattr(event, "kind", "")
        try:
            from tesseract.workspace_events.events import DECIDABLE_KINDS

            if kind not in DECIDABLE_KINDS:
                return None
            policy = self._app["config"].permissions
            if policy.workspace_card_posture(kind) != "auto":
                return None
        except Exception:
            # No policy wired means no operator is reachable either, and the
            # half that waits for one is the safe half.
            return None

        from tesseract.kernel.workspace_changes import SETTLED_BY_THE_MODE
        from tesseract.mirror.server.routes.workspace import DecisionError, apply_decision

        try:
            updated, _comments = await apply_decision(
                self._app,
                event.event_id,
                "approve",
                reason=SETTLED_BY_THE_MODE,
                actor="mode",
            )
        except DecisionError as exc:
            log.warning(
                "workspace_watch: %s could not be settled: %s",
                event.event_id, exc.payload,
            )
            return None
        except Exception:
            log.exception("workspace_watch: settling %s raised", event.event_id)
            return None
        log.info("workspace_watch: %s (%s) applied without asking", event.event_id, kind)
        return updated

    async def _tell_them_it_applied(self, event: Any) -> None:
        """A document that changed itself says so, wherever the operator is.

        Their framing, and it is the whole design: *"i do not want anymore
        asks aproval, let all be auto. in the end it can send me the
        notification and i read."* An approval is a message that stops the
        work; this is the same message after it, and the work did not stop.

        Watched here rather than emitted by whoever applied the change, for
        the reason this whole file exists: the producers that would forget are
        the ones nobody hears from. A change applied by the nightly
        consolidator, by a chat turn, or by a process that is not this one all
        arrive as the same row in the same file, so all three notify.

        The broadcast row is seeded at start, so a restart re-announces
        nothing, and `sync` only reaches here for a row that actually changed.
        """
        # Two things, and both: it HAPPENED, and the MODE is what did it. The
        # status alone would announce a card the operator just approved, which
        # is telling them what they did; the marker alone would announce a
        # decision that has not landed yet.
        from tesseract.kernel.workspace_changes import SETTLED_BY_THE_MODE
        from tesseract.workspace_events.events import SETTLED

        if getattr(event, "status", "") not in SETTLED:
            return
        if getattr(event, "decided_reason", "") != SETTLED_BY_THE_MODE:
            return
        payload = getattr(event, "payload", None) or {}
        try:
            from tesseract.mirror.server.app import _get_outbound_notifier

            notifier = _get_outbound_notifier(self._app)
            if notifier is None:
                return
            await notifier.notify("workspace_change_applied", {
                # A document card names the file; a card that names none is
                # described by its kind, which is what the operator would
                # have read on the card itself.
                "document": (
                    payload.get("label")
                    or payload.get("target_path")
                    or str(getattr(event, "kind", "") or "").replace("_", " ")
                    or "something"
                ),
                "summary": event.summary,
                "event_id": event.event_id,
            })
        except Exception:
            log.exception(
                "workspace_watch: could not say that %s applied", event.event_id
            )

    # ── reads ────────────────────────────────────────────────────

    def _read_rows(self) -> list[tuple[str, str, Any]]:
        events = self._store.list_events(limit=10_000)
        return [
            (ev.event_id, json.dumps(ev.to_dict(), sort_keys=True), ev)
            for ev in events
        ]

    def _snapshot(self) -> dict[str, str]:
        try:
            return {
                event_id: serialised for event_id, serialised, _ in self._read_rows()
            }
        except Exception:
            log.exception("workspace_watch: initial snapshot failed")
            return {}


class _Handler(FileSystemEventHandler):
    def __init__(self, watcher: WorkspaceWatcher) -> None:
        self._watcher = watcher

    def on_modified(self, event: Any) -> None:
        self._dispatch(getattr(event, "src_path", ""))

    def on_created(self, event: Any) -> None:
        self._dispatch(getattr(event, "src_path", ""))

    def on_moved(self, event: Any) -> None:
        # A status change rewrites through `events.jsonl.tmp` and renames, so
        # the destination is the event that matters.
        self._dispatch(getattr(event, "dest_path", ""))

    def _dispatch(self, raw_path: str | bytes) -> None:
        path = raw_path.decode("utf-8") if isinstance(raw_path, bytes) else raw_path
        if path and Path(path).name == _WATCHED_NAME:
            self._watcher._on_change()


__all__ = ["WorkspaceWatcher", "DEBOUNCE_SECONDS"]

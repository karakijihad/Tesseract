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
        return len(changed)

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

"""WorkspaceEvent / WorkspaceComment data model + JSONL store.

Append-only JSONL under ``tesseract/logs/workspace/``:

- ``events.jsonl``    — one row per event (proposal, approval-request, nudge,
                        autonomous post). Status mutates in-place via
                        rewrite (the file is small enough; a
                        status-overlay JSONL would suit a bigger one).
- ``comments.jsonl``  — one row per operator comment OR the assistant reply.
- ``seen.json``       — ``{"inbox": iso_ts, "stream": iso_ts}`` last-seen
                        markers; persisted so badge counts survive backend
                        restart, not just browser reload.

The store is intentionally tiny. The Inbox is its only consumer; a
Stream view would read the same events.jsonl with a kind-filter.

Concurrency: append + rewrite paths take both a per-instance
``threading.Lock`` (intra-process) AND a cross-process advisory file
lock on ``.lock`` in the same directory (msvcrt on Windows, fcntl on
POSIX). Mirror + REPL + scheduler can hold their own EventStore each;
the file lock serializes their writes so the ``Path.replace()`` rewrite
cannot race a concurrent open from another process. Codex audit
2026-05-06 M5.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Any, Iterator, Literal

log = logging.getLogger(__name__)


EventKind = Literal[
    "feedback_proposal",         # Layer B consolidator (merge / soul / archive)
    "feedback_sweep",            # Layer C daily transcript sweep
    "agent_approval",            # agent_promote pending
    "soul_proposal",             # legacy — superseded by change_proposal (kept for back-compat reads)
    "change_proposal",           # propose_change / soul_growth_propose — operator-gated workspace mutation
    "mission_reflection_proposal",  # historical records only — mission engine deleted; kept so old workspace events still deserialize
    "reflection_proposal",       # session reflection — informational, writes already happened, Resolve only
    "nudge",                     # generic operator-attention request
    "agent_post",                 # the assistant chose to post (workspace_post)
    "operator_post",             # operator-initiated thread (scratchpad, button, voice, hotkey)
    "daily_brief",               # Newsletter card: prose notes and the vault list, each section reading a store this machine owns
    "yaml_change_proposal",      # Knowledge-keeper proposes a catalog edit; apply path mutates the YAML on approve
    "kb_merge_conflict",         # Refresher + operator edited the same paragraph; KB file untouched, operator resolves
    "recovery_summary",          # RecoveryManager emits one per boot with per-scan counts + operator_attention list
    "vault_raw_ingest_batch",    # VaultRawWatchJob bundles ASK-routed files; approve runs vault_ingest per file
    "clarification",             # Agent asks the operator a question via async workspace thread; operator answers in comments
    "strategist_summary",        # Weekly initiative curator one-shot summary of all emitted Initiative items
    "runtime_lock_deny",         # SU-1/SU-5 — file_write or bash attempted to mutate a locked runtime path or config yaml; operator-visible audit surface
    "skill_approval",            # skill_create drafted a skill into quarantine; approve promotes, reject archives (mirror agent_approval)
    "skill_refinement",          # refinement job flags an underperforming skill + proposes a revised body; approve applies the diff to the live SKILL.md
    "working_set_proposal",      # working_set_review proposes which tools and playbooks the turn carries; approve moves the names through each file's own generator
    "tuning_proposal",           # runtime_tuning proposes a change to what the runtime spends or how often it runs; approve moves the seam the card names
    "project_proposal",          # project_propose puts up an idea no existing project covers; approving means it is worth starting, and creates nothing
]


#: The kinds that carry a decision the operator has to make: every one of them
#: is something `routes/workspace.py::apply_decision` accepts an approve or a
#: reject for. Declared here beside `EventKind` rather than in whichever
#: surface asked first, because two answers to "what is waiting on you" is how
#: one surface starts under-reporting: `/queue` counted `agent_post`, the one
#: inbox kind that asks for nothing, and every pending soul edit went missing
#: from the only view a phone had.
DECIDABLE_KINDS: tuple[str, ...] = (
    "agent_approval",
    "skill_approval",
    "skill_refinement",
    "working_set_proposal",
    "tuning_proposal",
    "change_proposal",
    "soul_proposal",
    "feedback_proposal",
    "yaml_change_proposal",
    "vault_raw_ingest_batch",
    "kb_merge_conflict",
    "clarification",
    "project_proposal",
    "nudge",
)

#: What an event's status says once it has been decided. Anything else is still
#: open, which is the safe way round: a kind that grows a new terminal status
#: over-reports by one until this list learns it, where the opposite silently
#: hides work.
SETTLED: frozenset[str] = frozenset(
    {"approved", "rejected", "applied", "resolved", "deleted", "closed"}
)

EventStatus = Literal["pending", "approved", "rejected", "resolved", "applied", "deleted"]
EventSource = Literal[
    "feedback_consolidator",
    "feedback_sweep",
    "orchestrator",
    "operator",
    "daily_brief",               # BriefRenderer write fan-out
    "knowledge_keeper",          # KB refresher + yaml_change_proposal emitter
    "recovery",                  # Boot-time RecoveryManager
    # Anything the assistant itself emits: workspace posts, clarification
    # questions, self-direction proposals, skill and soul drafts. One
    # member, not two: a persona-named one and an agent-authored one mean
    # the same actor and nothing ever discriminated between them.
    "agent",
    "strategist",                # Autonomy strategist (initiative curator)
    "security",                  # runtime_lock_deny emissions from file_write + bash_security
]
OperatorPostSource = Literal["button", "scratchpad", "voice", "hotkey", "telegram"]
Author = Literal["operator", "agent"]


@dataclass(frozen=True)
class WorkspaceEvent:
    event_id: str
    ts: str
    kind: EventKind
    source: EventSource
    title: str
    summary: str
    payload: dict[str, Any]
    status: EventStatus = "pending"
    priority: int = 5  # 1 = lowest, 10 = drop-everything
    decided_at: str | None = None
    decided_reason: str | None = None
    # Mark-once flag for events the chat-loop drains as one-shot turn
    # injections (Workstream D — `operator_post`). Harmless on other
    # kinds; only the drain helper reads it.
    delivered_to_agent: bool = False
    # Identity of the human (or system) that authored this event. Today
    # "operator" or "system"; the multi-user substrate will resolve
    # external-channel senders to a stable slug (e.g. "telegram:<chat_id>")
    # so per-tier scoping has something to key on without re-parsing
    # payload. Defaults preserve backward-compat for events written before
    # this field landed.
    author_id: str = "operator"
    author_display: str = "Operator"

    def with_status(
        self,
        status: EventStatus,
        *,
        reason: str | None = None,
    ) -> "WorkspaceEvent":
        return WorkspaceEvent(
            event_id=self.event_id,
            ts=self.ts,
            kind=self.kind,
            source=self.source,
            title=self.title,
            summary=self.summary,
            payload=self.payload,
            status=status,
            priority=self.priority,
            decided_at=datetime.now(timezone.utc).isoformat(),
            decided_reason=reason,
            delivered_to_agent=self.delivered_to_agent,
            author_id=self.author_id,
            author_display=self.author_display,
        )

    def with_delivered(self) -> "WorkspaceEvent":
        return WorkspaceEvent(
            event_id=self.event_id,
            ts=self.ts,
            kind=self.kind,
            source=self.source,
            title=self.title,
            summary=self.summary,
            payload=self.payload,
            status=self.status,
            priority=self.priority,
            decided_at=self.decided_at,
            decided_reason=self.decided_reason,
            delivered_to_agent=True,
            author_id=self.author_id,
            author_display=self.author_display,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def new(
        cls,
        *,
        kind: EventKind,
        source: EventSource,
        title: str,
        summary: str,
        payload: dict[str, Any],
        priority: int = 5,
        author_id: str = "operator",
        author_display: str = "Operator",
    ) -> "WorkspaceEvent":
        return cls(
            event_id=f"evt_{uuid.uuid4().hex[:12]}",
            ts=datetime.now(timezone.utc).isoformat(),
            kind=kind,
            source=source,
            title=title.strip()[:200],
            summary=summary.strip()[:1200],
            payload=payload,
            priority=max(1, min(10, int(priority))),
            author_id=author_id,
            author_display=author_display,
        )


@dataclass(frozen=True)
class WorkspaceComment:
    comment_id: str
    event_id: str
    ts: str
    author: Author
    body: str
    reply_to: str | None = None
    delivered_to_agent: bool = False  # operator → agent; flipped after drain

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def new(
        cls,
        *,
        event_id: str,
        author: Author,
        body: str,
        reply_to: str | None = None,
    ) -> "WorkspaceComment":
        return cls(
            comment_id=f"cmt_{uuid.uuid4().hex[:12]}",
            event_id=event_id,
            ts=datetime.now(timezone.utc).isoformat(),
            author=author,
            body=body.strip()[:4000],
            reply_to=reply_to,
        )


def _row_that_is_the_card(rows: list[WorkspaceEvent], event_id: str) -> int | None:
    """Which row IS the card for `event_id`: the newest one, or None.

    The file is append-only and an id may repeat on purpose. The recovery
    summary is one card rewritten every boot rather than a new card each time
    (`orchestrator/recovery/summary.py::EVENT_ID`, which says so and says the
    newest row wins), so ten rows can carry one id.

    Every reader here already takes the newest. Every WRITER must take the same
    one: a mutation that scans forward answers the oldest row instead, and the
    operator then resolves the card they were shown, gets an ok, and watches it
    stay pending while a historical shadow moves. That was live for three
    mutations at once, and it is why the pick lives in one function rather than
    being written out at each call site.
    """
    for idx in range(len(rows) - 1, -1, -1):
        if rows[idx].event_id == event_id:
            return idx
    return None


class EventStore:
    """Append-only event/comment store under ``logs_dir / 'workspace'``.

    Reads scan the file each call. The volume is tiny (the Inbox is
    curated, well under 100 events a week) and re-scan is simpler than an
    in-memory index that has to stay coherent with mutations.
    """

    def __init__(self, logs_dir: Path) -> None:
        self._dir = Path(logs_dir) / "workspace"
        self._lock = threading.Lock()
        self._dir.mkdir(parents=True, exist_ok=True)
        self._file_lock_path = self._dir / ".lock"

    @property
    def events_path(self) -> Path:
        return self._dir / "events.jsonl"

    @property
    def comments_path(self) -> Path:
        return self._dir / "comments.jsonl"

    @property
    def seen_path(self) -> Path:
        return self._dir / "seen.json"

    @contextmanager
    def _interprocess_lock(self) -> Iterator[None]:
        """Advisory cross-process exclusive lock on ``<dir>/.lock``.

        Acquired around every append/rewrite so Mirror + REPL + scheduler
        writers serialise even though they hold separate EventStore
        instances. Released on context exit. Falls open silently if the
        platform lock primitive is unavailable — better to risk the
        existing race than to crash the writer.
        """
        fh: IO[bytes] | None = None
        try:
            # ``a+b`` so the file is created if missing without truncating.
            fh = open(self._file_lock_path, "a+b")
            if sys.platform == "win32":
                try:
                    import msvcrt
                    # Lock 1 byte starting at offset 0; LK_LOCK blocks until
                    # acquired. Windows file locks are mandatory, so any
                    # second-process append/rewrite waits here.
                    msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
                    locked = True
                except (OSError, ImportError):
                    log.warning("workspace EventStore: msvcrt lock unavailable; falling back")
                    locked = False
            else:
                try:
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
                    locked = True
                except (OSError, ImportError):
                    log.warning("workspace EventStore: fcntl lock unavailable; falling back")
                    locked = False
            try:
                yield
            finally:
                if locked:
                    try:
                        if sys.platform == "win32":
                            import msvcrt
                            try:
                                fh.seek(0)
                            except OSError:
                                pass
                            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                        else:
                            import fcntl
                            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                    except OSError:
                        log.exception("workspace EventStore: lock release failed")
        finally:
            if fh is not None:
                try:
                    fh.close()
                except OSError:
                    pass

    def append_event(self, event: WorkspaceEvent) -> WorkspaceEvent:
        with self._lock, self._interprocess_lock():
            with self.events_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event.to_dict()) + "\n")
        return event

    def append_comment(self, comment: WorkspaceComment) -> WorkspaceComment:
        with self._lock, self._interprocess_lock():
            with self.comments_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(comment.to_dict()) + "\n")
        return comment

    def list_events(
        self,
        *,
        kinds: tuple[EventKind, ...] | None = None,
        status: EventStatus | None = None,
        limit: int = 200,
    ) -> list[WorkspaceEvent]:
        rows = self._read_events()
        latest: dict[str, WorkspaceEvent] = {}
        for ev in rows:
            latest[ev.event_id] = ev
        result = list(latest.values())
        if kinds is not None:
            result = [ev for ev in result if ev.kind in kinds]
        if status is not None:
            result = [ev for ev in result if ev.status == status]
        # Newest first, and only that. Priority as the primary key buries a
        # note written minutes ago under a day of `p=8` recovery_summary
        # boots, and the inbox stops reading like an inbox. Priority is
        # carried on the event and the panel badges it; it does not decide
        # where a row sits.
        result.sort(key=lambda e: e.ts, reverse=True)
        return result[:limit]

    def append_if_none_pending(self, event: "WorkspaceEvent") -> bool:
        """Append `event` unless one of its kind is already pending. True if written.

        **The check and the write are one act.** `one_is_pending` followed by
        `append_event` is two, and the read takes no lock at all, so two callers
        proposing at the same moment could each see an empty queue and each
        write: the operator's own chat and a scheduled turn are exactly two such
        callers. `is_concurrency_safe() -> False` does not help, because that
        only serialises tool calls inside one turn's dispatch batch.

        Both locks are held across the pair, the same pair `append_event` takes
        for its write alone, so a second writer waits for the first to finish
        before it looks.
        """
        with self._lock, self._interprocess_lock():
            # `_read_events_unlocked`, because the locking reader takes the
            # same two locks and neither is reentrant: calling it from inside
            # here deadlocks the process against itself.
            latest: dict[str, WorkspaceEvent] = {}
            for ev in self._read_events_unlocked():
                latest[ev.event_id] = ev
            if any(
                ev.kind == event.kind and ev.status == "pending"
                for ev in latest.values()
            ):
                return False
            with self.events_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event.to_dict()) + "\n")
        return True

    def one_is_pending(self, kind: str) -> bool:
        """Whether a card of this kind is already waiting to be decided.

        One at a time, per kind, and the rule lives here because two callers
        need it: a job filing a proposal (`scheduler/tasks/_card.py`) and a
        tool filing one (`kernel/tools/project_propose.py`). It was written
        twice, which is two places a change to the unreadable-queue policy has
        to reach.

        A queue that cannot be read reads as EMPTY on purpose: the cost of a
        duplicate card is an extra decision, and the cost of the other answer
        is a proposal nobody ever sees.
        """
        try:
            return bool(self.list_events(kinds=(kind,), status="pending"))  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001 - see the docstring
            log.warning("workspace events: the queue could not be read for %s", kind)
            return False

    def get_event(self, event_id: str) -> WorkspaceEvent | None:
        rows = self._read_events()
        idx = _row_that_is_the_card(rows, event_id)
        return rows[idx] if idx is not None else None

    def list_comments(self, event_id: str) -> list[WorkspaceComment]:
        rows = self._read_comments()
        thread = [c for c in rows if c.event_id == event_id]
        thread.sort(key=lambda c: c.ts)
        return thread

    def list_all_comments(self, *, limit: int = 400) -> list[WorkspaceComment]:
        """Every comment, newest first, whoever wrote it.

        `list_comments` answers about one thread, which cannot serve a reader
        asking what was said across the whole stream in a window. The daily
        brief needs that, and reaching `_read_comments` from another module
        would put a second reader of this file outside the class that owns
        its format.
        """
        rows = self._read_comments()
        rows.sort(key=lambda c: c.ts, reverse=True)
        return rows[:limit]

    def get_comment(self, comment_id: str) -> WorkspaceComment | None:
        """Return the comment with `comment_id`, or None.

        Codex-fix M2 (2026-05-23): the live `workspace_reply` broadcast
        now identifies the exact appended comment via its id instead of
        scanning for "the latest the assistant comment" — that heuristic raced
        under concurrent synthetic turns.
        """
        for c in reversed(self._read_comments()):
            if c.comment_id == comment_id:
                return c
        return None

    def list_undelivered_operator_comments(self) -> list[WorkspaceComment]:
        return [
            c for c in self._read_comments()
            if c.author == "operator" and not c.delivered_to_agent
        ]

    def list_undelivered_operator_posts(self) -> list[WorkspaceEvent]:
        return [
            e for e in self._read_events()
            if e.kind == "operator_post" and not e.delivered_to_agent
        ]

    def mark_event_delivered(self, event_id: str) -> bool:
        with self._lock, self._interprocess_lock():
            rows = self._read_events_unlocked()
            idx = _row_that_is_the_card(rows, event_id)
            if idx is None or rows[idx].delivered_to_agent:
                return False
            rows[idx] = rows[idx].with_delivered()
            self._rewrite_events_unlocked(rows)
            return True

    def mark_comment_delivered(self, comment_id: str) -> bool:
        with self._lock, self._interprocess_lock():
            rows = self._read_comments_unlocked()
            updated = False
            for idx, c in enumerate(rows):
                if c.comment_id == comment_id and not c.delivered_to_agent:
                    rows[idx] = WorkspaceComment(
                        comment_id=c.comment_id,
                        event_id=c.event_id,
                        ts=c.ts,
                        author=c.author,
                        body=c.body,
                        reply_to=c.reply_to,
                        delivered_to_agent=True,
                    )
                    updated = True
                    break
            if updated:
                self._rewrite_comments_unlocked(rows)
            return updated

    def update_event_status(
        self,
        event_id: str,
        status: EventStatus,
        *,
        reason: str | None = None,
    ) -> WorkspaceEvent | None:
        with self._lock, self._interprocess_lock():
            rows = self._read_events_unlocked()
            idx = _row_that_is_the_card(rows, event_id)
            if idx is None:
                return None
            rows[idx] = rows[idx].with_status(status, reason=reason)
            self._rewrite_events_unlocked(rows)
            return rows[idx]

    def merge_event_payload(
        self,
        event_id: str,
        updates: dict[str, Any],
    ) -> WorkspaceEvent | None:
        """Shallow-merge ``updates`` into the event's payload and rewrite the row.

        Used by the EB snooze route: the operator's snooze choice writes
        ``{"snoozed_until": iso}`` without flipping the event's status, so
        the card stays pending while the frontend hides it until the
        snooze expires.
        """
        with self._lock, self._interprocess_lock():
            rows = self._read_events_unlocked()
            idx = _row_that_is_the_card(rows, event_id)
            if idx is not None:
                ev = rows[idx]
                merged = {**(ev.payload or {}), **updates}
                rows[idx] = WorkspaceEvent(
                    event_id=ev.event_id,
                    ts=ev.ts,
                    kind=ev.kind,
                    source=ev.source,
                    title=ev.title,
                    summary=ev.summary,
                    payload=merged,
                    status=ev.status,
                    priority=ev.priority,
                    decided_at=ev.decided_at,
                    decided_reason=ev.decided_reason,
                    delivered_to_agent=ev.delivered_to_agent,
                    author_id=ev.author_id,
                    author_display=ev.author_display,
                )
                self._rewrite_events_unlocked(rows)
                return rows[idx]
        return None

    def get_seen(self) -> dict[str, str]:
        # External readers acquire both locks for symmetry with set_seen;
        # `set_seen` itself reuses the unlocked variant since it already
        # holds the locks (advisory file locks are non-reentrant).
        with self._lock, self._interprocess_lock():
            return self._get_seen_unlocked()

    def _get_seen_unlocked(self) -> dict[str, str]:
        if not self.seen_path.exists():
            return {}
        try:
            return json.loads(self.seen_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            log.warning("workspace seen.json unreadable; resetting")
            return {}

    def set_seen(self, panel: str, ts: str) -> None:
        with self._lock, self._interprocess_lock():
            data = self._get_seen_unlocked()
            data[panel] = ts
            self.seen_path.write_text(json.dumps(data), encoding="utf-8")

    # --- internals ---

    def _read_events(self) -> list[WorkspaceEvent]:
        # Symmetric file-lock with the writers so a future non-atomic
        # rewrite path can't expose a torn file to readers (today's
        # ``Path.replace()`` is atomic, but the asymmetry is brittle —
        # reviewer follow-up to Codex M5).
        with self._lock, self._interprocess_lock():
            return self._read_events_unlocked()

    def _read_events_unlocked(self) -> list[WorkspaceEvent]:
        if not self.events_path.exists():
            return []
        rows: list[WorkspaceEvent] = []
        for line in self.events_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                rows.append(WorkspaceEvent(**data))
            except (json.JSONDecodeError, TypeError) as exc:
                log.warning("workspace events.jsonl bad row: %s", exc)
        return rows

    def _rewrite_events_unlocked(self, rows: list[WorkspaceEvent]) -> None:
        tmp = self.events_path.with_suffix(".jsonl.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            for ev in rows:
                fh.write(json.dumps(ev.to_dict()) + "\n")
        tmp.replace(self.events_path)

    def _read_comments(self) -> list[WorkspaceComment]:
        with self._lock, self._interprocess_lock():
            return self._read_comments_unlocked()

    def _read_comments_unlocked(self) -> list[WorkspaceComment]:
        if not self.comments_path.exists():
            return []
        rows: list[WorkspaceComment] = []
        for line in self.comments_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                rows.append(WorkspaceComment(**data))
            except (json.JSONDecodeError, TypeError) as exc:
                log.warning("workspace comments.jsonl bad row: %s", exc)
        return rows

    def _rewrite_comments_unlocked(self, rows: list[WorkspaceComment]) -> None:
        tmp = self.comments_path.with_suffix(".jsonl.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            for c in rows:
                fh.write(json.dumps(c.to_dict()) + "\n")
        tmp.replace(self.comments_path)

    def prune_settled_before(self, cutoff: datetime) -> int:
        """Drop every row of every event that is SETTLED and older than
        `cutoff`, and the comments on it. Returns how many rows went.

        **Per event, never per row.** The inbox reads the newest row for an id
        and an older row is that event's history, so deleting one row at a time
        would eventually delete a `resolved` row and leave the `pending` row
        under it as the newest one, putting an answered decision back in front
        of the operator. So an event ages as a whole, on its newest row, and
        only when that row says it has been settled.

        A pending event never ages, whatever its date. A window that can delete
        an unanswered decision is not a window, it is a way to lose one.
        """
        with self._lock, self._interprocess_lock():
            rows = self._read_events_unlocked()
            if not rows:
                return 0
            newest: dict[str, WorkspaceEvent] = {}
            for ev in rows:
                newest[ev.event_id] = ev
            drop = {
                event_id for event_id, ev in newest.items()
                if ev.status in SETTLED and _before(ev.ts, cutoff)
            }
            if not drop:
                return 0
            keep = [ev for ev in rows if ev.event_id not in drop]
            removed = len(rows) - len(keep)
            self._rewrite_events_unlocked(keep)
            comments = self._read_comments_unlocked()
            kept_comments = [c for c in comments if c.event_id not in drop]
            if len(kept_comments) != len(comments):
                self._rewrite_comments_unlocked(kept_comments)
        return removed

    def has_pending_yaml_proposal(
        self,
        *,
        target_path: str,
        yaml_path: str,
        kind_origin: str,
    ) -> bool:
        """Emit-time dedup helper.

        Returns True when the inbox already carries a pending
        ``yaml_change_proposal`` matching the given target_path /
        yaml_path / kind_origin triple. The knowledge-keeper checks this
        before emitting a fresh proposal so a slow operator doesn't get
        a wall of duplicates.
        """
        for ev in self._read_events():
            if ev.kind != "yaml_change_proposal" or ev.status != "pending":
                continue
            payload = ev.payload or {}
            if (
                payload.get("target_path") == target_path
                and payload.get("yaml_path") == yaml_path
                and payload.get("kind_origin") == kind_origin
            ):
                return True
        return False


def _before(ts: str, cutoff: datetime) -> bool:
    """Whether a row's stamp is older than the cutoff.

    An unparsable or missing stamp reads as NOT older, which keeps it. A row
    whose date nobody can read is the one case where deleting on a guess costs
    something and keeping costs a line.
    """
    if not ts:
        return False
    try:
        when = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when < cutoff

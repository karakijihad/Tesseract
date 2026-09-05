"""The second attempt at answering an operator's workspace comment.

A comment posted on a thread fires one fire-and-forget dispatch to the
controller from the HTTP route, and that was the whole of it. If that attempt
failed, the operator was left with a question in a thread, no answer, and
nothing anywhere saying so.

Nothing marked a comment answered either. `mark_comment_delivered` had one
caller, on a synthetic-turn path that has no producer, so
`list_undelivered_operator_comments` returned every operator comment ever
written however well it had been answered. That is why this module cannot
trust the flag alone: on the first sweep after it ships, every historical
comment is in that queue, and replying to them would be a duplicate answer on
every thread the operator has ever used. The flag is the fast path and the
thread is the truth, so a comment with a later agent reply on its thread is
marked answered instead of dispatched.

Measured while building this, and it corrected the diagnosis that started it:
the comment that prompted the work had been answered 32 seconds after it was
posted. The timeout in the log two hours later belonged to a different
dispatch. So the reply path was working, and what was missing was any record
that it had.

Bounded on purpose. A thread whose reply keeps failing is a thread the
operator should be told about, not one to dispatch against for ever, so a
comment is tried `max_attempts` times and then left alone with a warning that
names it.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# comment_id / event_id -> attempts already made by this process. In memory on
# purpose: a backend restart is a new chance for a comment still outstanding,
# and the durable half is the store's own `delivered_to_agent` flag.
_attempts: dict[str, int] = {}


def reset() -> None:
    """Forget the attempt counts. For tests and for a config reload."""
    _attempts.clear()


def _age_seconds(ts: str, now: datetime) -> float:
    """Seconds since an ISO timestamp, or 0 when it cannot be read.

    0 reads as "just posted", which keeps an unparseable timestamp out of the
    sweep rather than dispatching against it immediately.
    """
    try:
        when = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return 0.0
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (now - when).total_seconds())


def _already_answered(store: Any, comment: Any) -> bool:
    """True when an agent comment landed on this thread after the operator's.

    The dispatch is fire-and-forget and the backend can stop waiting on a
    controller that goes on to write its reply anyway, so "the backend saw it
    succeed" and "the operator has an answer" are not the same question. This
    asks the second one.
    """
    try:
        thread = store.list_comments(comment.event_id)
    except Exception:  # noqa: BLE001 — an unreadable thread is not an answer
        logger.exception(
            "workspace reply retry: could not read the thread for %s",
            comment.comment_id,
        )
        return False
    return any(c.author == "agent" and c.ts > comment.ts for c in thread)


def _store() -> Any:
    from tesseract.kernel.workspace_changes import workspace_events_dir
    from tesseract.workspace_events import EventStore

    return EventStore(workspace_events_dir())


async def sweep(app: Any, *, now: datetime | None = None) -> int:
    """Dispatch a reply for every outstanding item old enough to be stalled.

    Returns how many were dispatched. Never raises: this runs on a loop that
    must outlive one bad pass.
    """
    from tesseract.orchestrator.autonomy.workspace_reply_dispatch import (
        dispatch_workspace_reply,
        load_workspace_reply_config,
    )

    cfg = load_workspace_reply_config()
    if not cfg.enabled:
        return 0

    now = now or datetime.now(timezone.utc)
    store = _store()
    dispatched = 0

    for comment in store.list_undelivered_operator_comments():
        if _age_seconds(comment.ts, now) < cfg.retry_after_seconds:
            continue  # the first attempt may still be running
        # The flag is the fast path; the thread is the truth. Nothing marked a
        # comment delivered before this change, so every comment already on
        # disk is in this queue however well it was answered, and a sweep that
        # trusted the flag alone would reply a second time to all of them.
        # Measured while building this: the comment that started it had been
        # answered 32 seconds after it was posted.
        if _already_answered(store, comment):
            store.mark_comment_delivered(comment.comment_id)
            logger.info(
                "workspace reply retry: %s already has an answer on its "
                "thread; marking it delivered rather than replying twice",
                comment.comment_id,
            )
            continue
        tried = _attempts.get(comment.comment_id, 0)
        if tried >= cfg.max_attempts:
            continue
        event = store.get_event(comment.event_id)
        if event is None:
            logger.warning(
                "workspace reply retry: comment %s names event %s, which is not "
                "in the store; leaving it alone",
                comment.comment_id, comment.event_id,
            )
            _attempts[comment.comment_id] = cfg.max_attempts
            continue

        _attempts[comment.comment_id] = tried + 1
        logger.warning(
            "workspace reply retry: %s has been waiting %.0fs with no answer; "
            "dispatching again (attempt %d of %d)",
            comment.comment_id, _age_seconds(comment.ts, now),
            tried + 1, cfg.max_attempts,
        )
        try:
            await dispatch_workspace_reply(
                app,
                event_id=comment.event_id,
                comment_id=comment.comment_id,
                event=event,
                kind="comment",
                comment_text=comment.body,
                config=cfg,
            )
        except Exception:  # noqa: BLE001 — one bad item must not end the pass
            logger.exception(
                "workspace reply retry: dispatch raised for %s", comment.comment_id
            )
        dispatched += 1
        if _attempts[comment.comment_id] >= cfg.max_attempts:
            logger.warning(
                "workspace reply retry: %s has failed %d times and will not be "
                "tried again. The operator asked something on event %s and has "
                "no answer.",
                comment.comment_id, cfg.max_attempts, comment.event_id,
            )

    return dispatched


async def retry_loop(app: Any) -> None:
    """Look on the configured interval, for the life of the backend.

    The config read is inside the guard for the same reason the spawn
    heartbeat's is: a file being rewritten as the loop reads it is a reason to
    keep the last good interval and look again, not a reason for the thing
    that catches unanswered questions to stop looking.
    """
    interval = load_interval()
    while True:
        try:
            await sweep(app)
            interval = load_interval()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — one bad pass must not end the loop
            logger.exception(
                "workspace reply retry: pass failed, looking again in %.0fs",
                interval,
            )
        await asyncio.sleep(interval)


def load_interval() -> float:
    from tesseract.orchestrator.autonomy.workspace_reply_dispatch import (
        load_workspace_reply_config,
    )

    return load_workspace_reply_config().retry_interval_seconds


__all__ = ["load_interval", "reset", "retry_loop", "sweep"]

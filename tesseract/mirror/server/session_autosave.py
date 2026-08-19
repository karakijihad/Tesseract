"""Autosave — write the open session to disk while it is still running.

The teardown save in ``ws_connection`` is at the mercy of the clean shutdown
path. Anything that skips the WS receive loop's ``finally`` — the supervisor
escalating past its graceful-stop grace period, a backend crash, a power cut —
leaves a whole session with nothing on disk, however many turns it ran. The
operator's workaround was typing ``/save`` by hand.

One timer, no bookkeeping: every ``mirror.yaml::session.autosave_interval_seconds``
the open session is written. There is no change detection, because deciding
whether a session is worth writing costs more than writing it.

The write is synchronous ON the event loop, deliberately. Measured on real
files: 8.6 KB installed and 25 KB in dev, both ~1 ms; a synthetic 2 MB session
is 40 ms, still inside the 50 ms the loop may block for, and compaction bounds history
long before that. Handing it to a worker thread would buy nothing and cost the
one guarantee that matters here — a synchronous write has no await inside it,
so teardown cancelling this pump can never land mid-write and leave two writers
on the same file.

What it writes is what a reconnect reads: ``sessions/chats/<chat_id>.json``,
one file per chat, which ``chat_restore`` rehydrates from. Recall indexing stays
on the teardown path — re-indexing every interval would buy nothing a reconnect
sees.

There is no second file. This pump used to also write a whole-history snapshot
under a name it minted from the clock, which is how one conversation became
eleven files in a day: the name had minute resolution and a connection is one
session, so every boot started a new one. A chat already has an id, so the write
lands on the record it belongs to and updating it is the same act as creating
it.
"""

from __future__ import annotations

import asyncio
import logging
import math

import yaml
from aiohttp import web

from tesseract.mirror.server import chat_store
from tesseract.mirror.server.config import MIRROR_YAML
from tesseract.mirror.server.session_model import ServerSession

log = logging.getLogger(__name__)

# The cadence the settings route accepts, and the cadence the pump will run.
# One definition: the route imports these, so a value the operator can save
# through the UI and a value the pump will honour cannot drift apart. The floor
# exists because the write is synchronous on the event loop; the ceiling
# because past it "autosave" stops meaning anything.
AUTOSAVE_MIN_SECONDS = 10
AUTOSAVE_MAX_SECONDS = 3600

# The last out-of-range value we complained about. Settings are re-read every
# tick, so an unlatched warning would repeat for the life of every connection —
# roughly 8,600 lines a day per session for one misconfiguration that is
# already being handled. The Pulse feed shows warnings, so that noise would
# crowd the operator's own signal.
_CLAMP_WARNED: float | None = None


def autosave_settings() -> tuple[bool, float]:
    """``(enabled, interval_seconds)`` from ``mirror.yaml::session``.

    Raises on a missing key rather than defaulting: a silent fallback here is a
    durability guarantee that quietly is not the one the operator configured.

    The interval is clamped rather than trusted. The route validates what it
    writes, but nothing validates what a hand-edited yaml contains, and `0`
    reaches `asyncio.sleep` as a busy loop that rewrites every chat on every
    iteration of the event loop. Clamping is loud (a warning naming both
    values) but keeps the session durable, which refusing would not.
    """
    raw = yaml.safe_load(MIRROR_YAML.read_text(encoding="utf-8")) or {}
    block = raw.get("session") if isinstance(raw, dict) else None
    if not isinstance(block, dict):
        raise KeyError("mirror.yaml::session block missing — no implicit defaults")
    for key in ("autosave", "autosave_interval_seconds"):
        if key not in block:
            raise KeyError(f"mirror.yaml::session.{key} missing — no implicit defaults")
    raw_interval = float(block["autosave_interval_seconds"])
    if not math.isfinite(raw_interval):
        raise ValueError(
            f"mirror.yaml::session.autosave_interval_seconds is not a number: {raw_interval!r}"
        )
    interval = min(max(raw_interval, AUTOSAVE_MIN_SECONDS), AUTOSAVE_MAX_SECONDS)
    global _CLAMP_WARNED
    if interval != raw_interval:
        if _CLAMP_WARNED != raw_interval:
            _CLAMP_WARNED = raw_interval
            log.warning(
                "autosave: interval %gs is outside %d-%ds, using %gs",
                raw_interval, AUTOSAVE_MIN_SECONDS, AUTOSAVE_MAX_SECONDS, interval,
            )
    elif _CLAMP_WARNED is not None:
        _CLAMP_WARNED = None
    return bool(block["autosave"]), interval


def save_now(app: web.Application, session: ServerSession) -> int:
    """Write every chat with history to its own record. Returns chats written.

    Synchronous on purpose — see the module docstring.

    Chats with no history are skipped: a freshly-created empty chat rewritten
    every interval is pure churn, and on an idle session it would be the only
    disk activity autosave produces. Teardown still persists them, because
    archive state belongs on disk even when the transcript is empty.
    """
    opts = app.get("adapter_options")
    return chat_store.persist_session_chats(
        session, skip_empty=True, model=getattr(opts, "model", "") or "",
    )


async def autosave_pump(app: web.Application, session: ServerSession) -> None:
    """Per-connection timer. Cancelled in ``websocket_handler.finally``.

    The config read is caught rather than left to propagate. Nothing retrieves
    this task's exception — the scheduler's done-callback only discards it, and
    teardown skips a task already `done()` — so an unhandled raise here would
    disable autosave on every connection while looking exactly like a working
    one.
    """
    try:
        enabled, interval = autosave_settings()
    except Exception:
        log.exception(
            "autosave DISABLED for %s — could not read its settings; a session "
            "that ends badly will lose its turns",
            session.session_id,
        )
        return
    while True:
        await asyncio.sleep(interval)
        # Re-read each tick so the Settings switch takes effect on the open
        # session rather than on the next one. The operator flipping a toggle
        # and watching nothing happen is how a setting stops being believed.
        # A yaml that has gone unreadable keeps the last good values — the
        # cadence is not worth dropping durability over.
        try:
            enabled, interval = autosave_settings()
        except Exception:
            log.warning(
                "autosave: settings unreadable for %s, keeping enabled=%s interval=%ss",
                session.session_id, enabled, interval,
            )
        if not enabled:
            continue
        try:
            save_now(app, session)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("autosave failed for %s", session.session_id)

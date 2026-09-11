"""When the operator was last here, on any surface at all.

One value, not one per surface. The chat store holds a last message time per
chat, the channel state holds one per chat id, and the workspace store stamps
the moment a card was answered, so every surface can say when it last heard
from the operator and none of them can say when the operator was last HERE.
That is the question a return note asks, and it is the only question this
file answers.

Machine-local, beside the brief's delivery marker, for the same reason that
one is: it records what this machine has seen, it must survive a backend
restart, and a value synced between machines would say the operator was
present at a desk they were nowhere near.

Best effort in both directions. A write that fails must never break the turn
the operator is in the middle of, and a marker that will not parse reads as
absent rather than as an error, because the caller's fallback is to say
nothing.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from tesseract.lib.atomic_replace import replace_with_retry

log = logging.getLogger(__name__)

_FILENAME = "operator-last-seen.json"


def marker_path() -> Path:
    from tesseract.paths import runtime_dir

    return runtime_dir() / _FILENAME


def record(*, now: datetime | None = None) -> None:
    """The operator spoke. Written from every door they can speak through.

    **What counts as speaking**, because two doors could reasonably read it
    differently and one of them nearly did: anything the runtime ACCEPTS from
    the operator counts, whatever it turns out to be. A `/status`, a `yes`
    answering a prompt and a tapped button are all the operator being here; a
    payload carrying no text and no attachment is not, and neither is a
    message the bridge turns away. So the cockpit records after its
    empty-payload check and the channel records after its allowlist check,
    and those are the same rule rather than two.

    Atomic, unlike the delivery marker it sits beside: this one is written on
    every inbound message rather than once a day, so the window in which a
    kill lands mid-write is the one that is actually reachable.
    """
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    path = marker_path()
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps({"at": stamp.isoformat()}), encoding="utf-8")
        replace_with_retry(tmp, path)
    except OSError:
        log.debug("last_seen: could not write %s", path, exc_info=True)


def read() -> datetime | None:
    """The instant, in UTC, or None if this machine has never seen them.

    None is the honest answer on a fresh install and on a marker written
    before this existed. A caller must not read it as a long absence: nothing
    happened, so there is nothing to report.
    """
    try:
        raw = json.loads(marker_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    from tesseract.lib.clock import parse_stamp

    parsed = parse_stamp(str(raw.get("at") or "")) if isinstance(raw, dict) else None
    return parsed.astimezone(timezone.utc) if parsed is not None else None


__all__ = ["marker_path", "read", "record"]

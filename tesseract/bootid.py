"""One boot id per process, minted once and shared.

Recovery records a boot id and the backend log is named after one. They are
only useful together — "show me what happened on the boot that raised this" is
the question both exist to answer — so they must be the SAME id. Minting per
call (the previous behaviour) produced a fresh uuid every time and made the
two unjoinable.

Kept as its own module rather than living in ``recovery/`` so the logging
setup can import it without pulling the orchestrator in at process start.
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone

_FORMAT = "boot-%Y%m%dT%H%M%S"

_lock = threading.Lock()
_current: str | None = None


def mint_boot_id(*, now: datetime | None = None) -> str:
    """A new id, unconditionally. ``boot-YYYYMMDDTHHMMSS-<8 hex>``.

    The hex suffix prevents two boots in the same second from colliding —
    rare in life, routine in fixtures that drive recovery repeatedly.
    """
    when = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return f"{when.strftime(_FORMAT)}-{uuid.uuid4().hex[:8]}"


def current_boot_id() -> str:
    """This process's id, minted on first call and stable thereafter.

    Locked because the backend arms file logging on the main thread while
    the supervisor's watcher threads are already running; two callers racing
    the first call would otherwise name their files differently.
    """
    global _current
    with _lock:
        if _current is None:
            _current = mint_boot_id()
        return _current


def reset_boot_id_for_tests() -> None:
    """Forget the minted id. Tests only — a process gets one boot."""
    global _current
    with _lock:
        _current = None


__all__ = ["current_boot_id", "mint_boot_id", "reset_boot_id_for_tests"]

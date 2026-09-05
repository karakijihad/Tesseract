"""Where "I approved going past the cap today" is written down.

The approval used to live only in the process that asked for it. That was a
deliberate safety bias, and it cost a day: a breaker stopped trying on a
spending cap, the operator approved continuing minutes later, and nothing
outside that one process could ever learn it. The runtime went on telling
itself the cap was still refusing.

So it is on disk, and it is stamped with the day it belongs to. The day is what
makes this safe to persist: a file written yesterday answers for yesterday and
is ignored today, so an approval cannot leak past midnight however the file is
copied, restored, or left behind by a crash. A reader never has to decide
whether a stale file is still true.

Two readers, one file: the ledger, which decides, and the recovery pass, which
explains a refusal to the operator afterwards. Two writers, too, since the
Mirror and the agent controller share one budget, which is why this appends one
line per approval rather than rewriting a map: two processes approving
different scopes in the same moment would each have read the same map and each
have written back only its own addition, losing one. An approval is also only
ever granted, never withdrawn before midnight, so append-only is the shape the
data already had.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

_FILENAME = "overage-unlocks.jsonl"


def unlocks_path(beside: Path | None = None) -> Path:
    """The file, beside the spend log it belongs with.

    `beside` is the ledger's own `log_path`, so a test pointing the ledger at a
    tmp file gets its approvals there too rather than in the live tree. Callers
    with no ledger in hand pass nothing and resolve the same production path.
    """
    if beside is not None:
        return beside.with_name(_FILENAME)
    from tesseract.paths import home_logs_root

    return home_logs_root() / _FILENAME


def read(today: str, path: Path | None = None) -> dict[str, str]:
    """Which scopes are approved for `today`, and when each was approved.

    A file for any other day reads as nothing approved. So does a missing one,
    an unreadable one and a damaged one: this decides whether to spend money,
    and the safe answer to "I cannot tell" is that nothing was approved.
    """
    target = path or unlocks_path()
    try:
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return {}
    except OSError:
        log.warning(
            "overage approvals at %s could not be read, so today reads as none approved",
            target,
        )
        return {}
    out: dict[str, str] = {}
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            # One torn line does not condemn the rest. A half-written line can
            # only be the last one, since every other was followed by a
            # complete append.
            continue
        if not isinstance(row, dict) or row.get("date") != today:
            continue
        scope = row.get("scope")
        if scope:
            out.setdefault(str(scope), str(row.get("at") or ""))
    return out


def record(scope: str, *, today: str, when: datetime, path: Path | None = None) -> None:
    """Write down one approval for today. Best effort: a disk error must not
    take down the turn the operator just approved."""
    target = path or unlocks_path()
    row = {"date": today, "scope": scope, "at": when.isoformat(timespec="seconds")}
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
    except OSError as exc:
        log.warning("could not write down the overage approval for %s: %s", scope, exc)


def fingerprint(path: Path | None = None) -> tuple[int, int] | None:
    """(mtime, size), so a reader can skip the read when nothing has moved."""
    target = path or unlocks_path()
    try:
        stat = target.stat()
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


__all__ = ["fingerprint", "read", "record", "unlocks_path"]

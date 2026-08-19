"""Durable approval ledger — append-only JSONL of every permission decision.

Audit-4 P1 fix: Mirror's `event_log.py` is an in-memory deque (cap 5000)
and the REPL has no record at all. Once the assistant commands terminals,
subprocesses, and delegated workers, ASK history must survive process
restarts for forensics and ops review.

Lives at ``runtime/logs/approvals.jsonl`` (``runtime_logs_root()``). One JSON object per
line. Single async lock serializes writes so concurrent tool calls
cannot interleave bytes.

Schema (per line)::

    {
      "ts": ISO8601 with timezone,
      "session_id": str,
      "call_id": str,
      "tool": str,
      "input_summary": dict (truncated for large fields),
      "posture_source": "security" | "path_validator" | "path" |
                        "mode" | "default" | "tool" |
                        "workspace_decision" |
                        "tool_tier_promotion" | "pty_delegate" |
                        "channel_mutation" | "installed_tree",
      "result": "allow_once" | "deny" | "timeout" | "cancelled" |
                "resolved" | "deleted" | "auto",
      "actor": "operator" | "timeout" | "system"
    }

``auto`` (2026-08-14) records a call nobody was asked about — an AUTO
posture resolving without a prompt. It is most of the volume and it is the
reason the file is now worth rotating: `memory_search`, `file_read` and
`grep` fire many times a turn. Nothing rotates or reaps it today.

The ``input_summary`` is ``BaseModel.model_dump()`` with any string field
longer than ``_MAX_SUMMARY_FIELD_CHARS`` truncated to that length plus a
``"...<truncated N chars>"`` suffix. Keeps the ledger scannable without
losing the leading bytes that identify the request.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from tesseract.lib.jsonl_rolls import rewrite, row_time
from tesseract.paths import runtime_logs_root

logger = logging.getLogger(__name__)

# threading.Lock, not asyncio.Lock: `_append` runs on worker threads via
# `asyncio.to_thread`, and a thread lock serializes appends across every
# event loop in the process — an import-time asyncio.Lock binds to the
# first loop that awaits it and raises from any other (Deferred 2026-07-12).
_LOCK = threading.Lock()
_MAX_SUMMARY_FIELD_CHARS = 500

PostureSource = Literal[
    "security",
    "path_validator",
    "path",
    "mode",
    "default",
    "tool",
    "workspace_decision",
    "tool_tier_promotion",
    "pty_delegate",
    "channel_mutation",
    "installed_tree",
]
#: `auto` is deliberately its own value rather than a second meaning for
#: `allow_once`, and the distinction is the whole reason the ledger stays
#: usable now that it records uncontested calls too: `allow_once` means a
#: person was asked and said yes, `auto` means nobody was asked at all.
#: Collapsing them would make every "did the operator approve this?" query
#: answer yes for actions no operator ever saw — and would drown the handful
#: of real approvals in thousands of rows. Filter with `result != "auto"` to
#: get back exactly the ledger that existed before AUTO was recorded.
Result = Literal[
    "allow_once", "deny", "timeout", "cancelled", "resolved", "deleted", "auto"
]
Actor = Literal["operator", "timeout", "system"]


def _truncate_field(value: Any) -> Any:
    if isinstance(value, str) and len(value) > _MAX_SUMMARY_FIELD_CHARS:
        return (
            value[:_MAX_SUMMARY_FIELD_CHARS]
            + f"...<truncated {len(value) - _MAX_SUMMARY_FIELD_CHARS} chars>"
        )
    if isinstance(value, dict):
        return {k: _truncate_field(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_truncate_field(v) for v in value]
    return value


def summarize_input(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize a tool input dict for the ledger. Truncates long strings."""
    if not raw:
        return {}
    return {k: _truncate_field(v) for k, v in raw.items()}


def ledger_path() -> Path:
    """Resolve the current ledger path, at call time.

    `runtime_logs_root()` does the resolving — it walks `install_root()`,
    which is `_home_at_call_time().parent`, so an overridden `TESSERACT_HOME`
    is honoured without this function reading the env var itself. It used to
    read it anyway, into a local that nothing then used, which made this look
    like the place the override was applied and hid where it actually is.
    """
    return runtime_logs_root() / "approvals.jsonl"


async def record_ask(
    *,
    session_id: str,
    call_id: str,
    tool_name: str,
    input_summary: dict[str, Any],
    posture_source: PostureSource,
    result: Result,
    actor: Actor,
) -> None:
    """Append one decision row to ``approvals.jsonl``. Best-effort: I/O
    failures are logged but never raised — a broken disk must not block a
    tool decision that's already been made."""
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "session_id": session_id,
        "call_id": call_id,
        "tool": tool_name,
        "input_summary": input_summary,
        "posture_source": posture_source,
        "result": result,
        "actor": actor,
    }
    line = json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
    path = ledger_path()
    try:
        await asyncio.to_thread(_append, path, line)
    except OSError as exc:
        logger.warning("approval_log: append failed for %s: %s", path, exc)


def _append(path: Path, line: str) -> None:
    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)


def roll_older_than(cutoff: datetime) -> int:
    """Move rows older than `cutoff` into `approvals-archive/`, and return how
    many moved. The window is the retention table's; this is the mechanism.

    **It lives here because the lock does.** Read-partition-rewrite from
    anywhere else is a lost-write bug wearing the shape of a safe one: the
    rewrite replaces the file with a list computed from a snapshot, so a row
    appended between the read and the replace is in neither the archive nor
    the live file. `_LOCK` is held across the whole roll, which is why this is
    a function on the module that owns the file rather than a caller reaching
    for a private lock.

    Archive first, then rewrite. The order is the crash guarantee: an
    interruption between them leaves a row in both files, which is
    duplication. The other order loses it.

    A row whose `ts` will not parse is KEPT. Nothing in this file is lost to
    housekeeping, and an unreadable timestamp is not a licence to guess.
    """
    path = ledger_path()
    with _LOCK:
        if not path.is_file():
            return 0
        keep: list[str] = []
        retire: dict[str, list[str]] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            stamped = row_time(line, "ts")
            if stamped is None or stamped >= cutoff:
                keep.append(line)
            else:
                retire.setdefault(stamped.strftime("%Y-%m"), []).append(line)
        if not retire:
            return 0

        archive_dir = path.parent / "approvals-archive"
        archive_dir.mkdir(parents=True, exist_ok=True)
        moved = 0
        for month, rows in sorted(retire.items()):
            with (archive_dir / f"approvals-{month}.jsonl").open(
                "a", encoding="utf-8"
            ) as fh:
                fh.write("\n".join(rows) + "\n")
            moved += len(rows)
        rewrite(path, keep)
        return moved

"""Fold the pre-2026-08 per-connection session files into chat records.

    python -m tesseract.scripts.migrate_legacy_sessions --dry-run
    python -m tesseract.scripts.migrate_legacy_sessions --apply

Until 2026-08 a conversation's identity was the name of the file it was written
to, and that name came from the wall clock at write time. A WebSocket
connection wrote one file, so a single conversation left a new copy behind on
every reconnect — one operator tree carried the same 21-message exchange ten
times. A chat record has a uuid stamped once at creation and is updated in
place, so the copies have no equivalent on the new side and must be folded in
rather than translated one-for-one.

What the run does:

1. Reads every ``sessions/*.json`` and ``sessions/archive/**/*.json``.
2. Collapses the copies. A history that is a PREFIX of a longer one is that
   same conversation caught earlier, so the longest wins — messages carry
   microsecond timestamps and reasoning ids, so an accidental prefix between
   two genuinely different conversations is not a case that arises.
3. Drops what the new side already holds, comparing content rather than names.
4. EXTENDS a record the new side holds only part of. A record truncated by a
   kill mid-write, a `/reset` or a compaction is shorter than the file it came
   from; minting the longer copy beside it would rebuild the duplication this
   replaces, and dropping it would lose turns, so the record keeps its id and
   its creation stamp and gains the rest of its own transcript.
5. Mints a record for the rest: a new ``chat_id``, and every stamp taken from
   the conversation's OWN header, not from the run.

Nothing is deleted. The source files move to ``sessions/_migrated/`` — the
unreadable ones too, so a corrupt file does not leave the directory it was
supposed to empty — and the operator removes that folder when they are
satisfied. A migration that deletes the only copy of a conversation on its
first run is not a migration. Re-running is safe: with the files moved there
is nothing left to read.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dataclasses import replace

from tesseract.mirror.server import chat_index
from tesseract.mirror.server.chat_content import (
    last_message_timestamp,
    sanitize_history_for_persistence,
)
from tesseract.mirror.server.chat_record import (
    ChatRecord,
    chats_dir,
    read_record,
    write_record,
)
from tesseract.paths import home_dir

MIGRATED_DIRNAME = "_migrated"


def sessions_dir() -> Path:
    return home_dir() / "sessions"


def migrated_dir() -> Path:
    return sessions_dir() / MIGRATED_DIRNAME


@dataclass
class LegacyFile:
    path: Path
    name: str
    started_at: str
    ended_at: str
    model: str
    history: list[dict[str, Any]]
    keys: tuple[str, ...]


def _message_key(msg: dict[str, Any]) -> str:
    return json.dumps(msg, sort_keys=True, ensure_ascii=False, default=str)


def _first_timestamp(history: list[dict[str, Any]]) -> str | None:
    for msg in history:
        ts = msg.get("timestamp")
        if isinstance(ts, str) and ts:
            return ts
    return None


def legacy_paths(root: Path) -> list[Path]:
    """Every file the migration is responsible for, readable or not."""
    paths = sorted(root.glob("*.json"))
    archive = root / "archive"
    if archive.is_dir():
        paths += sorted(archive.rglob("*.json"))
    return paths


def collect(root: Path) -> list[LegacyFile]:
    """Every legacy session file under ``root``, parsed and sanitized.

    ``sessions/archive/`` may not exist — nothing has created it since
    archiving became a flag on the record — so its absence is not an error.
    Unreadable files are skipped and reported by the caller as the difference
    between the files seen and the ones parsed.
    """
    paths = legacy_paths(root)
    files: list[LegacyFile] = []
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 — a corrupt file is reported, not fatal
            continue
        if not isinstance(data, dict):
            continue
        history = sanitize_history_for_persistence(data.get("history") or [])
        started = str(
            data.get("started_at")
            or _first_timestamp(history)
            or datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
            .astimezone()
            .isoformat()
        )
        ended = str(data.get("ended_at") or last_message_timestamp(history) or started)
        files.append(LegacyFile(
            path=path,
            name=path.stem,
            started_at=started,
            ended_at=ended,
            model=str(data.get("model") or ""),
            history=history,
            keys=tuple(_message_key(m) for m in history),
        ))
    return files


def collapse(files: list[LegacyFile]) -> list[LegacyFile]:
    """The distinct conversations among the copies, longest transcript first.

    A file whose history is a prefix of one already kept is a snapshot of that
    same conversation taken earlier, and keeping it would resurrect the
    duplication this replaces. Empty files carry no conversation at all.
    """
    ordered = sorted(files, key=lambda f: (-len(f.keys), f.name))
    kept: list[LegacyFile] = []
    for candidate in ordered:
        if not candidate.keys:
            continue
        if any(k.keys[: len(candidate.keys)] == candidate.keys for k in kept):
            continue
        kept.append(candidate)
    return kept


def existing_histories() -> list[tuple[str, tuple[str, ...]]]:
    """(chat_id, message keys) for every chat record already on disk."""
    out: list[tuple[str, tuple[str, ...]]] = []
    directory = chats_dir()
    if not directory.is_dir():
        return out
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        history = sanitize_history_for_persistence(data.get("history") or [])
        out.append((path.stem, tuple(_message_key(m) for m in history)))
    return out


def match(
    candidate: LegacyFile, records: list[tuple[str, tuple[str, ...]]]
) -> tuple[str | None, str]:
    """Where this conversation stands against the records already on disk.

    ``("<chat_id>", "held")`` when a record already contains it, ``("<chat_id>",
    "extend")`` when a record holds a PREFIX of it and is therefore the same
    conversation caught earlier, and ``(None, "mint")`` when nothing on the new
    side has it.

    The second answer is the one that is easy to miss. Checking containment in
    only one direction reads as "not held" and mints a second record beside the
    first — two conversations in the drawer with the same opening turns, which
    is the duplication this migration exists to remove. An empty record matches
    nothing: every history starts with zero messages.
    """
    for chat_id, keys in records:
        if keys[: len(candidate.keys)] == candidate.keys:
            return chat_id, "held"
        if keys and candidate.keys[: len(keys)] == keys:
            return chat_id, "extend"
    return None, "mint"


def mint(candidate: LegacyFile) -> ChatRecord:
    """A chat record for a conversation the new side never held.

    Every stamp is the conversation's OWN, never today's — a migration that
    dated these by its run would repeat the defect it exists to undo, one
    directory along. That is also why the record is written directly rather
    than through ``save_chat``, which stamps ``ended_at`` at the moment of the
    write: a conversation from last week would have looked like today's
    activity to every reader that orders by it.
    """
    return ChatRecord(
        chat_id=uuid.uuid4().hex,
        session_id="",
        title=candidate.name,
        created_at=candidate.started_at,
        started_at=candidate.started_at,
        ended_at=candidate.ended_at,
        model=candidate.model,
        turn_count=_turns(candidate.history),
        history=list(candidate.history),
    )


def extend(chat_id: str, candidate: LegacyFile) -> ChatRecord | None:
    """The record, grown to the longer transcript it is a prefix of.

    Its identity is untouched — same ``chat_id``, same ``created_at``, same
    title and model, because those are the live record's and the legacy file
    only ever knew a filename. What it gains is the turns it was missing, and
    an ``ended_at`` no earlier than it already had.
    """
    record = read_record(chat_id)
    if record is None:
        return None
    return replace(
        record,
        history=list(candidate.history),
        turn_count=_turns(candidate.history),
        ended_at=max(record.ended_at or "", candidate.ended_at) or None,
    )


def _turns(history: list[dict[str, Any]]) -> int:
    return sum(1 for m in history if m.get("role") == "user")


def write(record: ChatRecord) -> None:
    """Persist a record and its index row."""
    path = write_record(record)
    chat_index.upsert(record, path)


def _move_aside(root: Path, path: Path) -> None:
    """Move one source file under ``_migrated/``, keeping its relative path.

    A destination that already exists gets a numbered suffix rather than an
    exception: the folder is the only copy of these conversations until the
    operator deletes it, and a half-finished move is the one outcome worth
    ruling out.
    """
    destination = migrated_dir() / path.relative_to(root)
    destination.parent.mkdir(parents=True, exist_ok=True)
    n = 1
    while destination.exists():
        destination = destination.with_name(f"{path.stem}.{n}{path.suffix}")
        n += 1
    shutil.move(str(path), str(destination))


def run(*, apply: bool) -> int:
    root = sessions_dir()
    if not root.is_dir():
        print(f"no sessions directory at {root} - nothing to migrate")
        return 0

    sources = legacy_paths(root)
    files = collect(root)
    unreadable = [p for p in sources if p not in {f.path for f in files}]
    distinct = collapse(files)
    records = existing_histories()

    to_mint: list[LegacyFile] = []
    to_extend: list[tuple[str, LegacyFile]] = []
    held = 0
    for candidate in distinct:
        chat_id, action = match(candidate, records)
        if action == "held":
            held += 1
        elif action == "extend":
            to_extend.append((chat_id, candidate))
        else:
            to_mint.append(candidate)

    print(f"files read      : {len(files)}")
    print(f"unreadable      : {len(unreadable)}")
    for path in unreadable:
        print(f"  ? {path.name}")
    print(f"conversations   : {len(distinct)}")
    print(f"already on disk : {held}")
    print(f"records to grow : {len(to_extend)}")
    for chat_id, candidate in to_extend:
        print(f"  ^ {chat_id[:8]}  <- {candidate.name}  ({len(candidate.keys)} messages)")
    print(f"records to mint : {len(to_mint)}")
    for candidate in to_mint:
        print(f"  + {candidate.name}  ({len(candidate.keys)} messages, started {candidate.started_at})")

    if not apply:
        print("\ndry run - nothing written. Re-run with --apply.")
        return 0

    minted = 0
    for candidate in to_mint:
        write(mint(candidate))
        minted += 1
    grown = 0
    for chat_id, candidate in to_extend:
        record = extend(chat_id, candidate)
        if record is None:
            print(f"  ! {chat_id[:8]} could not be re-read; {candidate.name} left in place")
            continue
        write(record)
        grown += 1
    moved = 0
    for path in sources:
        if not path.exists():
            continue
        _move_aside(root, path)
        moved += 1

    print(f"\nrecords created : {minted}")
    print(f"records grown   : {grown}")
    print(f"files moved     : {moved} -> {migrated_dir()}")
    print("Delete that folder once you are satisfied; nothing reads it.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tesseract.scripts.migrate_legacy_sessions"
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="report only")
    mode.add_argument("--apply", action="store_true", help="mint records and move the files")
    args = parser.parse_args(argv)
    return run(apply=bool(args.apply))


if __name__ == "__main__":
    sys.exit(main())

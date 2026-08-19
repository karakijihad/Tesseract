"""One record per conversation, amended as the conversation continues.

What a conversation leaves behind is a memory: what was said, when, and which
door it came through. Nothing here reads the source it came from — a
:class:`~tesseract.capture.sources.Conversation` is already the same shape
whichever entry point produced it, so `source` is a field on the record rather
than a fork in the writer.

**One conversation is one file, and the id says so.** The id is derived from
the conversation's key rather than minted at random, so the funnel finds the
record it wrote last time without keeping a second index of what it has
written. A conversation that resumes AMENDS that record; it does not earn a
second one.

**The position decides what to write, not only whether to write.** It used to
decide only the latter: a chat that went quiet, resumed, and went quiet again
was recapped twice, and the second record held every turn of the first because
the writer took the collector's whole tail. Turns 1-10 then 1-20, where it
should have been 1-10 then 11-20. So the position now filters the turns as well
as arming the pass — the first record for a conversation carries the tail it
was found with, and every pass after it carries only what is new.

The position is on disk. The channel sweep this replaces kept its
last-reflected marker in a process dictionary, so every restart re-wrote the
recap it had already written — the guard read as durable and was not.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha1
from pathlib import Path
from typing import Any

from tesseract.capture.sources import Conversation, Turn
from tesseract.memory.types import MemoryFrontmatter, MemoryType

log = logging.getLogger(__name__)

REFLECTION_TAG = "conversation-recap"

# Namespaced so a conversation's position and a stage's own watermark cannot
# collide in the file they share.
WATERMARK_PREFIX = "reflect:"

# How much of one turn reaches the record. The verbatim exchange is still in
# its own store; the recap is what retrieval reads, and one long transcript
# must not crowd out every other memory in the window.
TURN_MAX_CHARS = 280

# Every recap is worth recalling, none is a decision. Matches what the channel
# writer used, and the classifier does not run on a record built from a
# transcript rather than from prose.
REFLECTION_IMPORTANCE = 6

_TRANSCRIPT_HEADING = "## Transcript (oldest → newest)"


class ReflectOutcome(str, Enum):
    WRITTEN = "written"
    AMENDED = "amended"
    UP_TO_DATE = "up_to_date"
    BLOCKED = "blocked"
    NO_STORE = "no_store"


def watermark_key(conv: Conversation) -> str:
    return f"{WATERMARK_PREFIX}{conv.key}"


def recap_id(conv: Conversation) -> str:
    """The one memory id this conversation's recap lives at, forever.

    Derived rather than minted so the funnel can find its own last record with
    a single read and no side index. Same shape as `generate_id` — `mem_` plus
    eight hex — so nothing downstream can tell a derived id from a random one,
    and the collision odds are a random id's.
    """
    return recap_id_for(conv.key)


def recap_id_for(key: str) -> str:
    """`recap_id` for a caller holding the key rather than the conversation.

    The chat store deleting a conversation has its id and nothing else, and
    the derivation must stay in one place — a second copy of this sha1 is a
    second answer to where a recap lives.
    """
    digest = sha1(key.encode("utf-8")).hexdigest()[:8]
    return f"mem_{digest}"


def mark_source_deleted(
    key: str, *, store_dir: Path | None = None, now: datetime | None = None
) -> bool:
    """Record on a conversation's recap that its transcript has been deleted.

    Operator ruling, 2026-08-19: the lesson persists. A memory is meant to
    outlive the conversation that taught it — that is what saving one is for —
    so nothing is pruned here. What the record loses is the way back, and this
    is what says so: `source_deleted_at` is stamped, retrieval renders "source
    deleted", and the fact reads as an old lesson with nothing left to re-read
    rather than as a pointer to a conversation that is simply missing.
    Deleting the memory itself stays the operator's own act.

    Idempotent — an already-stamped record keeps the first stamp, so the field
    dates the deletion rather than the last pass over it. Returns whether a
    record was stamped; a conversation the funnel never recapped has none, and
    that is not a failure.
    """
    from tesseract.memory.store import MemoryStore
    from tesseract.paths import home_dir

    root = store_dir or (home_dir() / "memory-store")
    if not root.is_dir():
        return False
    store = MemoryStore(root)
    memory_id = recap_id_for(key)
    existing = store.read(memory_id, log_access=False)
    if existing is None:
        return False
    frontmatter, body = existing
    if frontmatter.source_deleted_at is not None:
        return False
    stamped = frontmatter.model_copy(
        update={"source_deleted_at": now or datetime.now(timezone.utc)}
    )
    if not store.write(stamped, body, skip_wnts_check=True):
        return False
    log.info("capture: %s outlived %s, which has been deleted", memory_id, key)
    return True


def _clip(text: str) -> str:
    flat = text.replace("\n", " ").strip()
    if len(flat) <= TURN_MAX_CHARS:
        return flat
    return flat[: TURN_MAX_CHARS - 1].rstrip() + "…"


def _line(turn: Turn) -> str:
    return f"- **{turn.role}** @ {turn.at.isoformat()} — {_clip(turn.text)}"


_ROW = re.compile(r"^- \*\*(?P<role>[^*]+)\*\* @ (?P<at>\S+) — ")


def _summarise(lines: list[str]) -> str:
    """The record's own account of itself, read back off its transcript.

    Derived from the rendered rows rather than from the turns that produced
    them, so an amend describes the WHOLE record and not the batch that
    triggered it — the first version of this counted a total across the record
    and a role split across the new turns, and read "20 turns (5 from you / 5
    from the assistant)".
    """
    roles: list[str] = []
    stamps: list[str] = []
    for line in lines:
        match = _ROW.match(line)
        if match is None:
            continue
        roles.append(match.group("role"))
        stamps.append(match.group("at"))
    if not stamps:
        return "no turns"
    said = sum(1 for role in roles if role == "user")
    return (
        f"{len(stamps)} turns ({said} from you / {len(stamps) - said} from the "
        f"assistant) between {stamps[0]} and {stamps[-1]}"
    )


def new_turns_since(conv: Conversation, watermark: datetime | None) -> tuple[Turn, ...]:
    """The turns this pass has not already recorded."""
    if watermark is None:
        return conv.turns
    return tuple(t for t in conv.turns if t.at > watermark)


def reflection_record(
    conv: Conversation, *, now: datetime
) -> tuple[MemoryFrontmatter, str]:
    """The recap for one conversation — frontmatter and body.

    Pure, so the promise this funnel exists to keep is checkable: hand it the
    same turns under two sources and only the source moves.
    """
    heading = f"Conversation recap — {conv.source}:{conv.conversation_id}"
    rows = [_line(turn) for turn in conv.turns]
    body = "\n".join([f"# {heading}", "", _TRANSCRIPT_HEADING, "", *rows]) + "\n"

    frontmatter = MemoryFrontmatter(
        id=recap_id(conv),
        type=MemoryType.PROJECT,
        title=f"{heading} ({conv.turns[0].at.date().isoformat()})",
        summary=_summarise(rows),
        created_at=now,
        importance=REFLECTION_IMPORTANCE,
        # `chat:<id>` is what the channel recall path filters on, so it stays
        # the scoping tag for every source rather than becoming a second one.
        tags=[REFLECTION_TAG, f"source:{conv.source}", f"chat:{conv.conversation_id}"],
        source_type=conv.source,
    )
    return frontmatter, body


def amended_record(
    existing: tuple[MemoryFrontmatter, str],
    fresh: tuple[Turn, ...],
    *,
    now: datetime,
) -> tuple[MemoryFrontmatter, str]:
    """The existing record with the new turns appended.

    The old turns are read back off the RECORD, never re-read from the source —
    which is the whole point: the source only has to hand over what is new, and
    a conversation older than any tail keeps the history it has already earned.

    Counting is by transcript line rather than by re-parsing them, so a record
    written in an older body format still totals correctly.
    """
    frontmatter, body = existing
    added = [_line(turn) for turn in fresh]
    rows = [line for line in body.splitlines() if _ROW.match(line)] + added
    appended = body.rstrip("\n") + "\n" + "\n".join(added) + "\n"
    return (
        frontmatter.model_copy(
            update={"updated_at": now, "summary": _summarise(rows)}
        ),
        appended,
    )


async def reflect(
    conv: Conversation,
    *,
    bundle: Any,
    watermarks: Any,
    now: datetime,
) -> ReflectOutcome:
    """Record what this conversation has said that is not recorded yet.

    Creates the conversation's one record on the first pass and appends to it
    on every pass after. A policy refusal advances the position anyway: the
    admission policy's answer to these turns will not change in five minutes,
    and re-offering them every tick is a loop rather than a retry.
    """
    store = getattr(bundle, "store", None)
    if store is None:
        return ReflectOutcome.NO_STORE

    key = watermark_key(conv)
    seen = watermarks.get(key)
    fresh = new_turns_since(conv, seen)
    if not fresh:
        return ReflectOutcome.UP_TO_DATE

    existing = store.read(recap_id(conv), log_access=False)
    if existing is None:
        # First pass: the record carries the tail the conversation was found
        # with, so a chat that predates the funnel is not reduced to whatever
        # happened since it started running.
        frontmatter, body = reflection_record(conv, now=now)
        outcome = ReflectOutcome.WRITTEN
    else:
        frontmatter, body = amended_record(existing, fresh, now=now)
        outcome = ReflectOutcome.AMENDED

    written = bool(store.write(frontmatter, body))
    watermarks.set(key, conv.last_turn_at)
    if not written:
        log.info("capture: the admission policy declined the recap for %s", conv.key)
        return ReflectOutcome.BLOCKED

    # Best-effort, like every other memory writer: a missing Ollama must not
    # cost the record itself.
    index = getattr(bundle, "index", None)
    if index is not None:
        try:
            index.add_or_update(frontmatter)
        except Exception:
            log.debug("capture: index update skipped for %s", conv.key, exc_info=True)
    embeddings = getattr(bundle, "embeddings", None)
    if embeddings is not None:
        try:
            await embeddings.embed_and_add(frontmatter, body)
        except Exception:
            log.debug("capture: embedding skipped for %s", conv.key, exc_info=True)

    log.info(
        "capture: %s the recap for %s (%d new turn(s), mem=%s)",
        outcome.value,
        conv.key,
        len(fresh),
        frontmatter.id,
    )
    return outcome


__all__ = [
    "REFLECTION_IMPORTANCE",
    "REFLECTION_TAG",
    "ReflectOutcome",
    "TURN_MAX_CHARS",
    "WATERMARK_PREFIX",
    "amended_record",
    "mark_source_deleted",
    "new_turns_since",
    "recap_id",
    "recap_id_for",
    "reflect",
    "reflection_record",
    "watermark_key",
]

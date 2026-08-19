"""Chunkers + indexer entry points for session/workshop content.

CR-1 (2026-05-22). Pure-function chunkers are unit-testable in
isolation; the indexer entry points (``index_session_file``,
``index_workshop_file``) wire chunks into a :class:`WorkIndex` and
handle stat / re-ingest.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from tesseract.memory.work_index import WorkChunk, WorkIndex

logger = logging.getLogger(__name__)


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind in ("text", "input_text", "output_text"):
            text = item.get("text") or item.get("content") or ""
            if isinstance(text, str) and text:
                parts.append(text)
        elif kind == "image":
            parts.append(f"[image: {item.get('filename', 'unknown')}]")
        elif kind == "file":
            parts.append(f"[file: {item.get('filename', 'unknown')}]")
    return " ".join(parts).strip()


def chunk_session_history(
    history: list[dict[str, Any]],
    *,
    include_tool: bool = False,
) -> Iterable[dict[str, Any]]:
    """Yield one chunk per non-empty message.

    Tool messages are skipped by default — they tend to be voluminous
    JSON results that dilute recall. Pass ``include_tool=True`` to
    keep them (useful for code-recall workflows).
    """
    for idx, msg in enumerate(history):
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "tool" and not include_tool:
            continue
        text = _content_to_text(msg.get("content"))
        if not text or not text.strip():
            continue
        ts = msg.get("timestamp") or ""
        yield {
            "turn_idx": idx,
            "role": str(role) if role else "",
            "text": text,
            "ts": str(ts) if ts else "",
        }


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)


def chunk_workshop_markdown(markdown: str) -> Iterable[dict[str, Any]]:
    """Yield one chunk per markdown section, split at H1-H6 boundaries.

    Headings introduce a new chunk; intro text (anything before the
    first heading) lands as the leading chunk. Files with no headings
    yield a single chunk containing the whole body.
    """
    text = (markdown or "").strip()
    if not text:
        return
    matches = list(_HEADING_RE.finditer(text))
    if not matches:
        yield {"chunk_idx": 0, "text": text}
        return
    # Leading intro (before the first heading).
    if matches[0].start() > 0:
        intro = text[:matches[0].start()].strip()
        if intro:
            yield {"chunk_idx": 0, "text": intro}
    chunk_idx = 1 if matches[0].start() > 0 else 0
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[m.start():end].strip()
        if body:
            yield {"chunk_idx": chunk_idx, "text": body}
            chunk_idx += 1


def index_session_file(index: WorkIndex, path: Path, *, include_tool: bool = False) -> int:
    """Ingest a single session JSON file. Returns the chunk count.

    Idempotent — deletes prior chunks for the same ``source_path`` first.
    """
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("work_index: failed to read session %s: %s", path, exc)
        return 0
    history = data.get("history") if isinstance(data, dict) else None
    if not isinstance(history, list):
        return 0
    started = data.get("started_at") or data.get("ended_at") or ""
    session_id = path.stem
    index.delete_by_path(str(path))
    n = 0
    for chunk in chunk_session_history(history, include_tool=include_tool):
        # ``turn_idx`` keeps the position in the original history array
        # (sparse when role=tool messages are skipped). ``chunk_idx`` is
        # the 0-based dense ordinal across emitted chunks — what the
        # WorkChunk docstring promises and what range-based consumers
        # need.
        index.add(WorkChunk(
            source="session",
            source_path=str(path),
            source_ref=session_id,
            turn_idx=int(chunk["turn_idx"]),
            role=chunk["role"] or None,
            chunk_idx=n,
            ts=chunk["ts"] or str(started),
            text=chunk["text"],
        ))
        n += 1
    return n


def index_workshop_file(index: WorkIndex, path: Path) -> int:
    """Ingest a single workshop markdown file. Returns the chunk count.

    Idempotent. ``source_ref`` is the path's parent directory name
    (e.g. ``"entity-autonomy-plan"``) — the workshop convention is one
    folder per artifact.
    """
    try:
        markdown = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("work_index: failed to read workshop %s: %s", path, exc)
        return 0
    parent_slug = path.parent.name or path.stem
    ts = _iso_from_mtime(path)
    index.delete_by_path(str(path))
    n = 0
    for chunk in chunk_workshop_markdown(markdown):
        index.add(WorkChunk(
            source="workshop",
            source_path=str(path),
            source_ref=parent_slug,
            turn_idx=None,
            role=None,
            chunk_idx=int(chunk["chunk_idx"]),
            ts=ts,
            text=chunk["text"],
        ))
        n += 1
    return n


def _iso_from_mtime(path: Path) -> str:
    try:
        ts = path.stat().st_mtime
    except OSError:
        return ""
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def backfill(
    index: WorkIndex,
    *,
    chat_files: Iterable[Path] | None = None,
    workshop_dir: Path | None = None,
) -> dict[str, int]:
    """Walk both corpora end-to-end. Returns ``{"chats": N, "workshop": M}``.
    Idempotent — repeated calls produce the same row count (per-path delete
    on each ingest).

    The conversations arrive as paths rather than as a directory to glob. They
    live in ``sessions/chats/`` now and ``chat_store`` owns that walk — this
    used to take the legacy ``sessions/`` directory and glob it non-
    recursively, which meant it never saw a chat conversation at all.
    """
    out = {"chats": 0, "workshop": 0}
    for path in chat_files or ():
        out["chats"] += index_session_file(index, path)
    if workshop_dir and workshop_dir.exists():
        for path in sorted(workshop_dir.rglob("*.md")):
            out["workshop"] += index_workshop_file(index, path)
    return out

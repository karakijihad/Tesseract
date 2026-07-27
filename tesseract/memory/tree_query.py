"""AU-16 S2 — tree-scoped query surface for ``memory_search``.

Default ``memory_search`` behaviour is unchanged: omitting ``scope``
routes through the existing :class:`RetrievalPipeline`. Passing
``scope`` short-circuits the pipeline and returns markdown-ready
slices of the appropriate tree.

The supported scopes:

- ``"source"`` — returns the matching source tree (filtered by
  ``source`` slug substring on the query; or all source trees when
  the query is empty).
- ``"topic"`` — returns the topic tree for ``entity``, or every
  active topic file when ``entity`` is omitted.
- ``"global"`` — returns the daily digest for the most-recent date
  ≤``since`` (or today's digest when ``since`` is omitted).

The pipeline returns plain markdown — each result is a
``TreeQueryHit`` carrying the path + body + a brief headline. The
``memory_search`` tool wraps them into ``ToolResult`` output.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from tesseract.memory.trees.global_tree import (
    daily_digest_path,
    list_digest_dates,
    read_daily_digest,
)
from tesseract.memory.trees.source_tree import (
    list_source_tree_paths,
    read_source_tree,
    source_tree_path,
)
from tesseract.memory.trees.topic_tree import (
    TOPIC_TREES_ROOT,
    is_topic_active,
    list_active_topics,
    topic_tree_path,
)

log = logging.getLogger(__name__)


SUPPORTED_SCOPES: frozenset[str] = frozenset({"source", "topic", "global"})


@dataclass(frozen=True)
class TreeQueryHit:
    scope: str
    title: str
    path: Path
    body: str


def _filter_by_since(body: str, *, since: datetime) -> str:
    """Return the markdown sections whose ISO timestamp is ≥ ``since``.

    Sections are delimited by ``## Seal `` (source / topic trees). The
    banner block is always preserved. Empty result is the banner alone.
    """
    if not body:
        return body
    parts = body.split("\n## Seal ", 1)
    header = parts[0]
    if len(parts) == 1:
        return body
    rest = "## Seal " + parts[1]
    kept: list[str] = []
    for chunk in re.split(r"(?=^## Seal seal_)", rest, flags=re.M):
        if not chunk.strip():
            continue
        m = re.search(r"## Seal seal_[a-f0-9]+ — (\S+)", chunk)
        if m:
            try:
                ts = datetime.fromisoformat(m.group(1))
                if ts.astimezone(timezone.utc) < since.astimezone(timezone.utc):
                    continue
            except ValueError:
                pass
        kept.append(chunk.rstrip())
    if not kept:
        return header.rstrip() + "\n"
    return header.rstrip() + "\n\n" + "\n\n".join(kept) + "\n"


def query(
    *,
    scope: str,
    query_text: str = "",
    entity: str | None = None,
    source_slug: str | None = None,
    since: datetime | None = None,
) -> list[TreeQueryHit]:
    """Tree-scoped read. Returns at most one hit per matching file."""
    if scope not in SUPPORTED_SCOPES:
        raise ValueError(
            f"unsupported scope {scope!r}; expected one of {sorted(SUPPORTED_SCOPES)}"
        )
    if scope == "source":
        return _query_source(
            query_text=query_text, source_slug=source_slug, since=since
        )
    if scope == "topic":
        return _query_topic(entity=entity, since=since)
    return _query_global(since=since)


def _query_source(
    *, query_text: str, source_slug: str | None, since: datetime | None
) -> list[TreeQueryHit]:
    if source_slug:
        path = source_tree_path(source_slug)
        if not path.exists():
            return []
        body = path.read_text(encoding="utf-8")
        if since is not None:
            body = _filter_by_since(body, since=since)
        return [
            TreeQueryHit(
                scope="source",
                title=f"source/{source_slug}",
                path=path,
                body=body,
            )
        ]
    needle = query_text.strip().lower()
    out: list[TreeQueryHit] = []
    for path in list_source_tree_paths():
        if needle and needle not in path.stem.lower():
            continue
        body = path.read_text(encoding="utf-8")
        if since is not None:
            body = _filter_by_since(body, since=since)
        out.append(
            TreeQueryHit(
                scope="source",
                title=f"source/{path.stem}",
                path=path,
                body=body,
            )
        )
    return out


def _query_topic(
    *, entity: str | None, since: datetime | None
) -> list[TreeQueryHit]:
    if entity:
        if not is_topic_active(entity):
            return []
        path = topic_tree_path(entity)
        body = path.read_text(encoding="utf-8")
        if since is not None:
            body = _filter_by_since(body, since=since)
        return [
            TreeQueryHit(
                scope="topic",
                title=f"topic/{entity}",
                path=path,
                body=body,
            )
        ]
    out: list[TreeQueryHit] = []
    for slug in list_active_topics():
        path = TOPIC_TREES_ROOT() / f"{slug}.md"
        body = path.read_text(encoding="utf-8")
        if since is not None:
            body = _filter_by_since(body, since=since)
        out.append(
            TreeQueryHit(
                scope="topic",
                title=f"topic/{slug}",
                path=path,
                body=body,
            )
        )
    return out


def _query_global(*, since: datetime | None) -> list[TreeQueryHit]:
    dates = list_digest_dates()
    if not dates:
        return []
    if since is None:
        target = dates[0]
    else:
        cutoff = since.astimezone(timezone.utc).date()
        candidates = [d for d in dates if d <= cutoff]
        if not candidates:
            return []
        target = candidates[0]
    body = read_daily_digest(target)
    if body is None:
        return []
    return [
        TreeQueryHit(
            scope="global",
            title=f"global/{target.isoformat()}",
            path=daily_digest_path(target),
            body=body,
        )
    ]


__all__ = ["SUPPORTED_SCOPES", "TreeQueryHit", "query"]

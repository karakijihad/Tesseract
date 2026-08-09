"""AU-16 S2 — entity-keyed topic tree.

A topic file at ``<TESSERACT_HOME>/memory-store/trees/topic/<entity-slug>.md``
is created lazily when an entity name (drawn from a leaf's
``[[wikilink]]`` entities) crosses ``TOPIC_ACTIVATION_THRESHOLD``
occurrences across the lifetime seal stream.

Once active, every fresh seal whose leaves carry that entity appends
to the topic file (newest-first), so the operator gets one
chronological view per significant topic without manual curation.

Topic-tree files mirror the source-tree shape — markdown, no
frontmatter — so they're operator-readable in any editor.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path

from tesseract.memory.leaf_seals import Seal
from tesseract.memory.leaves import _resolve_home

# `append_seal` is read-check-replace: it reads the topic file, checks the
# seal id is absent, then `os.replace`s a rebuilt body. That was safe only
# while every caller ran to completion without yielding. `TopicRouteJob`
# now runs its pass under `asyncio.to_thread`, and the scheduler has no
# per-job running guard — `run_now` awaits `_run_job` directly and the
# command handler spawns each request independently — so two passes can
# read the same body and the later `os.replace` drops the earlier one's
# section. Callers hold this across a whole pass, not per-append, so a
# run's sections land together or not at all.
TOPIC_TREE_LOCK = threading.RLock()

log = logging.getLogger(__name__)


TOPIC_ACTIVATION_THRESHOLD = 3
_ENTITY_SLUG_RE = re.compile(r"[^a-z0-9._-]+")


def TOPIC_TREES_ROOT() -> Path:
    return _resolve_home() / "memory-store" / "trees" / "topic"


def entity_slug(entity: str) -> str:
    norm = _ENTITY_SLUG_RE.sub("-", entity.strip().lower())
    norm = norm.strip("-._") or "unknown"
    return norm[:80]


def topic_tree_path(entity: str) -> Path:
    return TOPIC_TREES_ROOT() / f"{entity_slug(entity)}.md"


def is_topic_active(entity: str) -> bool:
    return topic_tree_path(entity).exists()


def list_active_topics() -> list[str]:
    """Return the entity slugs whose topic files exist on disk."""
    root = TOPIC_TREES_ROOT()
    if not root.exists():
        return []
    return sorted(p.stem for p in root.glob("*.md"))


def activate_topic(entity: str) -> Path:
    """Create an empty topic file with the AU-16 frontmatter banner.
    Idempotent — returns the existing path if already active."""
    target = topic_tree_path(entity)
    if target.exists():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    banner = (
        "---\n"
        "kind: topic-summary\n"
        "state: sealed\n"
        "parent_tree: topic\n"
        f"entity: {entity}\n"
        "tags:\n  - topic-summary\n  - sealed\n"
        "---\n\n"
        f"# topic: {entity}\n"
        f"\n_Activated {datetime.now(timezone.utc).isoformat()} — newest seal first._\n\n"
    )
    target.write_text(banner, encoding="utf-8")
    return target


_SECTION_HEADER_RE = re.compile(r"^## Seal (?P<seal_id>seal_[a-f0-9]+) — ", re.M)


def append_seal(
    entity: str, seal: Seal, *, leaf_entities: dict[str, list[str]]
) -> bool:
    """Append a seal section to the topic file for ``entity``.

    Returns ``True`` when a new section was written, ``False`` when the
    seal id was already present (idempotent skip). ``leaf_entities``
    maps each ``leaf_id`` → the entities pulled from the leaf body so
    the section can show which leaves contributed.
    """
    target = activate_topic(entity)
    existing = target.read_text(encoding="utf-8")
    if any(m.group("seal_id") == seal.seal_id for m in _SECTION_HEADER_RE.finditer(existing)):
        return False

    lines: list[str] = []
    lines.append(f"## Seal {seal.seal_id} — {seal.sealed_at.astimezone(timezone.utc).isoformat()}")
    lines.append("")
    lines.append(f"Source: {seal.source_slug} · Leaves: {seal.leaf_count}")
    contributing = [lid for lid, ents in leaf_entities.items() if entity in ents]
    if contributing:
        lines.append("Contributing leaves:")
        for lid in contributing:
            lines.append(f"- {lid}")
    lines.append("")
    section = "\n".join(lines).rstrip() + "\n"

    # Splice newest section just after the banner block.
    split = existing.split("\n## Seal ", 1)
    header = split[0].rstrip() + "\n\n"
    rest = "## Seal " + split[1] if len(split) == 2 else ""
    new_body = header + section + ("\n" + rest if rest else "")

    tmp = target.with_name(f"{target.stem}.{os.getpid()}.{secrets.token_hex(3)}.tmp")
    tmp.write_text(new_body, encoding="utf-8")
    os.replace(tmp, target)
    return True


__all__ = [
    "TOPIC_ACTIVATION_THRESHOLD",
    "TOPIC_TREES_ROOT",
    "TOPIC_TREE_LOCK",
    "activate_topic",
    "append_seal",
    "entity_slug",
    "is_topic_active",
    "list_active_topics",
    "topic_tree_path",
]

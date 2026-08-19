"""Reading memory and the vault into nodes and edges. Deterministic, no model.

Everything here transcribes a relationship that is already written down, and
records where it read it. Nothing infers a new connection — an inferred edge
needs a budget and a prompt version, and neither exists yet.

Two rules the transcription keeps, both from the invariants:

* **Similarity is not a link.** `auto_links` are embedding neighbours, so they
  become `similar_to` edges rather than `links_to` ones. A caller filtering
  them out should not have to know how they were made.
* **Nothing invents a number.** An `auto_links` list keeps the winning ids and
  throws the cosine away, so those edges carry no confidence at all rather
  than a plausible one.
"""

from __future__ import annotations

import logging
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterable

from tesseract.orchestrator.atlas.config import ReviewWindows
from tesseract.orchestrator.atlas.model import (
    Conflict,
    Creator,
    Edge,
    Node,
    NodeKind,
    Provenance,
    entity_id,
)
from tesseract.orchestrator.atlas.store import Atlas

log = logging.getLogger(__name__)

# Hashing every raw byte on every pass is the one expensive thing in here, so
# a file over this size is identified by its stamp alone unless the stamp
# moved. Below it, hashing is cheaper than the stat games that would avoid it.
MAX_HASH_BYTES = 32 * 1024 * 1024


def stamp(path: Path) -> str:
    """`<mtime_ns>:<size>` — the cheap question "did this file change?"."""
    try:
        info = path.stat()
    except OSError:
        return ""
    return f"{info.st_mtime_ns}:{info.st_size}"


def content_hash(path: Path) -> str:
    try:
        if path.stat().st_size > MAX_HASH_BYTES:
            return ""
        return sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _window(windows: ReviewWindows, provenance: Provenance) -> int | None:
    days = getattr(windows, provenance.value)
    return int(days) or None


class _Emitter:
    """Holds the per-build constants so a call site names only what varies."""

    def __init__(
        self,
        atlas: Atlas,
        *,
        now: datetime,
        version: int,
        windows: ReviewWindows,
    ) -> None:
        self.atlas = atlas
        self.now = now
        self.version = version
        self.windows = windows

    def node(
        self,
        node_id: str,
        kind: NodeKind,
        title: str,
        locator: str,
        *,
        content: str = "",
        input_stamp: str = "",
        aliases: Iterable[str] = (),
    ) -> Node:
        return self.atlas.add_node(Node(
            id=node_id,
            kind=kind,
            title=title[:200],
            locator=locator,
            builder_version=self.version,
            first_seen=self.now,
            updated_at=self.now,
            content_hash=content,
            input_stamp=input_stamp,
            aliases=tuple(aliases),
        ))

    def edge(
        self,
        subject: str,
        obj: str,
        edge_type: str,
        *,
        provenance: Provenance,
        creator: Creator,
        locator: str,
        asserted_by: str = "",
        confidence: float | None = None,
    ) -> Edge:
        return self.atlas.add_edge(Edge(
            subject=subject,
            object=obj,
            type=edge_type,
            provenance=provenance,
            creator=creator,
            locator=locator,
            asserted_at=self.now,
            builder_version=self.version,
            confidence=confidence,
            review_after_days=_window(self.windows, provenance),
            asserted_by=asserted_by,
        ))

    def entities(
        self,
        subject: str,
        names: Iterable[Any],
        *,
        field_name: str,
        locator_base: str,
        provenance: Provenance,
        creator: Creator,
        asserted_by: str = "",
    ) -> int:
        made = 0
        for index, raw in enumerate(names or ()):
            name = str(raw).strip()
            if not name:
                continue
            node_id = entity_id(name)
            if node_id == "entity:":
                continue
            # The literal string is the alias. Two spellings are two nodes;
            # joining them is not the builder's call (invariant 2).
            self.node(node_id, NodeKind.ENTITY, name, locator_base, aliases=(name,))
            self.edge(
                subject,
                node_id,
                "mentions",
                provenance=provenance,
                creator=creator,
                locator=f"{locator_base}#{field_name}[{index}]",
                asserted_by=asserted_by,
            )
            made += 1
        return made


def find_duplicate_slugs(store: Any, *, now: datetime, version: int) -> list[Conflict]:
    """Two memories claiming one slug — a contradiction the store's own rule
    forbids, so finding one means something wrote around `memory_save`.

    Recorded rather than resolved. Both memories stand until the operator
    picks, because the atlas is derived and picking would make it primary.
    """
    by_slug: dict[str, list[Any]] = {}
    for fm in store.list_all():
        if fm.slug:
            by_slug.setdefault(fm.slug, []).append(fm)
    out: list[Conflict] = []
    for slug, group in sorted(by_slug.items()):
        if len(group) < 2:
            continue
        ids = tuple(sorted(f"mem:{fm.id}" for fm in group))
        out.append(Conflict(
            kind="duplicate_slug",
            subjects=ids,
            detail=(
                f"{len(group)} memories claim the slug {slug!r}, which is the "
                "store's exact-match key and must name one: "
                + "; ".join(f"{fm.id} ({fm.title})" for fm in group)
            ),
            locators=tuple(f"{fm.id}#slug" for fm in group),
            detected_at=now,
            builder_version=version,
        ))
    return out


def build_memory(
    atlas: Atlas,
    store: Any,
    *,
    now: datetime,
    version: int,
    windows: ReviewWindows,
) -> int:
    """Every memory as a node, and every relationship its frontmatter states.

    `store` is a `MemoryStore`; typed loosely so this module does not drag the
    memory layer into every importer of the atlas.
    """
    emit = _Emitter(atlas, now=now, version=version, windows=windows)
    seen = 0
    for fm in store.list_all():
        path = store.find_file(fm.id)
        if path is None:
            continue
        rel = _relative(path, store.store_dir)
        node_id = f"mem:{fm.id}"
        emit.node(
            node_id,
            NodeKind.MEMORY,
            fm.title or fm.id,
            rel,
            input_stamp=stamp(path),
            aliases=(fm.slug,) if fm.slug else (),
        )
        seen += 1

        for index, target in enumerate(fm.links or ()):
            if not str(target).startswith("mem_"):
                continue
            emit.edge(
                node_id,
                f"mem:{target}",
                "links_to",
                provenance=Provenance.STATED_IN_SOURCE,
                creator=Creator.RULE,
                locator=f"{rel}#links[{index}]",
            )
        for index, target in enumerate(fm.auto_links or ()):
            if not str(target).startswith("mem_"):
                continue
            emit.edge(
                node_id,
                f"mem:{target}",
                "similar_to",
                provenance=Provenance.INFERRED_BY_MODEL,
                creator=Creator.MODEL,
                locator=f"{rel}#auto_links[{index}]",
                asserted_by="auto_linker",
            )
        emit.entities(
            node_id,
            fm.entities,
            field_name="entities",
            locator_base=rel,
            provenance=Provenance.STATED_IN_SOURCE,
            creator=Creator.RULE,
        )
        source_path = fm.source_path.strip()
        if source_path:
            # The node as well as the edge. An edge whose object is not in the
            # graph is a dangling reference, and this one need not be: the
            # path names a file. Hashed only when it resolves inside the store
            # — a `source_path` can point at the operator's own tree, and the
            # atlas reads what it is given rather than going looking.
            inside = store.store_dir / source_path
            emit.node(
                f"source:{source_path}",
                NodeKind.RAW_SOURCE,
                Path(source_path).name,
                source_path,
                content=content_hash(inside) if inside.is_file() else "",
                input_stamp=stamp(inside) if inside.is_file() else "",
            )
            emit.edge(
                node_id,
                f"source:{source_path}",
                "derived_from",
                provenance=Provenance.STATED_IN_SOURCE,
                creator=Creator.RULE,
                locator=f"{rel}#source_path",
            )
    return seen


def build_vault(
    atlas: Atlas,
    manager: Any,
    *,
    now: datetime,
    version: int,
    windows: ReviewWindows,
    reuse: dict[str, tuple[str, str]] | None = None,
) -> int:
    """Every wiki page as a node, its raw source as another, and the
    relationships the ingest pass wrote between them.

    The wiki's relationships came out of a model — `related_slugs`, `entities`
    and `concepts` are all `vault_librarian` output — so they arrive as
    `inferred_by_model` however confidently the page states them.
    """
    emit = _Emitter(atlas, now=now, version=version, windows=windows)
    seen = 0
    for slug in manager.list_wiki_slugs():
        try:
            fm = manager.read_wiki_page_frontmatter(slug) or {}
        except OSError:
            log.warning("atlas: wiki page %s unreadable", slug)
            continue
        page_path = manager.wiki_dir / f"{slug}.md"
        rel = f"wiki/{slug}.md"
        node_id = f"vault:{slug}"
        aliases = tuple(str(a) for a in (fm.get("aliases") or ()) if str(a).strip())
        emit.node(
            node_id,
            NodeKind.WIKI_PAGE,
            str(fm.get("title") or slug),
            rel,
            input_stamp=stamp(page_path),
            aliases=aliases,
        )
        seen += 1

        # The page states its own other names, so the join between "the thing
        # memories call claude-cli" and this page is a stated claim rather
        # than a guess — which is the only kind of `same_as` this builder
        # writes. A near-duplicate that nothing declares stays two nodes.
        for index, alias in enumerate(aliases):
            alias_id = entity_id(alias)
            if alias_id in ("entity:", f"entity:{slug}"):
                continue
            emit.node(alias_id, NodeKind.ENTITY, alias, rel, aliases=(alias,))
            emit.edge(
                alias_id,
                node_id,
                "same_as",
                provenance=Provenance.STATED_IN_SOURCE,
                creator=Creator.RULE,
                locator=f"{rel}#aliases[{index}]",
            )

        for index, target in enumerate(fm.get("related_slugs") or ()):
            if not str(target).strip():
                continue
            emit.edge(
                node_id,
                f"vault:{target}",
                "related_to",
                provenance=Provenance.INFERRED_BY_MODEL,
                creator=Creator.MODEL,
                locator=f"{rel}#related_slugs[{index}]",
                asserted_by="vault_librarian",
            )
        # Recorded on the page that IS mentioned, so the edge is emitted in
        # the direction the claim actually runs.
        for index, origin in enumerate(fm.get("backlinks_from") or ()):
            if not str(origin).strip():
                continue
            emit.edge(
                f"vault:{origin}",
                node_id,
                "mentions",
                provenance=Provenance.INFERRED_BY_MODEL,
                creator=Creator.RULE,
                locator=f"{rel}#backlinks_from[{index}]",
                asserted_by="vault_librarian",
            )
        for field_name in ("entities", "concepts"):
            emit.entities(
                node_id,
                fm.get(field_name) or (),
                field_name=field_name,
                locator_base=rel,
                provenance=Provenance.INFERRED_BY_MODEL,
                creator=Creator.MODEL,
                asserted_by="vault_librarian",
            )

        source_path = str(fm.get("source_path") or "").strip()
        if source_path:
            source_id = f"source:{source_path}"
            raw = manager.root / source_path
            raw_stamp = stamp(raw)
            # The page records the hash it compiled against; the file's own
            # hash is what it is NOW. Recording the file's own means a source
            # edited after ingest reads as changed, which is the fact worth
            # having.
            #
            # Re-hashing an unchanged file on every pass is the one avoidable
            # cost in this module, so a prior hash is reused when the stamp
            # has not moved AND the prior node came from this builder.
            cached = (reuse or {}).get(source_id)
            if cached and raw_stamp and cached[0] == raw_stamp:
                digest = cached[1]
            else:
                digest = content_hash(raw) if raw.exists() else ""
            emit.node(
                source_id,
                NodeKind.RAW_SOURCE,
                Path(source_path).name,
                source_path,
                content=digest,
                input_stamp=raw_stamp,
            )
            emit.edge(
                node_id,
                source_id,
                "derived_from",
                provenance=Provenance.STATED_IN_SOURCE,
                creator=Creator.RULE,
                locator=f"{rel}#source_path",
            )
    return seen


def _relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


__all__ = [
    "MAX_HASH_BYTES",
    "build_memory",
    "build_vault",
    "content_hash",
    "find_duplicate_slugs",
    "stamp",
]

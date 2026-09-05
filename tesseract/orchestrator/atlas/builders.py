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
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path, PurePosixPath
from collections.abc import Callable
from typing import Any, Iterable

from tesseract.orchestrator.atlas import locate
from tesseract.orchestrator.atlas.config import ReviewWindows
from tesseract.orchestrator.atlas.model import (
    Conflict,
    Creator,
    Edge,
    Node,
    NodeKind,
    Provenance,
    entity_id,
    shorten,
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


class Emitter:
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
            # The one place a name is shortened, so every builder gets the
            # ceiling without remembering it and `Node` can refuse a long one
            # outright. See `model.MAX_TITLE_CHARS` for why 80.
            title=shorten(title),
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
    ledger: Any = None,
) -> int:
    """Every memory as a node, and every relationship its frontmatter states.

    `store` is a `MemoryStore`; typed loosely so this module does not drag the
    memory layer into every importer of the atlas.

    `ledger` is a `tags.Ledger` collecting what each record is filed under.
    Collected here and decided elsewhere, because which labels earn a node
    depends on the whole distribution and this sees one record at a time.
    Optional, so a caller that only wants the memory half does not have to
    know about tags at all.
    """
    emit = Emitter(atlas, now=now, version=version, windows=windows)
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
        if ledger is not None:
            ledger.add(node_id, fm.tags, rel)

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
            # path names a file.
            #
            # Where that file IS comes from `locate.source_file`, not from a
            # join here. This joined to the memory store and `build_vault`
            # joined to the vault, and three producers write `source_path`
            # against three different roots: between them the two builders
            # resolved NONE of the 26 raw sources on the machine this was
            # measured on, so every one carried an empty content hash and
            # invariant 7 was quietly not being kept for this kind.
            found = locate.source_file(source_path)
            emit.node(
                f"source:{source_path}",
                NodeKind.RAW_SOURCE,
                locate.named_file(source_path),
                source_path,
                content=content_hash(found) if found else "",
                input_stamp=stamp(found) if found else "",
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
    emit = Emitter(atlas, now=now, version=version, windows=windows)
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
            # `locate.source_file` and not `manager.root / source_path`: the
            # librarian writes this field relative to the vault, the promotion
            # pass relative to the memory store and with a `#section` on the
            # end, and the daily and workshop passes relative to home. One
            # function tries the declared roots, so the hash taken here and the
            # file the map opens are the same file.
            raw = locate.source_file(source_path)
            raw_stamp = stamp(raw) if raw else ""
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
                digest = content_hash(raw) if raw else ""
            emit.node(
                source_id,
                NodeKind.RAW_SOURCE,
                locate.named_file(source_path),
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


_FEEDBACK_LOCATOR = "autonomy/watchman/evidence"


def feedback_node_id(path: Path) -> str:
    """`feedback:<file name>` — identity adopted, never invented.

    The watchman already names a filed defect for the moment it was observed,
    what it was about and a fingerprint of its summary, and that name is unique
    within the tree. The absolute path is not usable as an id: it carries the
    machine that wrote it, so the same record copied to another install would
    become a second node.
    """
    return f"feedback:{path.stem}"


def build_feedback(
    atlas: Atlas,
    directory: Path,
    *,
    now: datetime,
    version: int,
    windows: ReviewWindows,
    within_days: int,
    reuse: dict[str, tuple[str, str]] | None = None,
) -> int:
    """What the judge filed, as nodes. No edges out of here.

    Runs BEFORE `build_runs`, because the edge that joins the two is drawn off
    the run's own payload and points AT one of these: a run that filed three
    defects names their paths, so the link is stated by the record rather than
    inferred from two timestamps landing near each other.

    Bounded by the same window as the rest of the body, and by the same dial.
    How far back the machine's own record is drawn is one question; a second
    dial would mean whichever was smaller did the bounding while the other
    appeared to.

    Fail-soft on a missing or unreadable tree, like the run log: a machine that
    has never filed a defect is the healthy case, and it must not be the one
    that leaves the operator with no atlas.

    `reuse` is what stops the whole tree being re-hashed every pass. An
    evidence file is written once and never rewritten, so an unmoved stamp is
    the end of the question — and without this the pass hashed all of them and
    the build then REPORTED them as reused, which was a true count of matching
    hashes and a false account of the work done.
    """
    from tesseract.orchestrator.watchman.report import (
        evidence_minute,
        name_of_defect,
        read_evidence_fields,
    )

    cached = reuse or {}
    emit = Emitter(atlas, now=now, version=version, windows=windows)
    cutoff = now - timedelta(days=within_days)
    try:
        files = sorted(directory.glob("*.md"))
    except OSError:
        log.warning("atlas: filed defects could not be read; the judge is not drawn")
        return 0
    seen = 0
    for path in files:
        # The name is checked first, and the file is opened only for records it
        # cannot rule out. The body may say where IN the minute the name claims
        # and no further, so the true moment is always inside
        # `[minute, minute + 1)` — which is enough to reject both ends without
        # reading anything. Opening every file to date it was the whole
        # per-file cost once the hash stopped being paid.
        minute = evidence_minute(path.stem)
        if minute is None or minute > now or minute + timedelta(minutes=1) <= cutoff:
            continue
        filed_at, _summary, defect_kind, defect_source = read_evidence_fields(path)
        # A name that does not carry the stamp is not dated by guessing. The
        # window is the whole reason these are nodes rather than the tree is,
        # so a record that cannot say when it was filed stays out.
        if filed_at is None or filed_at < cutoff or filed_at > now:
            continue
        node_id = feedback_node_id(path)
        raw_stamp = stamp(path)
        emit.node(
            node_id,
            NodeKind.FEEDBACK,
            # What went wrong, where, and on what day — the watchman's own
            # words, from `name_of_defect`, because the producer of a record
            # names it.
            #
            # It used to be the finding's SUMMARY, which is the log line the
            # defect was found in: a median of 98 characters and a maximum of
            # 200 on the live tree. That made this the one kind of record out
            # of six named with a paragraph, and it was the kind the operator
            # opened first. The summary has not gone anywhere — it is the `#`
            # heading of the file, so it is the first line of the body the
            # panel already renders, byte for byte as the watchman filed it.
            #
            # The file name is the fallback and not the summary: a record
            # whose header says neither what broke nor where is one this
            # runtime did not write, and its stem still carries the stamp,
            # the source and the kind that named it.
            name_of_defect(defect_kind, defect_source, filed_at)
            if (defect_kind or defect_source)
            else path.stem,
            f"{_FEEDBACK_LOCATOR}/{path.name}",
            content=(
                # `raw_stamp` truthy as well, the way the vault builder does
                # it: an unreadable file stamps `""`, and a cached `""` would
                # then compare equal and hand back a hash for bytes nothing
                # can read any more.
                known[1]
                if raw_stamp and (known := cached.get(node_id)) and known[0] == raw_stamp
                else content_hash(path)
            ),
            input_stamp=raw_stamp,
        )
        seen += 1
    return seen


def _memory_node_id(value: str) -> str:
    return f"mem:{value}"


def _feedback_node_id(value: str) -> str:
    """A filed defect is named by its file, so the path in the payload becomes
    the same id the feedback builder minted from the file itself.

    The separator is taken as either kind rather than left to `Path`, which
    resolves to the HOST's flavour. The run log is portable state, and a
    Windows path read on POSIX has no separators at all there: the whole
    string became the name, matched no node, and the edge vanished without
    anything saying so.
    """
    name = value.replace("\\", "/").rsplit("/", 1)[-1]
    return feedback_node_id(PurePosixPath(name))


def _topic_tree_node_id(value: str) -> str:
    """A topic the leaf router activated, as the tree that gathers it.

    `leaf_topic_route` records the entity name it routed under — `Mirror` —
    and the tree it appends to is `trees/topic/<slug>.md`, slugged the same
    way an entity id is. So the two meet at the slug and nowhere else, which
    is why this is a function on the row rather than a prefix.
    """
    slug = entity_id(value).removeprefix("entity:")
    return f"tree:topic/{slug}" if slug else ""


def _source_tree_node_id(value: str) -> str:
    """A source a sealing pass appended to, as the tree that gathers it.

    `leaf_seal` records the buffer's source slug and writes its section into
    `trees/source/<slug>.md`, so unlike a topic there is no re-slugging on the
    way: the two already meet at the string the seal carries.
    """
    slug = (value or "").strip().strip("/")
    return f"tree:source/{slug}" if slug else ""


# Which payload fields on a run row name ids, how each names one, and what the
# resulting edge claims. A field joins this table when a producer starts naming
# ids in it, and a producer that records how MANY cannot be joined here at all:
# inventing the link from a number is the fault this plan exists to remove.
#
# Measured 2026-09-01, before the two rows below were added: 53 of 3,538 runs
# had recorded producing anything the graph could name. The commonest payload
# in the log is `capture`'s — 46 percent of every run — and it records
# `run_id`, `stages`, `not_due` and `disabled`. Those jobs did work and wrote
# down a count of it, and the only fix for that is at the recorder.
#
# The id function is part of the row rather than a shared prefix because the
# producers do not name things the same way: a promoted memory is an id
# already, a filed defect is an absolute path on the machine that wrote it
# whose portable half is its file name, and a routed topic is an entity name
# that meets its tree at a slug.
#
# **The field NAME is the whole contract, across every job.** Nothing here ties
# a key to the job that owns it, so any job writing a list under one of these
# names is making the claim the row states. Two other producers already write
# `promoted` as a COUNT, and the list check below is what keeps them out; a
# future job wanting the name for something else has to change this table.
ID_FIELDS: tuple[tuple[str, Callable[[str], str], str], ...] = (
    ("promoted", _memory_node_id, "promoted_from"),
    ("evidence_paths", _feedback_node_id, "filed_by"),
    # The memories a run wrote, which the heartbeat has recorded by id from
    # the start and nothing read. Distinct from `promoted_from`: promoting
    # lifts a record that already existed, writing makes one.
    ("memory_ids", _memory_node_id, "written_by"),
    # The topic trees a routing pass appended to. Unreadable until the derived
    # tree layer became nodes, which is why the field sat unharvested next to
    # the one above it.
    ("activated", _topic_tree_node_id, "written_by"),
    # The source trees a sealing pass appended to. `capture` is 46 percent of
    # every run row and recorded four counts, so the commonest thing the
    # machine does was also the least legible on the map.
    ("sealed_into", _source_tree_node_id, "written_by"),
)


def build_runs(
    atlas: Atlas,
    rows: Iterable[dict],
    *,
    now: datetime,
    version: int,
    windows: ReviewWindows,
    within_days: int,
) -> int:
    """What the machine did, as nodes, and the links out of them that are real.

    Runs the memory and vault builders have already finished, because the one
    cross-compartment edge here points AT a memory and is only drawn when that
    memory is in the graph. A `promoted_from` into nothing would be a permanent
    dangling row for every memory the operator has since deleted.

    Bounded by `within_days` against each row's completion, so the graph does
    not grow for as long as the machine runs, and bounded on the far side by
    `now` as well: a rebuild asked to reproduce an older pass has to see the
    log that pass saw, or every job that has run since reads as drift.
    """
    emit = Emitter(atlas, now=now, version=version, windows=windows)
    cutoff = now - timedelta(days=within_days)
    seen = 0
    for row in rows:
        run_id = str(row.get("run_id") or "").strip()
        if not run_id:
            continue
        when = _row_moment(row)
        if when is None or when < cutoff or when > now:
            continue
        job = str(row.get("job_name") or "").strip() or run_id
        locator = f"{_RUNS_LOCATOR}#{run_id}"
        # No alias: `job` is already the title, and retrieval matches title,
        # id and alias alike, so a second copy of the same string adds nothing
        # a query could find.
        node_id = f"run:{run_id}"
        emit.node(node_id, NodeKind.RUN, job, locator)
        seen += 1

        # What this is a run OF. The one edge that turns the body from three
        # and a half thousand loose dots into something with a shape: 65
        # percent of run rows name a manifest entry or a pipeline stage, and
        # before this only 53 of 3,538 runs recorded producing anything at
        # all. Drawn only where the capability is in the graph, the same rule
        # every other edge here keeps — the nine job names in neither registry
        # are the autonomy kernel's own loops, and `report.NOT_INDEXED` says
        # so rather than this inventing a node for them.
        declares = f"capability:{job}"
        if declares in atlas.nodes:
            emit.edge(
                node_id,
                declares,
                "ran",
                provenance=Provenance.STATED_IN_SOURCE,
                creator=Creator.RULE,
                locator=f"{locator}/job_name",
                asserted_by=job,
            )

        payload = row.get("payload")
        if not isinstance(payload, dict):
            continue
        for field_name, node_id_of, edge_type in ID_FIELDS:
            named = payload.get(field_name)
            if not isinstance(named, list):
                continue
            for index, raw in enumerate(named):
                target = str(raw or "").strip()
                if not target:
                    continue
                subject = node_id_of(target)
                if not subject or subject not in atlas.nodes:
                    # The record names something the graph does not hold — a
                    # memory the operator deleted, a defect filed outside the
                    # window the body is drawn over. That is a fact about the
                    # store, not an edge, and drawing it would leave a dangling
                    # row forever.
                    continue
                emit.edge(
                    subject,
                    f"run:{run_id}",
                    edge_type,
                    provenance=Provenance.STATED_IN_SOURCE,
                    creator=Creator.RULE,
                    locator=f"{locator}/{field_name}[{index}]",
                    asserted_by=job,
                )
    return seen


_RUNS_LOCATOR = "logs/schedule/runs.jsonl"


def _row_moment(row: dict) -> datetime | None:
    """When the run finished, or when it fired if it never recorded finishing."""
    for key in ("completed_at", "fired_at"):
        value = row.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return None


def _relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


__all__ = [
    "MAX_HASH_BYTES",
    "build_feedback",
    "build_memory",
    "build_vault",
    "content_hash",
    "feedback_node_id",
    "find_duplicate_slugs",
    "stamp",
]

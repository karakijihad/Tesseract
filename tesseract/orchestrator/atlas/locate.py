"""What a locator MEANS, and reading the record behind one.

Every node carries a locator, and every builder wrote it against a different
root: a memory's is relative to the memory store, a wiki page's and a raw
source's to the vault, a filed defect's to the watchman's evidence tree, and a
run's is a label rather than a path at all. Five answers, and until now nothing
outside `builders.py` could turn any of them back into a file.

**This is the only place that can**, and that is the security boundary as much
as it is the tidiness. What a caller may open is not a path: it is a node in
`atlas.json`. The graph decides which files exist for this purpose, the table
below decides which root each kind resolves against, and the resolved path is
verified to sit under that root before anything is read. A locator is data
derived from names on disk, so it is treated as untrusted even though a
builder wrote it.

**What each kind's record IS, is declared and never defaulted.** A memory and
a wiki page are frontmatter over a body. A raw source is a file the library
was given, which may be bytes nothing can read. A run is one row of a log. A
filed defect is a written page. An entity has no file of its own at all, and
saying so is the honest answer rather than opening the page that named it.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import yaml

from tesseract.orchestrator.atlas.model import Node, NodeKind

log = logging.getLogger(__name__)

#: How much of a record is handed back. A vault source can be a whole book and
#: a canvas panel is not where it gets read; past this the record says it was
#: cut and names the file it came from.
MAX_BODY_CHARS = 20_000

#: How far into `runs.jsonl` a single run is looked for. The largest log this
#: machine keeps, read line by line rather than parsed whole, and bounded so a
#: node whose row has already aged out costs a scan and not a stall.
MAX_RUN_LINES = 200_000


@dataclass(frozen=True)
class Record:
    """One record, as a person would read it.

    `said` is set only when there is nothing to show, and it carries the
    reason: no file of its own, a file that is gone, a file that cannot be
    read. Three different claims, and one empty panel for all three would
    hide the two that are faults.
    """

    #: The locator as the builder wrote it. Relative, so nothing here hands a
    #: surface the operator's own directory layout.
    locator: str
    #: The record's own fields, in the order the file carries them. A list of
    #: pairs and not a mapping, because the order IS what the file says and a
    #: dict re-keyed by a renderer is a second opinion about the record.
    properties: tuple[tuple[str, str], ...] = ()
    body: str = ""
    truncated: bool = False
    said: str = ""
    #: What kind of thing the body is, so a surface knows whether to render it
    #: as prose. Never guessed from the text.
    body_is_markdown: bool = False
    aliases: tuple[str, ...] = ()


def _home() -> Path:
    from tesseract.paths import home_dir

    return home_dir()


def _memory_root() -> Path:
    return _home() / "memory-store"


def _vault_root() -> Path:
    return _home() / "vault"


def _evidence_root() -> Path:
    from tesseract.orchestrator.watchman.report import evidence_dir

    return evidence_dir()


def _agenda_root() -> Path:
    from tesseract.orchestrator.autonomy.paths import agenda_root

    return agenda_root()


def _skills_root() -> Path:
    """Where the assistant's skills live, playbooks among them. The home
    tree, not the package: a playbook is state the assistant wrote, and an
    update that replaces `app/` leaves it where it is."""
    from tesseract.paths import workspace_dir

    return workspace_dir() / "skills"


def _source_root() -> Path:
    """The installed package tree — where the code the runtime is made of is.

    `TESSERACT_DIR` and not `home_dir()`: a capability's locator names a module
    that ships with the app, and in an install the two are different trees. It
    is the one root here that is not user state, which is exactly why it is
    named separately rather than reached through a `..` from one that is.
    """
    from tesseract.paths import TESSERACT_DIR

    return TESSERACT_DIR


#: The trees a raw source may live in, checked on the RESOLVED path so the
#: same question answers containment as well (`_inside_a_source_tree`).
#:
#: A raw source's locator is a `source_path` a producer STATES, which makes it
#: the one locator here whose value did not come from walking a tree, so it is
#: the one that needs a closed set and not a containment check alone. These
#: four are the trees the library actually takes documents from. `config/` and
#: `.env` are absent on purpose, and `routes/home_files.py` declines to serve
#: them for the same reason.
RAW_SOURCE_TREES: tuple[str, ...] = (
    "memory-store",
    "workshop",
    "vault",
    "downloads",
)

#: The roots a `source_path` may be written against, in the order they are
#: tried. THREE producers write that field and none of them agrees with the
#: others: `vault_librarian.compile_source` writes it relative to the vault,
#: `librarian/promotion._source_anchor` relative to the memory store and with a
#: `#section` on the end, and the daily and workshop passes relative to home.
#:
#: Two atlas builders each guessed ONE of the three, which is why every raw
#: source on this machine carried an empty content hash: `build_memory` joined
#: to the memory store and `build_vault` to the vault, and between them they
#: resolved none of the 26. One function tries the declared roots so the
#: builder that hashes a file and the surface that opens it cannot disagree
#: about where it is.
SOURCE_ROOTS: tuple[Callable[[], Path], ...] = (
    _home,
    _memory_root,
    _vault_root,
)


def _inside_a_source_tree(path: Path) -> bool:
    """Containment and the closed set of trees, in one question.

    Every root above is inside home, so asking which tree under home the
    resolved path landed in answers both: a `..` or a symlink that climbed out
    fails `relative_to`, and anything the library does not take documents from
    fails the list. `config/` and `.env` are refused by it.
    """
    try:
        first = path.relative_to(_home().resolve()).parts[0]
    except (ValueError, IndexError):
        return False
    return first in RAW_SOURCE_TREES


def named_file(source_path: str) -> str:
    """The file name a stated path points at, with any `#section` dropped.

    A node's title, and it must not depend on whether the file resolves: a
    document that has moved is still called what it is called. Here rather
    than in each builder because both need it and the anchor rule is this
    module's.
    """
    return PurePosixPath(source_path.split("#", 1)[0]).name


def source_file(source_path: str) -> Path | None:
    """The file a stated `source_path` names, or `None` when it names none.

    The anchor is dropped first: `daily/2026-08-13.md#anon` names a section
    inside a file, and joined whole it would look for a file with a `#` in its
    name.

    Only a file that EXISTS comes back. A stated path that resolves nowhere
    is not distinguishable from one that was deleted after it was read, and
    guessing which would mean picking a root the value may never have been
    written against; the caller says both rather than asserting one.
    """
    relative = source_path.split("#", 1)[0].strip()
    if not relative:
        return None

    for root_of in SOURCE_ROOTS:
        candidate = (root_of() / relative).resolve()
        if _inside_a_source_tree(candidate) and candidate.is_file():
            return candidate
    return None


#: Which root each kind's locator is relative to. A kind absent from this table
#: has no readable record, which is a decision and not an oversight: `ENTITY`
#: is here as `None` on purpose, because an entity's locator names the page
#: that MENTIONED it and opening that would show a wiki page under a person's
#: name.
ROOT_OF: dict[NodeKind, Callable[[], Path] | None] = {
    NodeKind.MEMORY: _memory_root,
    NodeKind.WIKI_PAGE: _vault_root,
    # Measured 2026-08-31, and it is NOT the vault root the builder hashes
    # against: of 26 raw sources on this machine, 0 resolve under `vault/`
    # and 16 under home. The other ten name a path relative to
    # `memory-store/`, which is a second convention in one field and the
    # producer's to settle. They read as a file that is not there, which
    # is what the locator says.
    NodeKind.RAW_SOURCE: _home,
    NodeKind.FEEDBACK: _evidence_root,
    NodeKind.RUN: None,
    NodeKind.ENTITY: None,
    # A derived tree is a file in the memory store like a memory is; what
    # makes it a different kind is what it holds, not where it lives.
    NodeKind.TREE: _memory_root,
    # A tag has no file, for the same reason an entity does not: it exists
    # because records carry it, and the records are where it is written.
    NodeKind.TAG: None,
    NodeKind.AGENDA: _agenda_root,
    NodeKind.CAPABILITY: _source_root,
    NodeKind.PLAYBOOK: _skills_root,
}

#: What a kind's record is allowed to BE, where the root alone is not enough.
#:
#: A capability's root is the package tree, and `config/` is inside it — the
#: keys in `providers.yaml`, the postures in `permissions.yaml`. Containment
#: would let any locator under `tesseract/` be opened, and `locate.py`'s own
#: rule is that a locator is data derived from names on disk and is untrusted
#: even though a builder wrote it. `made_of.py` only ever writes a module path,
#: so nothing legitimate loses anything; what this stops is a hand-edited
#: `atlas.json`, or a builder arriving later, turning the one root that is
#: source code into a reader for the file beside it.
#:
#: The same shape as `RAW_SOURCE_TREES` and for the same reason, one level
#: down: where a root cannot answer on its own, the closed set does.
SUFFIX_OF: dict[NodeKind, str] = {
    NodeKind.CAPABILITY: ".py",
    # A skill folder may carry `scripts/`, which the loader never reads and
    # this reader must not either.
    NodeKind.PLAYBOOK: ".md",
}


class Refused(ValueError):
    """A locator that does not resolve to a file under its own root.

    Raised rather than returned, because there is no honest partial answer:
    the record either is the file the graph names or the graph is not what is
    being read.
    """


def resolve(node: Node) -> Path:
    """The file behind a node, verified to be under that kind's own root.

    The containment check happens after `resolve()`, so a symlink pointing out
    of the tree is refused as well as a `..` in the locator itself. Both are
    unlikely from a builder and neither is impossible: the locator is built
    from names on disk, and a name is somebody else's input.
    """
    root_of = ROOT_OF.get(node.kind)
    if root_of is None:
        raise Refused(f"{node.kind.value} records have no file of their own")

    root = root_of()
    # The fragment names a field or an index inside the file, never part of
    # the path. Dropping it here is what keeps `#links[0]` from becoming a
    # directory called `links[0]`.
    relative = node.locator.split("#", 1)[0]
    if not relative.strip():
        raise Refused("the record names no file")

    if node.kind is NodeKind.RAW_SOURCE:
        # A stated path, so it goes through the same function the builder
        # hashes with: three producers write this field against three roots,
        # and two answers to where a document lives is how every one of them
        # ended up with no hash at all.
        found = source_file(node.locator)
        if found is None:
            # Both claims in one sentence, on purpose. The field is stated
            # rather than walked, and three producers write it against three
            # roots, so "gone" and "somewhere I will not open" are not
            # separable here and asserting either would be a guess.
            raise Refused(
                "the file this names is not in any of the places the library "
                "takes documents from, or it has been deleted since it was "
                "read. The next nightly pass takes it off the map"
            )
        return found

    wanted = SUFFIX_OF.get(node.kind)
    if wanted is not None and not relative.endswith(wanted):
        # Never echo the locator: it is what a caller would use to probe.
        log.warning("atlas: a %s locator is not a %s", node.kind.value, wanted)
        raise Refused("that kind of record is not kept in a file like that")
    if node.kind is NodeKind.PLAYBOOK and not _is_playbook_shape(relative):
        # The builder writes exactly `<skill dir>/SKILL.md`. A kept revision
        # under `history/` or any other markdown beside it is not the active
        # record, and a locator naming one is a graph somebody edited.
        log.warning("atlas: a playbook locator is not <dir>/SKILL.md")
        raise Refused("that kind of record is not kept in a file like that")

    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        # Never echo either path: one is the operator's directory layout and
        # the other is what a caller would use to probe it.
        log.warning("atlas: a %s locator resolved outside its own tree", node.kind.value)
        raise Refused("the record is not where that kind of record lives") from None
    return candidate


def _is_playbook_shape(relative: str) -> bool:
    parts = [p for p in relative.replace("\\", "/").split("/") if p]
    return len(parts) == 2 and parts[1] == "SKILL.md" and parts[0] not in (".", "..")


def read(node: Node) -> Record:
    """The record behind a node, in the shape a person reads it.

    Never raises. A record that cannot be read is a fact the panel has, not a
    failure of the panel, and the reason is the difference between "there is
    nothing here" and "something is wrong".
    """
    if node.kind is NodeKind.ENTITY:
        return Record(
            locator=node.locator,
            aliases=node.aliases,
            said=(
                "a person or a thing has no record of its own. It exists "
                "because other records name it, and the connections below are "
                "where it is named"
            ),
        )
    if node.kind is NodeKind.TAG:
        return Record(
            locator=node.locator,
            aliases=node.aliases,
            said=(
                "a label has no record of its own. It exists because records "
                "are filed under it, and the connections below are every "
                "record that is"
            ),
        )
    if node.kind is NodeKind.RUN:
        return _read_run(node)
    if node.kind is NodeKind.AGENDA:
        return _read_agenda(node)

    try:
        path = resolve(node)
    except Refused as refused:
        return Record(locator=node.locator, said=str(refused))

    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return Record(
            locator=node.locator,
            said=(
                "the file this was drawn from is not there any more. The next "
                "nightly pass takes it off the map"
            ),
        )
    except UnicodeDecodeError:
        # The vault stores what it is given and indexes only what it can read.
        # A PDF is a record the library HAS, and saying that is the answer.
        return Record(
            locator=node.locator,
            said=(
                "this is a file the library keeps rather than something it "
                "reads. It is stored whole and searched by what other records "
                "say about it"
            ),
        )
    except OSError:
        log.warning("atlas: a %s record could not be read", node.kind.value)
        return Record(
            locator=node.locator,
            said="the file this was drawn from could not be read",
        )

    properties, body = _split_frontmatter(raw)
    cut = len(body) > MAX_BODY_CHARS
    return Record(
        locator=node.locator,
        properties=properties,
        body=body[:MAX_BODY_CHARS] if cut else body,
        truncated=cut,
        body_is_markdown=True,
        aliases=node.aliases,
    )


def _split_frontmatter(raw: str) -> tuple[tuple[tuple[str, str], ...], str]:
    """The file's own fields in the file's own order, then the rest of it.

    Order matters: the point of showing a record here is that somebody who
    never installs Obsidian sees what Obsidian would show them, and Obsidian
    shows the frontmatter as written. `yaml.safe_load` returns a dict, and a
    dict is insertion-ordered in Python, so the order survives the parse.

    A file whose frontmatter does not parse keeps its body: half a record is
    worth more than a message about YAML.
    """
    if not raw.startswith("---"):
        return (), raw
    parts = raw.split("\n---", 2)
    if len(parts) < 2:
        return (), raw
    head = parts[0][3:]
    body = parts[1].lstrip("\n") if len(parts) == 2 else parts[1].lstrip("\n")
    try:
        parsed = yaml.safe_load(head)
    except yaml.YAMLError:
        return (), raw
    if not isinstance(parsed, dict):
        return (), raw
    return tuple((str(key), _as_text(value)) for key, value in parsed.items()), body


def _as_text(value: object) -> str:
    """A field's value as one string. An empty one stays empty, because the
    surface says `Empty` and a renderer that received `"[]"` could not."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value if str(item).strip())
    if isinstance(value, dict):
        return ", ".join(f"{key}: {item}" for key, item in value.items())
    return str(value)


def _read_agenda(node: Node) -> Record:
    """One agenda item, read the way a run's row is read and not as prose.

    An item is a JSON object, so the generic path was handing its raw bytes to
    a MARKDOWN renderer: the Inspector's own rule is that machine text must not
    be interpreted, because a structure's punctuation becomes headings and
    emphasis. It also buried the goal, the status and the score in a wall of
    braces when they are the fields somebody opens this to read.

    So the scalars are the record's own fields, in the order the file carries
    them, and what is left — the status history, the approvals, the score
    components — is the body, rendered as written. The same shape `_read_run`
    gives a run row, for the same reason.
    """
    try:
        path = resolve(node)
    except Refused as refused:
        return Record(locator=node.locator, said=str(refused))
    try:
        item = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Record(
            locator=node.locator,
            said=(
                "the item this was drawn from is not there any more. The next "
                "nightly pass takes it off the map"
            ),
        )
    except (OSError, ValueError):
        log.warning("atlas: one agenda item could not be read")
        return Record(
            locator=node.locator, said="this decision could not be read"
        )
    if not isinstance(item, dict):
        return Record(locator=node.locator, said="this decision could not be read")

    nested = {
        key: value
        for key, value in item.items()
        if isinstance(value, (dict, list)) and value
    }
    return Record(
        locator=node.locator,
        properties=tuple(
            (str(key), _as_text(value))
            for key, value in item.items()
            if key not in nested
        ),
        body=json.dumps(nested, indent=2) if nested else "",
    )


def _read_turn(node: Node) -> Record:
    """A conversation turn's own record, the manifest file recovery reads.

    The locator's path half is a label under `runtime/turns/`, and the file is
    found from `turns_root()`, which is what decides where the tree is; the
    day directory and the file name are taken from the label, and nothing
    else in it is trusted as a path.
    """
    from tesseract.orchestrator.turns import turns_root

    relative = node.locator.split("#", 1)[0]
    parts = [p for p in relative.split("/") if p]
    if len(parts) != 4 or parts[:2] != ["runtime", "turns"] or ".." in parts:
        return Record(locator=node.locator, said="this turn names no record")
    path = turns_root() / parts[2] / parts[3]
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Record(
            locator=node.locator,
            said="the turn's record has aged out; the map still knows it happened",
        )
    except (OSError, ValueError):
        return Record(locator=node.locator, said="the turn's record could not be read")
    return Record(
        locator=node.locator,
        properties=tuple(
            (str(key), _as_text(value))
            for key, value in raw.items()
            if key != "stages"
        ),
        body=json.dumps(raw.get("stages"), indent=2) if raw.get("stages") is not None else "",
    )


def _read_run(node: Node) -> Record:
    """One row of the run log, found by the id its node was named for.

    The path half of a run's locator is a LABEL: `builders.py` writes
    `logs/schedule/runs.jsonl` as a literal while `scheduler/log.py::runs_path`
    is what decides where the file is. So the file comes from there and the
    locator's fragment is what identifies the row.

    Read a line at a time. This is the largest log the machine keeps, and the
    Atlas room already paid once for parsing it whole to answer one question.
    """
    from tesseract.scheduler.log import runs_path

    if node.locator.startswith("runtime/turns/"):
        return _read_turn(node)

    run_id = node.locator.split("#", 1)[-1] if "#" in node.locator else ""
    if not run_id:
        return Record(locator=node.locator, said="this run names no record")

    target = runs_path()
    try:
        with target.open(encoding="utf-8") as handle:
            for count, line in enumerate(handle):
                if count >= MAX_RUN_LINES:
                    break
                if run_id not in line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if str(row.get("run_id") or "") != run_id:
                    continue
                return Record(
                    locator=node.locator,
                    properties=tuple(
                        (str(key), _as_text(value))
                        for key, value in row.items()
                        if key != "payload"
                    ),
                    # The payload is the run's own account of what it did, and
                    # it is a nested structure rather than prose. Rendered as
                    # written, indented, so nothing here decides what a step
                    # meant.
                    body=json.dumps(row.get("payload"), indent=2)
                    if row.get("payload") is not None
                    else "",
                )
    except OSError:
        log.warning("atlas: the run log could not be read for one record")
        return Record(
            locator=node.locator,
            said="the record of what runs on this machine could not be read",
        )
    return Record(
        locator=node.locator,
        said=(
            "this run has aged out of the log it was drawn from. The next "
            "nightly pass takes it off the map"
        ),
    )


# ── one namespace ───────────────────────────────────────────────────────────
#
# `atlas_query`'s own docstring said where it stopped: "Memory records only,
# for now. The vault's FTS ids key raw ingest paths and the atlas keys wiki
# slugs, and those two namespaces do not meet." So the one structure in this
# runtime built to span memory, vault, runtime and what the machine is made of
# was read by a tool that could see one compartment of four.
#
# **The graph already spans them.** What was single-compartment was the READER.
#
# The join is made here rather than in the tool for the reason the rest of this
# module exists: what an id MEANS is one question with one answer, and a second
# reader spelling out a chunk-id format for itself is how `_as_node_id` came to
# split `vault:` ids on a `#` that no chunk id has ever contained — mapping
# every vault row to something the graph never held, with a test that asserted
# the same invented shape so both agreed and both were wrong.
#
# And it is made through the FILE, never through string surgery. A raw source's
# node id is the `source_path` a producer stated, written against one of three
# roots; a vault chunk id is a vault-relative path. Those two strings can name
# one document and not match, so both are resolved to a path on disk and the
# paths are compared. `source_file` is the one function that resolves either.

#: How the memory FTS spells a row that is not a memory. Imported rather than
#: repeated: `vault_indexer.chunk_id` and `consistency` own these, and a copy
#: here is the second opinion this section exists to remove.
def _foreign_prefixes() -> tuple[str, str]:
    from tesseract.memory.consistency import DAILY_FTS_PREFIX
    from tesseract.memory.vault_indexer import VAULT_ID_PREFIX

    return VAULT_ID_PREFIX, DAILY_FTS_PREFIX


#: Id shapes that resolve to no node, and why. Declared rather than discovered,
#: because a join that fails quietly is worse than one that does not exist: a
#: caller reads an empty answer as an empty library.
UNRESOLVED: tuple[tuple[str, str], ...] = (
    (
        "a vault document nothing in the library cites",
        "the atlas holds a raw source because a memory or a wiki page names it "
        "as where it came from. A document that has been ingested and never "
        "cited is in the vault's index and not on the map",
    ),
    (
        "a daily note no memory was drawn from",
        "same rule: a daily note reaches the map as the source a memory cites, "
        "so one nothing was distilled out of has no node",
    ),
    (
        "a record filed since the map was last drawn",
        "the map is derived and is rebuilt on a schedule, so a record written "
        "after the last pass resolves to nothing until the next one. It is in "
        "the store and a search finds it; the map catches up",
    ),
)


def _by_file(atlas) -> dict[Path, str]:
    """Every node that names a file, keyed by the file itself.

    Built per call. The alternative is holding it on the graph, and the graph
    is re-read per question anyway: a resolution that cached would answer from
    a map the nightly pass had already replaced.
    """
    found: dict[Path, str] = {}
    for node in atlas.nodes.values():
        try:
            path = resolve(node)
        except Refused:
            continue
        found.setdefault(path, node.id)
    return found


def node_for(atlas, foreign_id: str) -> str:
    """The node an id names, wherever the id came from, or `""`.

    Three cases, in order:

    1. It is already a node id. The atlas answers for itself.
    2. It is a memory FTS row (`mem_<id>`). The builder names those `mem:<id>`,
       so the row gains a prefix.
    3. It names a FILE — a vault chunk or a daily note. Both are resolved to a
       path and matched against the file every node resolves to, so the two
       spellings of one document meet at the document.

    `""` and never a guess: a caller that got a plausible wrong node would walk
    the graph from the wrong place and cite it.
    """
    row_id = (foreign_id or "").strip()
    if not row_id:
        return ""
    if row_id in atlas.nodes:
        return row_id
    if row_id.startswith("mem_"):
        return f"mem:{row_id}" if f"mem:{row_id}" in atlas.nodes else ""

    vault_prefix, daily_prefix = _foreign_prefixes()
    if row_id.startswith(vault_prefix):
        # `vault:<vault-relative path>:chunk_<n>`. The chunk index is dropped
        # from the RIGHT, because a path may itself contain a colon on a
        # machine that allows one and splitting from the left would take the
        # drive letter.
        body = row_id[len(vault_prefix):]
        relative = body.rsplit(":chunk_", 1)[0]
    elif row_id.startswith(daily_prefix):
        relative = f"daily/{row_id[len(daily_prefix):]}.md"
    else:
        return ""

    found = source_file(relative)
    if found is None:
        return ""
    return _by_file(atlas).get(found, "")


__all__ = [
    "MAX_BODY_CHARS",
    "MAX_RUN_LINES",
    "RAW_SOURCE_TREES",
    "ROOT_OF",
    "SOURCE_ROOTS",
    "SUFFIX_OF",
    "UNRESOLVED",
    "Record",
    "Refused",
    "named_file",
    "node_for",
    "read",
    "resolve",
    "source_file",
]

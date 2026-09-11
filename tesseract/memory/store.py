"""Canonical memory file store.

Read/write/delete/list .md files in memory-store/. Every file has YAML
frontmatter validated by MemoryFrontmatter. Access and write events are
logged to events/*.jsonl.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence

import yaml

from tesseract.memory.derivation import (
    DerivationRefusal,
    load_derivation_config,
    resolve as derivation_resolve,
)
from tesseract.memory.related_block import (
    END_MARKER,
    START_MARKER,
    RelatedItem,
    replace_related_block,
)
from tesseract.memory.types import MemoryFrontmatter, MemoryType, Stability
from tesseract.memory.capture_policy import CapturePolicy

logger = logging.getLogger(__name__)

#: The auto-managed Related block, matched ONLY where `replace_related_block`
#: puts one: at the end of the body, from the LAST opening marker. Anchoring
#: the end alone is not enough, and this was wrong once: `search` finds the
#: FIRST marker, and a non-greedy body then runs to the last closing one, so a
#: record whose prose quotes the markers had that prose swallowed into the
#: block and could never be classified as repaired. Starting at the last
#: opening marker means the interior can hold no marker of its own.
_AUTO_BLOCK_TAIL = re.compile(
    re.escape(START_MARKER) + r"(?P<inner>.*?)" + re.escape(END_MARKER) + r"\s*\Z",
    re.DOTALL,
)
#: One rendered link line, in the two shapes `render_related_block` writes.
#: The alias is captured rather than skipped: checking the id alone left the
#: text beside it unchecked, and `- [[a real id|any payload at all]]` is a
#: well-formed line whose id passes. That was a live bypass, not a latent one.
_AUTO_BLOCK_LINK = re.compile(
    r"^-\s*\[\[(?P<id>[^\]|]+)(?:\|(?P<alias>[^\]]*))?\]\]$"
)


def _body_of(text: str) -> str:
    """The body of a memory file, without running its frontmatter through YAML.

    Line-anchored like `MemoryStore._split_frontmatter`, and not a split on the
    bare substring `---`: any YAML value carrying those three characters moves
    a naive split's second boundary and hands back something that is not the
    body. Raises `ValueError` on a file that opens a fence and never closes it.
    """
    text = text.replace("\r\n", "\n")
    if not text.startswith("---\n"):
        return text.strip()
    return text[text.index("---\n", 4) + 4:].strip()


def _split_auto_block(body: str) -> tuple[str, str | None]:
    """`(prose, block interior)`, where a block only counts at the very end.

    From the LAST opening marker rather than the first, so a marker quoted in
    the record's own prose stays prose on both sides of the comparison.
    """
    start = body.rfind(START_MARKER)
    if start == -1:
        return body.strip(), None
    match = _AUTO_BLOCK_TAIL.match(body, start)
    if match is None:
        return body.strip(), None
    return body[:start].strip(), match.group("inner")


def _block_declares_only(
    inner: str,
    auto_links: Sequence[str],
    title_of: Callable[[str], str | None],
) -> bool:
    """Whether the block holds nothing but links the store itself would render.

    This is what stops the marker pair being a hole. Without it, `stored prose
    + markers + anything at all` compares equal to what is stored, so the write
    reads as a repair and the payload is persisted having been judged by
    nothing.

    EVERY part of a line is checked against something the store owns, not just
    the id. The id must be in the record's own `auto_links`, and the alias must
    be the linked record's own title, which is what `render_related_block`
    puts there. Checking the id alone was the first version and it left the
    text beside it free: one real id plus `|` plus anything was accepted, and
    `memory_update` could reach it with caller content on any record that had
    a link. A caller supplies no part of this region that the store cannot
    derive for itself.
    """
    declared = {str(link) for link in (auto_links or ())}
    for line in inner.splitlines():
        line = line.strip()
        if not line or line == "## Related":
            continue
        match = _AUTO_BLOCK_LINK.match(line)
        if match is None:
            return False
        memory_id = match.group("id").strip()
        if memory_id not in declared:
            return False
        alias = match.group("alias")
        if alias is None:
            continue
        # An alias that is not the neighbour's own title is text this record
        # did not get from the store. A title is not itself judged by the
        # capture policy, here or anywhere, so this bounds the region to what
        # is already in the store rather than making it unreachable.
        if alias.strip() != (title_of(memory_id) or "").strip():
            return False
    return True

# Layer A — operator-directives section in the system prompt.
# Floor 6 keeps load-bearing rules and drops trivial corrections.
DIRECTIVES_IMPORTANCE_FLOOR = 6


def extract_wikilinks(text: str) -> list[str]:
    return re.findall(r"\[\[([^\]]+)\]\]", text)


def _publish_bus_event(event: str, payload: dict) -> None:
    """Publish to the orchestrator background bus, log (don't re-raise) on failure.

    The orchestrator is optional — if the module fails to import, no-op.
    Runtime errors in publish() are demoted to debug-level so a transient
    bus issue can't break memory writes, but they do show up in logs for
    triage instead of being silently swallowed.
    """
    try:
        from tesseract.orchestrator.background_event_bus import get_background_bus
    except ImportError:
        return
    try:
        get_background_bus().publish(event, payload)
    except Exception:
        logger.debug("background_bus publish %r failed", event, exc_info=True)


def _inject_kind_tag(frontmatter: MemoryFrontmatter) -> MemoryFrontmatter:
    """Return a copy of ``frontmatter`` whose ``tags`` list is led by
    the record's MemoryType value (``feedback`` / ``user`` / ``project`` /
    ``reference`` / ``conscience``).

    The kind tag is load-bearing — Obsidian's graph view color groups
    key off ``tag:#<kind>``. Operator-set tags
    survive after the kind tag. The function is idempotent — calling
    twice yields the same frontmatter.
    """
    kind = frontmatter.type.value
    existing = list(frontmatter.tags or [])
    if existing and existing[0] == kind:
        return frontmatter
    deduped: list[str] = [kind]
    seen: set[str] = {kind}
    for tag in existing:
        if not tag or tag in seen:
            continue
        seen.add(tag)
        deduped.append(tag)
    return frontmatter.model_copy(update={"tags": deduped})


RECORD_SUBDIRS = ("user", "feedback", "project", "reference", "conscience")


def list_frontmatter(
    store_dir: Path,
    type_filter: MemoryType | None = None,
    _cache: dict[str, tuple[int, int, MemoryFrontmatter | None]] | None = None,
) -> list[MemoryFrontmatter]:
    """Every parseable memory record under ``store_dir``, frontmatter only.

    A module-level function rather than a method because constructing a
    ``MemoryStore`` calls ``_ensure_dirs``, which creates the store tree. Read-
    only callers — anything answering "what is in here" rather than writing to
    it — must be able to ask without bringing the tree into existence.

    ``_cache`` (internal — ``MemoryStore.list_all`` passes its own) maps a
    file path to ``(mtime_ns, size, frontmatter-or-None)``; unchanged files
    skip the read+YAML+model pass, turning the per-query cost of this walk
    from a full re-parse of the store into one stat per file. ``None`` marks
    a non-record file (operator docs) so it costs only the stat on repeats.
    External edits, deletes, and new files are all caught by the stat compare.
    """
    subdirs = (
        (type_filter.value,) if type_filter else RECORD_SUBDIRS
    )
    results: list[MemoryFrontmatter] = []
    walked: set[str] = set()
    for subdir in subdirs:
        subdir_path = store_dir / subdir
        if not subdir_path.exists():
            continue
        # rglob walks operator sub-buckets (`reference/people/`, etc.)
        # so files dropped into a new folder are picked up without a
        # schema change. The frontmatter `type` still routes writes;
        # sub-buckets are organizational only.
        for md_file in subdir_path.rglob("*.md"):
            # Skip pure operator docs (README.md, INDEX.md) silently —
            # they live alongside memory records as folder-level
            # documentation and intentionally carry no frontmatter.
            # Files WITH a malformed frontmatter still log a warning
            # via the except below.
            try:
                key = ""
                stat = None
                if _cache is not None:
                    key = str(md_file)
                    walked.add(key)
                    stat = md_file.stat()
                    hit = _cache.get(key)
                    if (
                        hit is not None
                        and hit[0] == stat.st_mtime_ns
                        and hit[1] == stat.st_size
                    ):
                        if hit[2] is not None:
                            results.append(hit[2])
                        continue
                with md_file.open("r", encoding="utf-8") as f:
                    first = f.readline()
                if first.strip() != "---":
                    if _cache is not None and stat is not None:
                        _cache[key] = (stat.st_mtime_ns, stat.st_size, None)
                    continue
                text = MemoryStore._read_frontmatter_block(md_file)
                fm = MemoryStore._parse_frontmatter_only(text)
                if _cache is not None and stat is not None:
                    _cache[key] = (stat.st_mtime_ns, stat.st_size, fm)
                results.append(fm)
            except Exception:
                logger.warning("Failed to parse %s", md_file)
    if _cache is not None and type_filter is None:
        # Full scan — drop cache entries for files that no longer exist.
        for stale in set(_cache) - walked:
            del _cache[stale]
    return results


#: The subdirectories a memory of a declared `MemoryType` can live in, and
#: the only ones a lookup by id searches. `daily`, `derived` and `events` are
#: staging and are deliberately NOT here: the librarian promotes out of them,
#: and an id resolved from one would be a record the store does not consider
#: filed yet. Exported because a reader outside this module that needs to find
#: a memory by id must search the same set, and three copies of a list like
#: this is how one of them comes to be missing a type.
MEMORY_SUBDIRS: tuple[str, ...] = (
    "user", "feedback", "project", "reference", "conscience",
)


def _inside(path: Path, root: Path) -> bool:
    """Whether `path` really lands inside `root`, symlinks followed.

    `resolve()` before comparing, because the question is where the file IS
    and not how it was spelled. Missing parents are fine: `resolve` on a
    non-existent path still normalises it, and a path that cannot be resolved
    at all is not one to hand back.
    """
    try:
        return path.resolve().is_relative_to(root)
    except (OSError, ValueError):
        return False


class MemoryStore:
    def __init__(self, store_dir: Path) -> None:
        self._store_dir = store_dir
        self._policy = CapturePolicy()
        # Parsed-frontmatter cache for list_all — path -> (mtime_ns, size,
        # frontmatter-or-None). The lock serializes concurrent scans:
        # list_all runs both on the loop thread and under asyncio.to_thread
        # (retrieval Stage 0/A), and a torn dict iteration would raise.
        # Known tradeoff: write()/delete() invalidation takes this same lock,
        # so a loop-thread write can block for the duration of a concurrent
        # scan — bounded by the cold first scan (seconds on a large store,
        # once per process); warm scans are stat-only. The coarse lock is
        # load-bearing: without it a scan racing a write could re-insert a
        # stale entry after the write's invalidation.
        self._fm_cache: dict[str, tuple[int, int, MemoryFrontmatter | None]] = {}
        self._fm_cache_lock = threading.Lock()
        self._ensure_dirs()

    @property
    def store_dir(self) -> Path:
        """Public read-only view of the store root. Callers that need to
        write forensic events (memory_save type_mismatch guard, librarian
        heartbeat log) derive paths from here.
        """
        return self._store_dir

    def _ensure_dirs(self) -> None:
        # memory types: user / feedback / project / reference /
        # conscience. derived/ holds FAISS + FTS artifacts; events/ holds the
        # write/access audit log. daily/ (F1 2026-04-20) is the raw capture
        # layer — the librarian promotes entries from daily/ into the
        # canonical subdirs on heartbeat. Phase-1 identity/ and memory/
        # (retired 2026-04-17) are left alone; MemoryType enum has no
        # entries for them.
        for subdir in (*MEMORY_SUBDIRS, "daily", "derived", "events"):
            (self._store_dir / subdir).mkdir(parents=True, exist_ok=True)

    def _type_to_subdir(self, mem_type: MemoryType) -> str:
        return mem_type.value

    def find_file(self, memory_id: str) -> Path | None:
        """The file a memory id names, or None.

        **This is the chokepoint, so every property is held here at once.**
        `read`, `delete`, `update` and `promote` all act on what this returns
        and none of them re-checks it, and `delete` unlinks it. The id is a
        plain tool argument, so it is whatever the model was persuaded to
        pass. The same escape was already closed once in this subsystem, in
        `memory_get._resolve_memory_path`; it was open here.

        1. The result is inside the store or there is no result. Checked on
           the RESOLVED path, not the spelling, so a symlink inside a bucket
           pointing out of the tree is caught as well as a `..` segment.
        2. An id is ONE path segment. Ids on disk are `mem_3adcf2b8`; the
           sub-buckets an operator curates (`reference/people/`) are
           DIRECTORIES the walk finds, never part of an id. A separator in
           one is not a curated bucket, it is a way out of the store.
        3. Those sub-buckets keep working: the recursive walk stays.
        4. It fails closed and never raises. Every caller reads None as "no
           such memory", which is the answer a refused id deserves.
        5. It costs what it cost: the containment check is per candidate, not
           a second walk.
        """
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", memory_id or ""):
            return None
        try:
            root = self._store_dir.resolve()
        except OSError:
            return None
        target = f"{memory_id}.md"
        for subdir in MEMORY_SUBDIRS:
            base = self._store_dir / subdir
            if not base.exists():
                continue
            direct = base / target
            if direct.exists() and _inside(direct, root):
                return direct
            for path in base.rglob(target):
                if path.is_file() and _inside(path, root):
                    return path
        return None

    def _is_an_admission(
        self, path: Path, body: str, auto_links: Sequence[str]
    ) -> bool:
        """Whether this write is putting NEW content into the store.

        Every property this has to hold AT ONCE, because it has now been got
        wrong twice in two different directions:

        1. A record that is not on disk yet is an admission.
        2. A write that hands back the stored body unchanged is a repair.
        3. A write that differs ONLY in the auto-managed Related block is a
           repair: the cascade strips it, the scrub rewrites it and the
           auto-linker adds to it, and all three can leave a body under the
           trivial-body floor.
        4. Any other difference in the prose is an admission, however old the
           id. This is the one `path.exists()` missed, and `memory_update`
           walked through it with caller content.
        5. Nothing is declared by a caller and nothing is exempt by name.
        6. **Caller bytes never get to say where the auto-managed region is.**
           The markers are matched only as a block at the END of the body,
           where `replace_related_block` puts one, so a body whose own prose
           quotes them is compared as prose on both sides rather than having a
           span of itself silently eaten.
        7. **And never what is inside it.** A repair's block may contain only
           links the FRONTMATTER already declares. Otherwise `stored prose +
           markers + anything` compares equal to the stored body and arbitrary
           content rides into the store inside a region nothing judged, which
           is property 4 defeated by the fix for property 6.

        A stored record that cannot be read is an admission. That is the safe
        direction: judging a repair costs a repair, and admitting unjudged
        content costs the rule.
        """
        if not path.exists():
            return True
        try:
            stored = _body_of(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):  # unreadable or malformed, so judge it
            return True

        stored_prose, _ = _split_auto_block(stored)
        new_prose, new_inner = _split_auto_block(body)
        if new_prose != stored_prose:
            return True
        if new_inner is None:
            # The block removed altogether, which is the cascade's own move
            # when a record loses its last neighbour.
            return False

        def title_of(memory_id: str) -> str | None:
            record = self.read(memory_id, log_access=False)
            return record[0].title if record is not None else None

        return not _block_declares_only(new_inner, auto_links, title_of)

    @property
    def last_block_reason(self) -> str | None:
        """Which capture rule turned the last write away, if one did.

        A caller that has to tell the operator why a save did not happen needs
        the rule's key to look up what it says about itself. Exposed rather
        than reached for, so the rule stays a fact of the policy and not of
        whoever asked.
        """
        return self._policy.last_reason

    def log_event(self, filename: str, entry: dict) -> None:
        """Append a forensic event to `events/<filename>` with an auto timestamp.

        Public so external callers (memory_save's type_mismatch guard) can
        route through the same JSONL sink instead of writing directly.
        """
        entry["timestamp"] = datetime.now(timezone.utc).isoformat()
        events_dir = self._store_dir / "events"
        events_dir.mkdir(parents=True, exist_ok=True)
        path = events_dir / filename
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

    def write(
        self,
        frontmatter: MemoryFrontmatter,
        body: str,
        *,
        subdir_override: str | None = None,
    ) -> bool:
        # Cleared per write, not per judgement. `last_block_reason` is read by
        # whoever has to tell the operator why a save did not happen, and only
        # `admits()` used to reset it, so a repair whose credential check then
        # failed reported whichever rule had blocked some earlier, unrelated
        # write on the same store.
        self._policy.last_reason = None

        if subdir_override is not None:
            target_subdir = self._validate_relative_path(subdir_override)
            target_subdir.mkdir(parents=True, exist_ok=True)
            path = target_subdir / f"{frontmatter.id}.md"
        else:
            # Preserve existing location so updates/auto-link/librarian
            # rewrites to a memory living in a sub-bucket (e.g.
            # `reference/people/`) don't relocate it back to the type root.
            # Only falls back to the type default when the file is brand-new.
            existing_path = self.find_file(frontmatter.id)
            if existing_path is not None:
                path = existing_path
            else:
                subdir = self._type_to_subdir(frontmatter.type)
                path = self._store_dir / subdir / f"{frontmatter.id}.md"

        # Depth is stamped here and nowhere else, because this is the one
        # function that can see both the record and the records it names. A
        # caller cannot declare itself shallow, and a chain that would run
        # past the cap never reaches the disk — a refused write is recoverable
        # and a store full of restatement is not.
        #
        # New records only. The rule governs adding a link to a chain, not
        # touching a record that already exists: the cascade and scrub paths
        # rewrite records written long before this field, and refusing their
        # repair would strand exactly the records most in need of it.
        #
        # One stat on the resolved path rather than `find_file`, which rglobs
        # five subdirectories and finds nothing by definition when the record
        # is new. Asking it here would have put five recursive walks of the
        # store on the `subdir_override` path, which skipped that lookup
        # entirely before.
        # Admission is asked of an ADMISSION, and this is where the store
        # decides what one is. The properties it has to hold at once:
        #
        #   1. a new record is judged;
        #   2. a repair is never judged, because the cascade and the scrub
        #      strip a Related block and can legitimately take a record under
        #      the trivial-body floor, and refusing that repair would strand
        #      the records most in need of it;
        #   3. no caller can opt out by passing an argument, which is what the
        #      deleted `skip_wnts_check` let any of them do;
        #   4. content a caller SUPPLIES is judged even when it lands on a
        #      record that already exists.
        #
        # Property 4 is the one `path.exists()` alone got wrong. It reads as a
        # test of whether this is an admission and is really a test of whether
        # the ID is new, and those come apart at `memory_update`, which takes
        # caller content and writes it under the same id: a whole body of code
        # or turn-recap entered the store with no rule consulted, through an
        # `auto` tool. So the question asked is whether the BODY is the store's
        # own. A repair hands back what was there, or what was there with its
        # link block removed; anything else is content arriving from outside
        # and is judged. Nothing is declared by a caller and nothing is
        # exempt by name.
        judged = self._is_an_admission(path, body, frontmatter.auto_links)
        if judged:
            if not self._policy.admits(body):
                reason = self._policy.last_reason or "capture_policy"
                self.log_event("writes.jsonl", {
                    "memory_id": frontmatter.id,
                    "type": frontmatter.type.value,
                    "title": frontmatter.title,
                    "status": "blocked",
                    "reason": reason,
                })
                logger.info("Memory %s blocked by %s", frontmatter.id, reason)
                return False

        # Derivation stays NEW RECORDS ONLY, which is a narrower question than
        # admission and deliberately not the same one: the rule governs adding
        # a link to a chain, and the cascade and scrub rewrite records written
        # long before the field existed. Widening it to every admission would
        # refuse a repair this function has just decided is not an admission.
        if not path.exists():
            frontmatter, refusal = self._resolve_derivation(frontmatter)
            if refusal is not None:
                self.log_event("writes.jsonl", {
                    "memory_id": frontmatter.id,
                    "type": frontmatter.type.value,
                    "title": frontmatter.title,
                    "status": "refused",
                    "reason": refusal.message(),
                })
                logger.info("Memory %s refused: %s", frontmatter.id, refusal.message())
                return False

        # Every memory record gets a leading `kind` tag matching
        # its MemoryType so the Obsidian graph view's color groups fire
        # on the canonical store directly (no separate wiki mirror).
        # Operator-set tags survive AFTER the kind tag. Idempotent on
        # repeat writes — set semantics enforced by `_inject_kind_tag`.
        frontmatter = _inject_kind_tag(frontmatter)
        yaml_dict = frontmatter.to_yaml_dict()
        content = "---\n" + yaml.dump(yaml_dict, default_flow_style=False, sort_keys=False) + "---\n\n" + body

        # A memory is the most durable of the four places a credential can
        # land: it survives the conversation, it is retrieved into later
        # prompts, and nothing asks before one is written. Here rather than in
        # `memory_save` because this is the one function that puts a memory
        # file on disk — the tool, `memory_update`, the librarian, the
        # auto-linker and the cascade rewrites all arrive through it.
        #
        # The wall is on the way in at `brain/tools.py::execute_tool`, so this
        # is a backstop. It REFUSES the write when the store cannot be read,
        # which is the one degradation that is safe here: an unwritten memory
        # is recoverable and a durable one is not.
        # A check that cannot run refuses the write, and that INCLUDES the
        # check failing to import. It used to set `redact = None` and carry on,
        # so the one condition the comment above calls unsafe was the one
        # condition that wrote the record anyway. `SECURITY.md` states the
        # stronger rule to the operator, and this is the code that owes it.
        try:
            from tesseract.credentials.redaction import redact

            content = redact(content)
        except Exception as exc:  # noqa: BLE001
            self.log_event("writes.jsonl", {
                "memory_id": frontmatter.id,
                "type": frontmatter.type.value,
                "title": frontmatter.title,
                "status": "blocked",
                "reason": "credential_check_unavailable",
            })
            logger.error(
                "Memory %s not written: it could not be checked for "
                "credentials (%s)", frontmatter.id, exc,
            )
            return False

        tmp = path.with_suffix(".tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(path)
        # Our own rewrite may not move (mtime_ns, size) — a fixed-width
        # updated_at bump inside the same filesystem tick is byte-for-byte
        # the same stat. Invalidate directly; the stat compare only has to
        # catch external (human-timescale) edits.
        with self._fm_cache_lock:
            self._fm_cache.pop(str(path), None)

        self.log_event("writes.jsonl", {
            "memory_id": frontmatter.id,
            "type": frontmatter.type.value,
            "title": frontmatter.title,
            "status": "written",
            # Whether the rules looked at this at all. The ledger could say
            # what the funnel refused and not what it declined to examine,
            # and the repair path is the one a bug would ride in on: a
            # regression that started classifying admissions as repairs left
            # no trace anywhere.
            "judged": judged,
        })
        logger.info("Memory %s written to %s", frontmatter.id, path)
        _publish_bus_event("memory_written", {"id": frontmatter.id, "source": frontmatter.type.value})
        return True

    def _resolve_derivation(
        self, frontmatter: MemoryFrontmatter
    ) -> tuple[MemoryFrontmatter, DerivationRefusal | None]:
        """Look up what this record says it was written from, then judge it.

        Resolving a locator is the store's job and deciding what it means is
        `derivation`'s; keeping them apart is what lets a locator pointing
        outside the store (a vault file, a daily note) read as raw material
        rather than as an unreadable memory.

        A config the operator has broken must not take memory writing down
        with it: memory writes are unconditional and this check is a rule
        over them, so an unreadable config leaves the write alone.
        """
        try:
            config = load_derivation_config()
        except Exception as exc:  # noqa: BLE001
            logger.warning("derivation rule not applied (%s)", exc)
            return frontmatter, None
        sources: dict[str, MemoryFrontmatter | None] = {}
        unresolved: set[str] = set()
        for locator in frontmatter.derived_from:
            record = None
            if locator.startswith("mem_"):
                read_result = self.read(locator, log_access=False)
                if read_result is not None:
                    record, _ = read_result
                else:
                    # A `mem_` id that does not read is NOT raw material. It
                    # is a deleted source, a typo, or a race with a concurrent
                    # delete, and treating it as raw would let one bogus id
                    # satisfy the every-summary-reads-something-raw rule and
                    # reset the depth of a chain that never ended.
                    # `_cascade_deleted_id` cleans `links` and `auto_links`
                    # and never `derived_from`, so a deleted source is a live
                    # route to this.
                    unresolved.add(locator)
            sources[locator] = record
        return derivation_resolve(frontmatter, sources, config, unresolved=unresolved)

    def update_body(self, memory_id: str, new_body: str) -> bool:
        """Refresh an existing memory's body + `updated_at`.

        Returns False when the id is unknown or the write does not land. This
        replaces the body with content from somewhere else, so the capture
        policy DOES judge it: what makes a write a repair is handing back the
        body that is already there, and a cosine merge does the opposite.
        Logs a `status: "updated", reason: "cosine_merge"` event alongside the
        atomic-write event so the forensic log shows both the body replace and
        the dedupe-driven rationale.
        """
        existing = self.read(memory_id, log_access=False)
        if existing is None:
            return False
        fm, _ = existing
        fm = fm.model_copy(update={"updated_at": datetime.now(timezone.utc)})
        ok = self.write(fm, new_body)
        if ok:
            self.log_event("writes.jsonl", {
                "memory_id": memory_id,
                "type": fm.type.value,
                "title": fm.title,
                "status": "updated",
                "reason": "cosine_merge",
            })
        return ok

    def read(self, memory_id: str, log_access: bool = True) -> tuple[MemoryFrontmatter, str] | None:
        path = self.find_file(memory_id)
        if path is None:
            return None

        text = path.read_text(encoding="utf-8")
        fm, body = self._parse_file(text)

        if log_access:
            self.log_event("access.jsonl", {
                "memory_id": memory_id,
                "action": "read",
            })
        return fm, body

    def list_all(self, type_filter: MemoryType | None = None) -> list[MemoryFrontmatter]:
        with self._fm_cache_lock:
            return list_frontmatter(self._store_dir, type_filter, _cache=self._fm_cache)

    def list_active_directives(
        self,
        *,
        importance_floor: int = DIRECTIVES_IMPORTANCE_FLOOR,
        types: tuple[MemoryType, ...] = (MemoryType.FEEDBACK, MemoryType.USER),
    ) -> list[MemoryFrontmatter]:
        """Active operator-directive records ranked for the system prompt.

        Includes ``feedback`` and ``user`` records by default — the operator
        often saves a durable preference under ``user`` (e.g.
        ``mem_66d4e50b`` "Inline preview by default") rather than ``feedback``.
        Both shapes describe rules the assistant should obey across sessions, so both
        flow into the Operator Directives section.

        Filter: ``type in types``, ``stability == active``, ``importance >= floor``.
        Sort: importance desc, then created_at desc (newest tiebreak).
        Dedup: walk the sorted list — a record is dropped when its id is
        already kept (exact duplicate) OR when it has no slug AND its
        ``auto_links`` set intersects the kept id-set (librarian-merged
        variant of a rule already represented). Slug-keyed records describe
        distinct operator decisions and survive the auto_links check
        unconditionally — cross-references between two slug-keyed records
        are nearly always topical, not duplicate-markers. If two records
        share the same slug (no write-side uniqueness enforcement), both
        survive — duplication is preferable to silent eviction.
        """
        records: list[MemoryFrontmatter] = []
        for mt in types:
            records.extend(
                fm for fm in self.list_all(mt)
                if fm.stability == Stability.ACTIVE and fm.importance >= importance_floor
            )
        records.sort(
            key=lambda fm: (-fm.importance, -fm.created_at.timestamp()),
        )
        kept: list[MemoryFrontmatter] = []
        kept_ids: set[str] = set()
        for fm in records:
            if fm.id in kept_ids:
                continue
            related = set(fm.auto_links or [])
            # Only no-slug records can be auto-deduped via auto_links — a
            # slug marks a distinct operator decision (unique-by-construction)
            # and auto_links from it are cross-refs, not duplicate markers.
            if not fm.slug and related & kept_ids:
                continue
            kept.append(fm)
            kept_ids.add(fm.id)
            # Propagate outbound auto_links into kept_ids only for no-slug
            # records — when a slug-keyed record is kept, its auto_links
            # point at other distinct decisions that must remain eligible.
            if not fm.slug:
                kept_ids.update(related)
        return kept

    @staticmethod
    def _read_frontmatter_block(path: Path) -> str:
        """Read only the YAML frontmatter block, not the full file body."""
        with path.open("r", encoding="utf-8") as f:
            lines: list[str] = []
            first_line = f.readline()
            if first_line.strip() != "---":
                raise ValueError("Memory file missing YAML frontmatter")
            lines.append(first_line)
            for line in f:
                lines.append(line)
                if line.strip() == "---":
                    break
            return "".join(lines)

    def delete(self, memory_id: str) -> bool:
        path = self.find_file(memory_id)
        if path is None:
            return False

        path.unlink()
        with self._fm_cache_lock:
            self._fm_cache.pop(str(path), None)
        self.log_event("writes.jsonl", {
            "memory_id": memory_id,
            "status": "deleted",
        })
        logger.info("Memory %s deleted", memory_id)
        cascaded = self._cascade_deleted_id(memory_id)
        if cascaded:
            logger.info("Memory %s cascade cleaned %d entries", memory_id, cascaded)
        _publish_bus_event(
            "memory_deleted", {"id": memory_id, "cascaded": cascaded}
        )
        return True

    def _cascade_deleted_id(self, deleted_id: str) -> int:
        """Strip `deleted_id` from every other entry's frontmatter link lists
        and re-render its auto-managed ``## Related`` block.

        Closes the dangling-ref class of `memory_lint` findings: deleting a
        memory leaves stale `auto_links`/`links` IDs and broken wikilinks
        inside the Related block of any entry that pointed at it. We touch
        only entries that actually reference the deleted id and only the
        block boundaries owned by the auto-linker — operator-written body
        wikilinks outside the markers stay intact so the lint can still
        surface them for an operator decision.

        Returns the number of entries rewritten.
        """
        touched = 0
        for fm in self.list_all():
            if fm.id == deleted_id:
                continue
            auto_links = list(fm.auto_links or [])
            links = list(fm.links or [])
            if deleted_id not in auto_links and deleted_id not in links:
                continue

            new_auto_links = [x for x in auto_links if x != deleted_id]
            new_links = [x for x in links if x != deleted_id]

            read_result = self.read(fm.id, log_access=False)
            if read_result is None:
                continue
            _, body = read_result

            items: list[RelatedItem] = []
            for lid in new_auto_links:
                nbr = self.read(lid, log_access=False)
                title = nbr[0].title if nbr is not None else ""
                items.append((lid, title))

            updated_fm = fm.model_copy(
                update={"auto_links": new_auto_links, "links": new_links}
            )
            new_body = replace_related_block(body, items)
            if self.write(updated_fm, new_body):
                touched += 1
                self.log_event("writes.jsonl", {
                    "memory_id": fm.id,
                    "status": "cascaded",
                    "removed_ref": deleted_id,
                })
        return touched

    def _validate_relative_path(self, relative_path: str) -> Path:
        """Resolve a relative path and verify it stays within store_dir."""
        path = (self._store_dir / relative_path).resolve()
        store_resolved = self._store_dir.resolve()
        if not str(path).startswith(str(store_resolved) + os.sep) and path != store_resolved:
            raise ValueError(f"Path escapes store boundary: {relative_path}")
        return path

    def read_file(self, relative_path: str) -> str | None:
        """Read an arbitrary file relative to store_dir. Returns None if missing."""
        path = self._validate_relative_path(relative_path)
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8")

    def append_to_file(self, relative_path: str, content: str) -> None:
        """Append content to a file relative to store_dir. Creates if missing."""
        path = self._validate_relative_path(relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(content)

    def list_daily_notes(self) -> list[Path]:
        """Return daily note paths sorted newest-first.

        Reads from `daily/` — the F1 raw-capture layer (`_ensure_dirs`
        creates it). Pre-reboot `memory/` is retired.
        """
        daily_dir = self._store_dir / "daily"
        if not daily_dir.exists():
            return []
        return sorted(daily_dir.glob("????-??-??.md"), reverse=True)

    def archive_file(self, src: str, dest: str) -> bool:
        """Move a file from src to dest (both relative to store_dir). Returns True on success."""
        src_path = self._validate_relative_path(src)
        if not src_path.exists():
            return False
        dest_path = self._validate_relative_path(dest)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src_path), str(dest_path))
        return True

    @staticmethod
    def _split_frontmatter(text: str) -> tuple[dict, str]:
        """Return `(yaml_dict, body)` for a memory-file text. Body may be empty
        if the caller only needs frontmatter."""
        text = text.replace("\r\n", "\n")
        if not text.startswith("---\n"):
            raise ValueError("Memory file missing YAML frontmatter")
        end = text.index("---\n", 4)
        yaml_dict = yaml.safe_load(text[4:end])
        body = text[end + 4:].strip()
        return yaml_dict, body

    @classmethod
    def _parse_file(cls, text: str) -> tuple[MemoryFrontmatter, str]:
        yaml_dict, body = cls._split_frontmatter(text)
        return MemoryFrontmatter.from_yaml_dict(yaml_dict), body

    @classmethod
    def _parse_frontmatter_only(cls, text: str) -> MemoryFrontmatter:
        yaml_dict, _ = cls._split_frontmatter(text)
        return MemoryFrontmatter.from_yaml_dict(yaml_dict)

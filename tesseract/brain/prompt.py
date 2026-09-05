"""System prompt assembly for the assistant.

Reads the declarations in `workspace/` and composes a system prompt.

Three documents are inlined on every turn — SOUL.md (who it is and how it
sounds), USER.md (what it has learned about the operator), OPERATING.md
(how it works) — plus the active-project block, the tool map, and a memory
capsule (MEMORY.md synthesis + today's and yesterday's
`memory-store/daily/*.md` captures).

`SECTIONS` is the whole assembly. It names every block in append order, which
group it belongs to, whether it is volatile, and the builder that produces it.
Nothing is appended outside it — the old imperative sequence let a section
arrive without anyone asking whether it had earned its place, and let the
policy live in a second list that had to be kept in step by hand.

**Most of the head is frozen for the life of a conversation.** Caching is a
prefix match: a block that differs from one request to the next invalidates
everything after it, and everything after the head is the whole conversation.
So a held section is read once, at the start, and those exact bytes are sent
for as long as the conversation lasts (`ChatSession._held_sections`).

Holding had to become a mechanism, because the previous version was a hope.
`changes="never"` was not enforced anywhere: `_document`'s builder re-reads its
file on every assembly, so one save through Identity -> Documents moved bytes
at the top of the prefix and re-read the whole conversation behind them. Eight
of the ten head sections could do that. Two were held.

What a freeze gives up is an edit landing mid-conversation, and a section is
held only if it says which of these answers covers it — `hold_reason`,
declared beside the builder. Saying nothing is a legitimate answer and means
the section is NOT held, which is the safe default: the cost of staying silent
is a rebuild nobody notices, and the cost of a wrong claim would be an edit
nobody sees.

- **An operator act retires it.** `bump_head_revision()`, called from
  `workspace_changes.apply_change`, the one funnel every approved document
  write passes through, and the only place that KNOWS, because further down an
  approved edit and a background job rewriting the same file are the same
  bytes. The three inlined documents ride on this.
- **Nobody asked it to move, and it is reachable another way.** The memory
  capsule and the diary digest. `memory_search` and `file_read` fetch anything
  newer the moment the assistant wants it, which is what makes holding them
  safe rather than merely cheap.

There was a third answer here — "nothing in the process can move it" — and it
was claimed for the tool map and the channel overlay. Both claims were wrong,
and they are worth keeping as the warning this field exists for. The tool map
holds its NAMES still but renders `tool.tier`, which `Settings -> Tools`
reassigns live. `CHANNEL.md` was the one workspace document no list named, so
an approved write edited it under a running backend; it is a document like the
other three now, and the reason its section still is not held is in the section
itself. A `hold_reason` is prose and no guard can go and check it, so the only
defence is that it is written where the section is declared, by someone who has
just had to look.

**A section with no such answer is not held**, and says so by leaving
`hold_reason` empty — four of them do. The saved directives come out of the
memory store, the pointer list reads `workspace/skills/`, and the two above;
no write path passes through the funnel, so holding any of them swallowed the
operator's own edits for the rest of a conversation with nothing anywhere
saying so. They are rebuilt every turn,
which costs nothing while nothing changes: identical bytes leave the cached
prefix exactly where it was, and the only turn that pays is the one after an
edit actually landed.

A fold releases every hold: compaction discards the cached prefix anyway, so a
fresh read is free there.

`changes` still records WHO moves each block, because that is worth knowing
and a reader has no other way to find out. It no longer decides placement on
its own. Two sections leave the head:

- `changes="every_turn"` — the clock and the autonomy digest. They differ on
  every single turn, so they can never be held and never be cached.
- `rides_late=True` — the active project, and only it. `project_open` has to
  reach the NEXT turn (`chat.py::_head_for_turn` invariant 3), and a held head
  would answer with the project the operator just left until the conversation
  ended. Riding late means paying for it on every turn, which was affordable
  only because it was measured: 330 characters against a break-even near 2,600
  tokens.

Both ride AFTER everything else in the request, past the conversation and past
the operator's own message, where what they change has nothing behind it to
invalidate.

`assemble_system_prompt` returns a `PromptParts` carrying both halves; a caller
that only wants the text gets the whole prompt, exactly as before, because a
`PromptParts` IS the whole prompt.

A wrong call here is silent in every direction. Send a block late that never
moves and the cached prefix is merely shorter than it could be. Send one in
the head that differs every turn and the whole conversation behind it is
re-read every turn, at full price, with nothing anywhere saying so — that
mistake cost 952,098 tokens in one measured session before the clock was moved
out of the head. Hold something the operator moves without a matching
`bump_head_revision` at its write site and the edit is swallowed for the rest
of the conversation with the operator told nothing, which is the defect
`fix_pass_2026_04_24/test_prompt_rebuild.py` exists to close. A new write path
to a head document owes that bump.

**Nothing here is dropped, trimmed or truncated at assembly time.** Every
section in `SECTIONS` is sent, every turn. Size is a thing to OPTIMISE,
measured by `prompt_payload.py` and shown in Conscience, not a threshold
to amputate on: a character ceiling costs a channel its retrieved memory
and costs every turn its cacheable prefix, to save room a 400,000-token
window was never short of.

The one cap that remains is where the content is BUILT: `PER_FILE_CAP` =
12_000 bounds the memory capsule's parts, whose `TOTAL_CAP` is 60_000. That
is a decision about how much memory is worth retrieving, taken by the code
that retrieves it, and it is the right place for it.

If any workspace file is missing, degrade gracefully: skip that section
and log a warning. Never raise.

Module-size cleanup (Task 7.5) split this file's supporting responsibilities
into sibling modules — this file keeps the orchestrating entry point
(`assemble_system_prompt`), the section roster, and the "Right
now" temporal block (kept here, not in `prompt_time.py`, because several
tests patch `_now_local` / `_identity_config_path` / `_TEMPORAL_FALLBACK_WARNED`
directly on `tesseract.brain.prompt` — see `prompt_time.py`'s docstring):

- `prompt_rules.py` — operating-rules loader + the `# Active project` block.
- `prompt_content.py` — file-read helpers, manifest/skills pointers, memory
  capsule, diary digest, operator directives, channel overlay.
- `prompt_autonomy.py` — the agenda/self-reflection/failures digest.
- `prompt_time.py` — time-of-day bucketing, age, identity-config loading,
  conscience drift snippet.

All of the above are re-exported here so historical import paths
(`from tesseract.brain.prompt import X`) keep resolving.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from tesseract.lib.atomic_replace import replace_with_retry
from tesseract.paths import home_dir

from tesseract.brain.prompt_autonomy import (
    AUTONOMY_DIGEST_LEAD,
    OPEN_AGENDA_STATUSES,
    _build_autonomy_digest_section,
    _ranked_agenda_reader,
    _read_agenda_entries,
    _read_failures_snapshot,
)
from tesseract.brain.prompt_content import (
    CHANNEL_OVERLAY_HEADER,
    DAILY_FILES_TO_LOAD,
    DIARY_DIGEST_CHAR_BUDGET,
    DIARY_DIGEST_DAYS,
    DIRECTIVES_CHAR_BUDGET,
    MEMORY_CAPSULE_TOTAL_CAP,
    PER_FILE_CAP,
    SOURCE_ROLLUPS_TO_LOAD,
    TOPIC_HUBS_TO_LOAD,
    _build_diary_digest,
    _build_directives_section,
    _build_manifest_block,
    _build_skills_block,
    _build_memory_capsule,
    _read_capped,
    _read_file,
    _section,
    _strip_frontmatter,
    build_channel_overlay,
)
from tesseract.brain.prompt_rules import _build_project_block
from tesseract.brain.prompt_time import (
    _DEFAULT_TOD_BUCKETS,
    _default_conscience_dir,
    age_from_iso,
    _compute_age_days,
    _drift_snippet,
    _load_identity_config,
    _load_last_line,
    _parse_hhmm,
    _time_of_day_bucket,
)

logger = logging.getLogger(__name__)

def _default_memory_store() -> Path:
    """Call-time default memory-store root, honoring `TESSERACT_HOME`.

    Live production callers (`mirror/server/app.py::_build_chat_infra`,
    `mirror/server/session_factory.py`'s channel prompt builder) never
    pass `memory_store_dir` explicitly, so this default is what they
    actually read from every chat turn — it must follow the relocated
    home, not the code tree an app update wipes.
    """
    return home_dir() / "memory-store"


def _identity_config_path() -> Path:
    """Call-time resolution under `TESSERACT_HOME` so an operator's edits
    to a relocated `identity.yaml` aren't silently ignored after an app
    update replaces the code tree."""
    return home_dir() / "config" / "identity.yaml"


_TEMPORAL_FALLBACK_WARNED: bool = False


def _warn_temporal_fallback_once() -> None:
    """Emit the temporal-fallback exception at most once per process.

    `_build_now_section` is called per chat turn; a misconfigured identity.yaml
    would otherwise produce a stack trace every turn.
    """
    global _TEMPORAL_FALLBACK_WARNED
    if _TEMPORAL_FALLBACK_WARNED:
        return
    logger.exception("temporal_context: falling back to legacy now-section")
    _TEMPORAL_FALLBACK_WARNED = True


def _now_local() -> datetime:
    """Wall-clock local time (tz-aware). Indirection enables test patching."""
    return datetime.now().astimezone()


#: The labels the `Right now` block carries, in order. `OPERATING.md` teaches
#: the model how to read them and that paragraph is GENERATED from this tuple
#: — renaming a field here without the generator would leave the document
#: describing a key that never arrives, and the model quoting it back.
TEMPORAL_FIELDS: tuple[str, ...] = ("Today", "Local time", "Age")



def _build_glossary_section(registry: Any) -> str:
    """The tool map, rendered from whichever registry the caller could reach.

    Optional on purpose. Several entry points assemble a prompt with no
    registry in scope — `check_spoken_audio.py`, a boot-time frozen prompt, a
    test — and a prompt without the map is worse than one with it but not
    broken, while raising here would take the turn down. Same fail-open
    contract as every other block in this file.
    """
    if registry is None:
        return ""
    try:
        from tesseract.brain import glossary

        return glossary.render(registry)
    except Exception:
        logger.exception("glossary: could not render the tool map; prompt goes without it")
        return ""


def _resolve_registry(provider: Callable[[], Any] | None) -> Any:
    """The registry, or `None` — never an exception. A provider is a closure
    over app state that may not exist yet (`app.get("tool_registry")` during
    boot), which is a normal state rather than an error."""
    if provider is None:
        return None
    try:
        return provider()
    except Exception:
        logger.exception("prompt: tool registry unavailable; the map and the "
                         "schema budget go without it")
        return None


def _core_schema_chars(registry: Any) -> int:
    """Size of the tool schemas that ride EVERY turn beside this prompt.

    Core tier only. What a session has additionally unlocked through
    `tool_search` varies per session and is not knowable here — but the floor
    is, and the floor is what a fixed ceiling has to be measured against.
    """
    if registry is None:
        return 0
    try:
        import json

        return sum(
            len(json.dumps(schema))
            for schema in registry.schemas_for_adapter(enabled_extended=set())
        )
    except Exception:
        logger.exception("prompt: could not size the tool schemas; budgeting prose alone")
        return 0


def _build_now_section() -> str:
    """Ephemeral context block — rebuilt each call.

    Emits a `<temporal_context>` block (date, local time, time-of-day, age)
    plus the existing optional drift line. Kept small and at the end of the
    prompt so the static prefix stays cache-eligible. Fail-open: any error
    sourcing temporal data falls back to the legacy one-line `- Today:` form.

    Everything here comes from `identity.yaml`, which is authoritative for
    it. Reading SOUL.md's frontmatter instead would cost a YAML parse on
    every prompt build for a second copy of the same fields.
    """
    lines: list[str]
    try:
        cfg = _load_identity_config(_identity_config_path())
        buckets_raw = cfg.get("time_of_day_buckets")
        if not buckets_raw:
            raise ValueError("identity.yaml missing required key: time_of_day_buckets")
        buckets = {k: (v["start"], v["end"]) for k, v in buckets_raw.items()}
        born_at_iso = cfg.get("born_at")
        if not born_at_iso:
            raise ValueError("identity.yaml missing required key: born_at")

        now = _now_local()
        tod = _time_of_day_bucket(now, buckets=buckets)
        age_days = _compute_age_days(born_at_iso, now=now)
        born_date = datetime.fromisoformat(born_at_iso).date().isoformat()

        values = (
            f"{now:%Y-%m-%d %A}",
            f"{now:%H:%M} ({tod})",
            f"day {age_days} (born {born_date})",
        )
        lines = [f"- {label}: {value}"
                 for label, value in zip(TEMPORAL_FIELDS, values, strict=True)]
    except Exception:
        _warn_temporal_fallback_once()
        now = _now_local()
        lines = [f"- {TEMPORAL_FIELDS[0]}: {now:%Y-%m-%d %A}"]

    drift_line = _drift_snippet()
    if drift_line:
        lines.append(drift_line)
    return _section("Right now", "\n".join(lines))


@dataclass(frozen=True)
class PromptInputs:
    """Everything a section builder is allowed to read.

    Resolved once per assembly so no builder reaches for a default of its
    own; two builders resolving the memory store independently is how they
    come to disagree about which one they read.
    """

    workspace: Path
    memory_store: Path
    channel_name: str | None
    failures_scope: str | None
    registry: Any


@dataclass(frozen=True)
class Section:
    """One block of the system prompt.

    Every section is sent. There is no tier and no drop order: what is fed is
    decided where the content is written, and the Conscience payload panel
    reports the size so it can be optimised on purpose instead of amputated
    on a threshold.
    """

    name: str
    build: Callable[[PromptInputs], str]
    #: Which third of the payload this belongs to — `instructions`, `tools`
    #: or `memory`. Declared HERE, beside the builder, and not in a lookup
    #: table next to the readout: a map keyed by section name is a second
    #: list, and a section added to this one would quietly inherit whatever
    #: that map's default happened to be. `_GROUPS` is the closed set and
    #: boot raises on anything else.
    group: str
    #: What the payload readout calls it in the left column — a filename for
    #: an inlined document, a short noun phrase otherwise. Falls back to
    #: `name`, which is a key rather than a label.
    label: str = ""
    #: One plain sentence saying what the block IS, for the column beside the
    #: name. Declared here because the section is the only thing that knows;
    #: a table of descriptions kept next to the readout would describe
    #: whatever the block once was.
    description: str = ""
    #: The workspace file this section inlines whole, if it is one. Declared
    #: rather than inferred so the CI ceilings in
    #: `instruction_surface_IS_4` can read the roster instead of grepping
    #: this module's source for a path expression.
    document: str | None = None
    #: The cockpit view that shows where this block comes from, so the payload
    #: readout can offer a way to go and look at it. `None` where no surface
    #: exists yet — the readout renders those plain rather than as a link to
    #: nowhere. Same reasoning as `document`: the section knows its own
    #: provenance, and a table elsewhere claiming to would be free to be wrong.
    origin: str | None = None
    #: What the operator will find there, in words.
    origin_hint: str | None = None
    #: WHO moves what this section reads. One of `CHANGE_RATES`.
    #: Declared beside the builder, because the builder is the only thing that
    #: knows what it reads — a list of names kept elsewhere would be free to be
    #: wrong, and wrong here is silent.
    #:
    #: The axis is not speed, it is agency, and that is the whole point: two
    #: blocks can move exactly as often and still belong in different places,
    #: because one moved because the operator asked and the other moved
    #: because a background job finished.
    #:
    #: `"never"`   — a workspace document or the tool map. Byte-identical for
    #:               the life of the process.
    #: `"on_write"` — the operator moved it: a document edit, a directive
    #:               edit, `project_open`. Between acts the rebuild produces
    #:               the SAME BYTES, which is why one of these can sit in the
    #:               head at all: a prefix cache asks what the bytes are, not
    #:               how they were produced. Whether it is then HELD is a
    #:               separate question with its own answer, `hold_reason` —
    #:               the directives are on_write and are NOT held, because
    #:               nothing at their write site retires a hold.
    #: `"in_background"` — nobody asked: the memory capsule and the diary
    #:               digest move when a consolidation job writes, and they
    #:               would move mid-answer. Rides in the head and is HELD for
    #:               the conversation, because the alternative is a 200,000
    #:               token re-read bought by 7,168 tokens of memory that
    #:               `memory_search` can fetch on demand anyway. A date
    #:               rollover moves these too, for the same nobody's reason,
    #:               and the hold covers that as well.
    #: `"every_turn"` — the clock and the autonomy digest. These cannot be in
    #:               the head at any price: a block that differs every turn
    #:               invalidates everything after it, and everything after the
    #:               head is the whole conversation.
    changes: str = "never"
    #: Ride below the cache boundary even though the rate does not say to.
    #: For the ONE case the hold cannot cover: a block whose move has to reach
    #: the NEXT turn rather than the next conversation. `project_open` is that
    #: case, and it is the only one — an assistant still naming the old project
    #: after being asked to switch is wrong in a way no cache saving pays for.
    #:
    #: The next turn, not the turn that called it. A turn assembles its prompt
    #: once and reuses it across tool iterations (`chat.py::_turn_prompt`), so
    #: a mid-turn `project_open` is read by the turn after it. That predates
    #: this block moving and `chat.py` invariant 3 is where it is stated.
    #:
    #: Declared rather than derived because it is a placement decision made
    #: against a measured size, and the rate is a statement about WHO moves
    #: the block. Conflating them would have meant calling the project block
    #: `every_turn`, which is false: nobody moves it most turns.
    rides_late: bool = False
    #: Why a conversation may keep this section's first read, in a few words.
    #: The answer is the only thing that makes a hold safe, and it is different
    #: for each section: an operator act retires it, or nothing in the process
    #: can move it, or it moves for nobody's reason and is reachable another
    #: way.
    #:
    #: Silence is not an error, it is the safe default. Boot cannot check this
    #: — it is prose, and no guard can read it and go and see whether the
    #: funnel it names exists — so the failure it protects against is a
    #: section held on a claim nobody made. A blank means rebuilt every turn,
    #: which no one has to notice.
    #:
    #: Empty means NOT held: the section is rebuilt every turn and an edit to
    #: it lands on the next one. That is the right answer for a block whose
    #: writes do not pass through a funnel that calls `bump_head_revision`,
    #: and it costs nothing when nothing changed, because a rebuild that
    #: produces the same bytes leaves the cached prefix intact.
    hold_reason: str = ""

    @property
    def volatile(self) -> bool:
        """Whether this section leaves the system prompt and rides last.

        `"every_turn"` always does. Anything else rides last only by saying
        so: see `rides_late`.
        """
        return self.changes == "every_turn" or self.rides_late

    @property
    def held(self) -> bool:
        """Whether a conversation reads this once and keeps those bytes.

        A section in the head is held IF IT SAID WHY. `changes="never"` was
        never enforced and never could be: it is a claim about how often the
        operator acts, while every `never` section re-reads its file on each
        assembly, so one save through Identity -> Documents moved bytes at the
        top of the prefix and re-read the whole conversation behind it.

        Holding everything fixed that and broke something else. A hold is only
        safe when the operator's own edit can still get past it, and that needs
        a funnel at the write site calling `bump_head_revision`. Two sections
        have no such funnel — the directives come out of the memory store and
        the pointer list reads `workspace/skills/` — so holding them swallowed
        an edit with nothing anywhere saying so. They are not held, and they
        cost nothing to rebuild: identical bytes leave the cached prefix
        exactly where it was.

        So the question every section answers is `hold_reason`, and boot
        refuses a head section that does not answer it. The hold itself lives
        in `ChatSession`, which is the only thing that knows where a
        conversation begins.
        """
        return bool(self.hold_reason) and not self.volatile

    @property
    def display_label(self) -> str:
        return self.label or self.name


#: The three parts a payload divides into. Closed — a fourth would be a new
#: claim about how the prompt is read, not a detail.
GROUPS: tuple[str, ...] = ("instructions", "tools", "memory")

#: Who moves a section's content, least often first. Closed — a fifth rate
#: would be a new claim about where a block can ride, not a detail. What each
#: one means, and why only the last leaves the head, is on `Section.changes`.
CHANGE_RATES: tuple[str, ...] = (
    "never", "on_write", "in_background", "every_turn",
)


class PromptParts(str):
    """The whole prompt, and where it splits.

    A `str`, and that is the point. Five separate places wire a
    `prompt_builder` and one of them appends its own paragraph to what comes
    back; a return type that forced each of them to learn about two halves
    would be the same shape as the bug this phase was opened to fix, where two
    of three registration points were updated and the third was not. Every
    existing caller keeps getting the string it already got — the full prompt,
    both halves, joined the way `join_sections` joins anything.

    `head` and `late` are for the one caller that can act on the split,
    `ChatSession._messages_for_turn`. Anything that concatenates gets a plain
    `str` back and loses them, which degrades to sending the whole prompt as
    the system prompt: correct, just not cached as well as it could be.

    That degradation is silent, and concatenation is not the only way to
    reach it: `.strip()` and slicing do the same, quietly reverting a session
    to the pre-split behaviour with nothing in the payload readout to show
    it. Read `head`/`late` off what this returns; do not normalise it first.

    `copy` and `pickle` are NOT in that list, measured rather than assumed:
    both raise `TypeError` here, because they call `__new__` with no
    arguments and it requires two. Loud, so neither needs guarding against.
    """

    head: str
    late: str
    sections: dict[str, str]

    def __new__(
        cls, head: str, late: str, sections: dict[str, str] | None = None
    ) -> PromptParts:
        obj = super().__new__(cls, "\n\n".join(p for p in (head, late) if p))
        obj.head = head
        obj.late = late
        # Every block this assembly built, keyed by section name, so a caller
        # that holds one can re-join the head with its own bytes for it. Read
        # by `ChatSession` and nothing else; a caller that does not know about
        # it gets the head exactly as assembled, which is the whole prompt as
        # it always was. Empty (never absent) when a caller builds a
        # `PromptParts` from two strings, so the hold degrades to no hold
        # rather than to an exception.
        obj.sections = dict(sections or {})
        return obj


def _document(
    name: str,
    filename: str,
    *,
    title: str | None = None,
    description: str = "",
    hold_reason: str = "",
) -> Section:
    """A workspace document, inlined whole and never dropped.

    Read UNCAPPED. `_read_capped` truncates at `PER_FILE_CAP` and reports it
    at INFO, which is not a report anyone reads — `workspace/AGENTS.md` sat
    1,233 chars over and lost the rule that a new agent waits for operator
    approval, mid-word, for weeks. `instruction_surface_IS_4` holds each file
    to a stated ceiling in CI instead, where a build can fail rather than a
    sentence disappearing at runtime.
    """

    def build(ctx: PromptInputs) -> str:
        body = _strip_frontmatter(_read_file(ctx.workspace / filename))
        if not body:
            return ""
        return _section(title, body) if title else body

    # The filename IS the label. These are the three files the operator edits
    # under Identity -> Documents, and naming the row anything else would make
    # the reader translate before they can go and look.
    return Section(name, build, group="instructions", document=filename,
                   label=filename, description=description,
                   hold_reason=hold_reason,
                   origin="identity", origin_hint="Identity -> Documents")


def _build_manifest_section(ctx: PromptInputs) -> str:
    """Pointer block + the skills pointers.

    One section rather than two because they answer the same question — what
    exists that is not inlined here — and a reader who has one wants the
    other.
    """
    manifest = _build_manifest_block(ctx.workspace)
    skills = _build_skills_block(ctx.workspace)
    if not skills:
        return manifest
    return f"{manifest}\n\n{skills}" if manifest else skills


#: The whole assembly, in append order. Nothing is appended outside it, and
#: every entry is sent on every turn.
SECTIONS: tuple[Section, ...] = (
    # Who it is and how it sounds. IDENTITY.md merged in here: the two files
    # held each other's contents — IDENTITY carried the personality dials and
    # the register rules, which is what a soul file is FOR, while SOUL carried
    # a self-narrative that restated OPERATING.
    _document(
        "soul", "SOUL.md",
        description="How it sounds, and the register it holds to.",
        hold_reason="every write to this file goes through workspace_changes.apply_change, which bumps the head revision",
    ),
    _document(
        "operator", "USER.md", title="Operator",
        description="What it has learned about you, and keeps.",
        hold_reason="every write to this file goes through workspace_changes.apply_change, which bumps the head revision",
    ),
    # Named OPERATING.md rather than AGENTS.md because three unrelated files
    # carried that one name: the repo-root Codex conventions file (fixed by the
    # `agents.md` convention, so it is the one that could not move), this file,
    # and the `# Sub-agents` section inside it.
    _document(
        "operating", "OPERATING.md",
        description="How it works: the rules it follows on every turn.",
        hold_reason="every write to this file goes through workspace_changes.apply_change, which bumps the head revision",
    ),
    Section(
        "project", lambda ctx: _build_project_block(), group="instructions",
        label="active project",
        description="Which project you have open, and where it lives on disk.",
        origin="agents", origin_hint="Agents -> Projects",
        # Rebuilt from `ProjectStore().active()` on every assembly, and
        # `project_open` changes that store while the process runs, so
        # `on_write` is the rate that says so.
        changes="on_write",
        # The one block that cannot be held: `project_open` has to reach the
        # NEXT turn (`chat.py::_head_for_turn` invariant 3), and a held head
        # would answer with the project the operator just left until the
        # conversation ended. So it rides below the boundary instead, where it
        # is re-sent every turn and can invalidate nothing.
        #
        # The next turn, not this one. A turn assembles its prompt once and
        # reuses it across tool iterations (`chat.py::_turn_prompt`), so a
        # `project_open` called mid-turn is read by the turn after it. That is
        # the contract as stated, and it predates this block moving.
        #
        # Affordable because it is small, and that was measured rather than
        # assumed: 330 characters against a break-even near 2,600 tokens.
        # A block that grew past that should go back to being held, and the
        # operator told before it does.
        rides_late=True,
    ),
    # The tool map sits above "Right now" so it rides inside the cacheable
    # static prefix: static per build, so after the first turn of a cache
    # window it costs nothing to send.
    Section(
        "glossary", lambda ctx: _build_glossary_section(ctx.registry),
        group="tools", label="the tool map",
        description="Every tool it has, one line each, grouped by the question "
                    "each one answers.",
        origin="settings", origin_hint="Settings -> Tools",
        # NOT held, and the first draft of this said the opposite: "the
        # shipped roster cannot change inside a running process". That was
        # checked against which tool NAMES render() emits, and the names do
        # hold still. The rendered TEXT does not. `glossary.render` reads
        # `tool.tier` twice (the per-tool mark, and the "carried this turn"
        # count), `tier` is a mutable instance attribute, and
        # `boot._apply_tool_tiers` reassigns it live from Settings -> Tools
        # (`routes/workspace.py::_reload_tiers`) and from the Conscience
        # working-set route, neither of which bumps the revision.
        #
        # `routes/conscience.py` promises the operator "the change is live on
        # the next turn". Holding this block would have made that false for
        # every conversation already running.
    ),
    # Bounded where it is BUILT — `MEMORY_CAPSULE_TOTAL_CAP` and
    # `PER_FILE_CAP` in `prompt_content.py` — which is the only cap it has and
    # the only one it needs.
    Section(
        "capsule", lambda ctx: _build_memory_capsule(ctx.memory_store),
        group="memory", label="memory capsule",
        description="The slice of memory it starts the turn already holding: "
                    "your curated MEMORY.md, the last two days, and the "
                    "freshest few topic pages. Sized as the files read now. A "
                    "conversation already under way is carrying the copy it "
                    "read when it began.",
        # Nobody asks for this to move. A consolidation job writes, a day
        # rolls over, and 7,168 tokens at the very front of the prompt differ
        # from what the provider has cached — which re-reads the entire
        # conversation behind it at full price, mid-answer, for memory the
        # assistant did not ask for and can fetch with `memory_search` the
        # moment it wants it. So it is read once per conversation and held.
        changes="in_background",
        hold_reason="nobody asked it to move and memory_search reaches anything newer",
    ),
    Section(
        "diary", lambda ctx: _build_diary_digest(ctx.memory_store),
        group="memory", label="diary digest",
        description="A few lines on what it did over the last few days, in its "
                    "own words. Sized as the files read now, like the capsule "
                    "above, and held for a conversation the same way.",
        # Written by jobs that run mid-session, and unchanged between them.
        # Held with the capsule, and for its reason: the job that writes it
        # runs because a clock said so, not because the operator did.
        changes="in_background",
        hold_reason="nobody asked it to move and the diary is readable on request",
    ),
    # The operating rules survive inline in OPERATING.md; this block carries
    # operator-saved directives.
    Section(
        "directives", lambda ctx: _build_directives_section(ctx.memory_store),
        group="memory", label="saved directives",
        description="Standing instructions you saved that are not switched on "
                    "right now, listed so it knows they exist.",
        # The operator edits these expecting the next turn to obey, and an
        # edit is the only thing that moves them.
        changes="on_write",
        # NOT held, and no `hold_reason` is the mechanism that says so.
        #
        # These are built from `MemoryStore.list_active_directives()`, and a
        # memory is written by `memory_save`, by `memory_update`, by the
        # cockpit, and by the feedback consolidator. None of those passes
        # through `apply_change`, so nothing retires a hold on this block: it
        # was held for one commit and swallowed the operator's own edits for
        # the rest of a conversation with nothing anywhere saying so.
        #
        # Rebuilding costs nothing when nothing changed. The bytes are
        # identical, so the cached prefix is untouched, and the only turn that
        # pays is the one after an edit actually landed — which is the turn
        # that should pay.
    ),
    Section(
        "manifest", _build_manifest_section, group="instructions",
        label="pointers",
        description="A list of the files and skills it can open on request, "
                    "so it knows what exists without carrying any of it.",
        # NOT held, for `directives`' reason. The skills half reads
        # `workspace/skills/`, written through the file tools rather than
        # through `apply_change`, so a skill authored mid-conversation would
        # never reach the list that is supposed to name it.
    ),
    # The overlay rides BEFORE "Right now" so it stays inside the cacheable
    # static prefix. It is identical for every turn of a given channel adapter,
    # so it shares a cache slot with the base prompt. Cockpit
    # (`channel_name is None`) leaves the prompt unchanged.
    Section(
        "channel", lambda ctx: build_channel_overlay(ctx.channel_name)
        if ctx.channel_name is not None else "", group="instructions",
        label="surface contract",
        description="What this surface cannot physically carry, and nothing "
                    "else. Absent at the cockpit.",
        origin="channels", origin_hint="Channels",
        # NOT held, and it is the one section where that is a choice rather
        # than a property. CHANNEL.md is now a workspace document like the
        # three above it — named in `permissions.yaml::workspace_documents`,
        # in `PROPOSABLE_PATHS`, and unreachable by any write verb — so an
        # edit to it does bump the head. What it does not have is one head to
        # bump: the overlay is per CHANNEL, built from `ctx.channel_name`, and
        # a conversation on Telegram holds a different string from the one in
        # the cockpit. Rebuilding it every turn is cheaper than tracking which
        # of them a revision retires.
    ),
    # Agenda + self-reflection cross-feed. Adjacent to "Right now" so open
    # items and ambient failure signal reach every turn.
    Section(
        "digest", lambda ctx: _build_autonomy_digest_section(ctx.memory_store, ctx.failures_scope),
        group="memory", label="autonomy digest",
        description="What it has open on its own agenda, and anything that has "
                    "been failing.",
        origin="autonomy", origin_hint="Autonomy",
        # An agenda item closed by a background job during the session has to
        # reach the next turn, not the next restart — and the failures half
        # tracks a streak that moves inside a single turn. Nothing about it
        # holds still long enough for the head.
        changes="every_turn",
    ),
    Section(
        "now", lambda ctx: _build_now_section(), group="instructions",
        label="date & time",
        description="Today's date, the local time, and how long this instance "
                    "has been running.",
        origin="identity", origin_hint="Identity -> born_at",
        # A clock inside a frozen prefix is the whole bug this split exists
        # for: it changed the payload every minute and sent each turn to a
        # cache that had never seen it.
        changes="every_turn",
    ),
)


def _verify_sections() -> None:
    """Every section declares a group from the closed set.

    Import-time, because a section with a typo'd group would otherwise show up
    as an absent row in the payload readout — a section that silently is not
    counted, which is the failure this whole folder exists to stop. A typo'd
    change rate is worse than absent: `Section.volatile` compares against one
    exact string, so anything else silently means "never moves" and puts a
    block that does move in front of the whole conversation.

    A hold is not checked here and cannot be: `hold_reason` is prose, and no
    guard can read it and decide whether the funnel it names exists. What is
    checked is that a head section made the CLAIM, so the question is answered
    at the declaration by whoever added the section rather than discovered by
    an operator whose edit went missing. Leaving it blank is a legitimate
    answer and means the block is rebuilt every turn.
    """
    for section in SECTIONS:
        if section.group not in GROUPS:
            raise ValueError(
                f"prompt.SECTIONS: {section.name!r} declares group "
                f"{section.group!r}; must be one of {GROUPS}"
            )
        if section.changes not in CHANGE_RATES:
            raise ValueError(
                f"prompt.SECTIONS: {section.name!r} declares change rate "
                f"{section.changes!r}; must be one of {CHANGE_RATES}"
            )
        # A section that rides below the boundary is re-sent every turn and
        # can hold nothing, so a reason there would be a claim about a hold
        # that does not exist.
        if section.volatile and section.hold_reason:
            raise ValueError(
                f"prompt.SECTIONS: {section.name!r} rides late and cannot be "
                f"held, so it must not declare a hold_reason"
            )


_verify_sections()


#: The sections a conversation reads once and keeps, derived from the roster
#: rather than listed again here. A second list is how a section would quietly
#: not be held, or worse, how one moved below the boundary would go on being
#: held after the declaration beside its builder said to stop.
HELD_SECTION_NAMES: tuple[str, ...] = tuple(
    s.name for s in SECTIONS if s.held
)

#: A COUNTER ON DISK, bumped when the operator changes something the head
#: inlines. A conversation holds its head against this number, so a deliberate
#: act lands on the next turn and nothing else does.
#:
#: On disk rather than in memory, and that is not an optimisation. The
#: supervisor spawns the agent controller as a separate OS process
#: (`supervisor/daemon.py`, `controller_daemon_enabled` defaults on), so a
#: module global is per interpreter: a document approved in the Mirror backend
#: could never retire a held head in the controller, and those conversations
#: would answer from a stale SOUL.md until the daemon restarted. The filesystem
#: is the state both processes already share.
#:
#: A number rather than a broadcast to live sessions, because every session has
#: to agree and none of them should have to be found: a fold, a reconnect, a
#: channel with no socket and a chat nobody has opened yet all read the same
#: file. One small read per turn, against ten the assembly already does.
#:
#: Bumped at the WRITE SITE and only where the operator acted. The bytes cannot
#: tell an approved edit from a consolidation job rewriting the same file, so
#: nothing downstream can make this distinction; `workspace_changes.apply_change`
#: can, and is the one funnel every approved document write already passes
#: through.
_REVISION_FILE = "head-revision"

#: The last value successfully read in THIS process. What an unreadable file
#: degrades to, and the direction of that degradation is the whole point: a
#: reader that invented a new number on every failure would refresh the head
#: every turn and destroy the cache this exists to protect. Holding a stale
#: number keeps the conversation exactly as correct as it was before any of
#: this was built.
_last_good_revision = 0
_revision_lock = threading.Lock()


def _revision_path() -> Path:
    """Resolved at call time, so a test pointing `TESSERACT_HOME` somewhere
    else is answered by that home rather than the one imported at boot."""
    from tesseract.paths import runtime_dir

    return runtime_dir() / _REVISION_FILE


def head_revision() -> int:
    """The current head revision, read from disk.

    Every failure returns the last value this process read successfully, so an
    unreadable file means conversations keep the heads they are holding. That
    is the safe direction: the cost is an operator edit landing late, which is
    what happened before this mechanism existed, rather than every turn in
    every chat re-reading its whole prompt.
    """
    global _last_good_revision
    try:
        text = _revision_path().read_text(encoding="utf-8").strip()
    except OSError:
        return _last_good_revision
    try:
        _last_good_revision = int(text)
    except ValueError:
        logger.warning(
            "head revision file is not a number (%s); held prompts will not "
            "notice your next document edit until it is deleted",
            _revision_path(),
        )
    return _last_good_revision


def bump_head_revision() -> None:
    """Retire every conversation's held head, because the operator acted.

    Deliberately not scoped to a section or a file. A hold is cheap to retake —
    the builders read the same files the un-held path read every turn — and a
    per-section counter would be a second roster to keep in step with the
    first.
    """
    global _last_good_revision
    with _revision_lock:
        path = _revision_path()
        try:
            current = int(path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            current = _last_good_revision
        nxt = current + 1
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # A unique name per call, not `<name>.tmp`. `_revision_lock` is
            # per interpreter and the whole point of this file is that there
            # is more than one; two processes bumping at once would otherwise
            # write the same tempfile and truncate each other.
            fd, tmp_name = tempfile.mkstemp(
                prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
            )
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(str(nxt))
            replace_with_retry(Path(tmp_name), path)
            # Only once the number is actually on disk. Advancing the local
            # fallback after a failed publish would claim a retirement that
            # no other process can see, and `head_revision` would overwrite
            # it with the stale disk value on its very next read anyway.
            _last_good_revision = nxt
        except OSError:
            logger.warning(
                "could not publish the head revision to %s; a conversation "
                "already running will keep the prompt it is holding, so this "
                "edit reaches it when it next starts a new chat",
                path, exc_info=True,
            )


def assemble_system_prompt(
    workspace_dir: Path | None = None,
    memory_store_dir: Path | None = None,
    channel_name: str | None = None,
    failures_scope: str | None = None,
    tool_registry_provider: Callable[[], Any] | None = None,
) -> PromptParts:
    """Compose the system prompt by walking `SECTIONS` in order.

    What is inlined, in what order, and what the budget may take back is
    the table — this function resolves the inputs, runs each builder once,
    and joins whatever came back non-empty.

    ``tool_registry_provider`` is resolved at call time and renders the tool
    map (`glossary.py`). Optional: an entry point with no registry in scope
    assembles a prompt without the map rather than failing.

    ``failures_scope`` selects which `ChatSession`'s tool-error streak the
    autonomy digest shows. When not
    given explicitly, falls back to `failures_signal.active_scope()` — the
    contextvar `ChatSession._current_system_prompt` binds around its
    `prompt_builder()` call — so a shared, session-agnostic `prompt_builder`
    closure (the cockpit case) still renders the calling session's own
    streak, never a concurrent chat's. Still `None` for a boot/frozen
    prompt assembled outside any per-turn call — no streak line, correct
    (no turn history yet).

    Returns a `PromptParts`, which IS the whole prompt as a string and
    additionally says where it splits. There is no second entry point for the
    split: one function assembles the prompt, and a caller either reads the
    string or reads the two attributes on it.
    """
    ctx = resolve_inputs(
        workspace_dir=workspace_dir,
        memory_store_dir=memory_store_dir,
        channel_name=channel_name,
        failures_scope=failures_scope,
        tool_registry_provider=tool_registry_provider,
    )
    built = build_sections(ctx)
    if not built:
        return PromptParts(
            "You are the assistant, the operator's personal AI assistant.", ""
        )
    return PromptParts(
        join_sections(built, volatile=False),
        join_sections(built, volatile=True),
        built,
    )


def resolve_inputs(
    *,
    workspace_dir: Path | None = None,
    memory_store_dir: Path | None = None,
    channel_name: str | None = None,
    failures_scope: str | None = None,
    tool_registry_provider: Callable[[], Any] | None = None,
) -> PromptInputs:
    """Everything the builders read, resolved once.

    Split out of `assemble_system_prompt` so `prompt_payload.py` measures the
    payload the turn would actually send rather than one assembled from its
    own idea of where the workspace is.
    """
    if workspace_dir is None:
        from tesseract.paths import workspace_dir as _resolve_workspace_dir
        root = _resolve_workspace_dir()
    else:
        root = workspace_dir

    if failures_scope is None:
        from tesseract.brain import failures_signal
        failures_scope = failures_signal.active_scope()

    return PromptInputs(
        workspace=root,
        memory_store=memory_store_dir or _default_memory_store(),
        channel_name=channel_name,
        failures_scope=failures_scope,
        registry=_resolve_registry(tool_registry_provider),
    )


def _state_root_pattern() -> re.Pattern[str] | None:
    """The state root, both spellings, as a pattern that ends at a boundary.

    Built per assembly rather than cached across them: `TESSERACT_HOME` is
    honoured at call time everywhere else in this runtime, and a cached root
    would answer for the wrong install after a test fixture or an update moved
    it. `build_sections` builds it ONCE and hands it to every section, because
    the alternative is a `stat` per section for an answer that cannot change
    inside one assembly.

    **The boundary is the whole reason this is a pattern and not a
    `str.replace`.** A plain replace rewrites a SIBLING whose name merely
    starts with the root's: with a root of `.../home`, the string
    `.../home-backup/x` became `.-backup/x`, corrupting a path that was never
    inside the state root and was nobody's leak. So the run must end where the
    root ends: at a separator, or at the end of the token.

    Case-insensitively on Windows, because the same directory spelled
    `C:\\Users` and `c:\\users` opens the same file and only one of them would
    have matched.

    **A trailing separator is stripped, and a bare filesystem root refuses.**
    `Path.resolve()` keeps the separator on a drive root, so a state root of
    `C:\\` produced a body already ending in one and then demanded ANOTHER
    separator after it: `C:\\workspace\\SOUL.md` matched nothing and the guard
    was silently off for every path on the machine. Stripping fixes the drive
    case (`C:` is a fine prefix). A POSIX `/` strips to nothing, and there is
    no useful answer there: every absolute path would be inside the state root
    and rewriting all of them would mangle the prompt to protect a
    configuration that is already broken. It says so once and stands down,
    which is the only honest option of the three.
    """
    from tesseract.paths import home_dir

    try:
        root = str(home_dir().resolve()).rstrip("\\/")
    except (OSError, RuntimeError, ValueError):
        return None
    if not root:
        logger.warning(
            "TESSERACT_HOME is a filesystem root, so every path on this "
            "machine is inside it and the prompt cannot be checked for one. "
            "Point it at a directory of its own."
        )
        return None
    forms = sorted({root, root.replace("\\", "/")}, key=len, reverse=True)
    body = "|".join(re.escape(form) for form in forms)
    flags = re.IGNORECASE if os.name == "nt" else 0
    # Two ways the root can end, and both are needed.
    #
    # A separator, which is CONSUMED, so what is left is relative rather than
    # rooted. Or nothing that could continue the NAME: the root was written on
    # its own, and it becomes `.`.
    #
    # "Nothing that could continue the name" rather than "whitespace or end",
    # because the block renders the root inside backticks and a stricter
    # boundary missed it. The excluded set is what a directory name may go on
    # with; a backtick, a quote, a comma or a bracket ends it.
    #
    # A space is NOT in the set, and that is a trade rather than an oversight.
    # It means a real sibling named `<root> Files` is rewritten to `. Files`,
    # the same class as the `-backup` defect above. The other side is that the
    # root written on its own in a sentence is the common case and requiring a
    # separator would miss all of it. A corrupted path is the safer of the two
    # failures, and a directory whose name is the state root's plus a space is
    # rarer than prose.
    return re.compile(rf"(?:{body})(?:[\\/]|(?![A-Za-z0-9_\-.+~]))", flags)


def scrub_state_root(text: str, pattern: re.Pattern[str] | None = None) -> str:
    """Rewrite any absolute path into the state root as a relative one.

    **Why this exists at all.** A source scanner refuses a home-directory shape
    in a docstring and nothing watched the string the runtime BUILDS. The same
    path is a leak in a prompt and unwatched, and it shipped once: an absolute
    workspace pointer went to whichever provider `chat_role` resolved to, on
    every turn, carrying the operator's home directory and username.

    **Why here and not at the one builder that was caught.** Seven of the
    twelve sections render text this runtime did not write: three inline a
    workspace document verbatim, four inline memory the assistant wrote about
    the operator. Any of them can carry the literal state root because a person
    or a past turn typed it there. Fixing the builder that leaked is fixing the
    instance; this is the class, and it is the only place every section passes.

    **Why rewriting rather than refusing or blanking.** A relative path is what
    the file tools anchor at the state root anyway, so the model loses nothing
    and arguably gains: `workspace/SOUL.md` is the form it would have to
    construct to read the file. Refusing would take a section the model needs
    off the prompt over a formatting problem. Blanking would edit the
    operator's own sentence into nonsense.

    Paths OUTSIDE the state root are untouched, and that is deliberate rather
    than an omission: where the operator keeps their work is a fact the model
    has to have, and the project block exists to tell it.

    **It rewrites the operator's own prose too, and that is the decision
    rather than an accident.** Three of the twelve sections inline a workspace
    document verbatim, so a sentence a person typed into USER.md naming the
    state root is rendered to the model with that path made relative. The
    alternative is exempting those three, which is a narrower rule than the
    one written here and reopens the half that matters: a person is exactly
    who writes an absolute path into a document, and a past turn of the
    assistant is the other. What is lost is a sentence reading slightly
    differently to the model than on disk. What would be lost the other way is
    the operator's home directory and username, on every turn.
    """
    if not text:
        return text
    if pattern is None:
        pattern = _state_root_pattern()
    if pattern is None:
        return text
    # `.` when the root was named on its own, nothing when a separator
    # followed it, so what is left reads as the relative path it now is.
    return pattern.sub(lambda m: "" if m.group(0)[-1] in "\\/" else ".", text)


def build_sections(ctx: PromptInputs) -> dict[str, str]:
    """Run every builder once. Keyed by section name; empties are absent.

    Every section meets `scrub_state_root` on the way out. That is the whole
    enforcement: one pass, over the composed text, in the one function every
    builder returns through.
    """
    pattern = _state_root_pattern()
    built: dict[str, str] = {}
    for section in SECTIONS:
        text = section.build(ctx)
        if text:
            built[section.name] = scrub_state_root(text, pattern)
    return built


def join_sections(built: dict[str, str], *, volatile: bool | None = None) -> str:
    """Append order is `SECTIONS`, never the dict's.

    ``volatile`` selects one half of the split: ``False`` is the frozen head,
    ``True`` is the block that rides late. ``None`` is both in one string,
    which is what a caller that measures or logs the payload wants and what
    every caller got before the split existed.
    """
    return "\n\n".join(
        built[s.name] for s in SECTIONS
        if s.name in built and (volatile is None or s.volatile is volatile)
    )



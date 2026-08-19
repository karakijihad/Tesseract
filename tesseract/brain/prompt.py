"""System prompt assembly for the assistant.

Reads the declarations in `workspace/` and composes a system prompt.

Three documents are inlined on every turn — SOUL.md (who it is and how it
sounds), USER.md (what it has learned about the operator), OPERATING.md
(how it works) — plus the active-project block, the tool map, and a memory
capsule (MEMORY.md synthesis + today's and yesterday's
`memory-store/daily/*.md` captures).

`SECTIONS` is the whole assembly. It names every block in append order,
which tier it rides (`PINNED` survives any budget, `DROPPABLE` is shed by
`order`, lowest first), and the builder that produces it. Nothing is
appended outside it — the old imperative sequence let a section arrive
without anyone asking whether it had earned its place, and let the drop
policy live in a second list that had to be kept in step by hand.

The "Right now" section (today's date) is always last so the static prefix
above it stays byte-identical and cache-eligible.

`PER_FILE_CAP` = 12_000 bounds the memory capsule's parts, whose
`TOTAL_CAP` is 60_000 — and nothing else. (The manifest block emits its own
description text rather than reading the files it points at, so there is
nothing there to cap.) The inlined documents are read uncapped: they are the
tier `_apply_total_budget` never drops, so a cap that silently cut them
instead would contradict that tier. `MAX_TOTAL_CHARS` is the one budget over
prose and schemas together.

If any workspace file is missing, degrade gracefully: skip that section
and log a warning. Never raise.

Module-size cleanup (Task 7.5) split this file's supporting responsibilities
into sibling modules — this file keeps the orchestrating entry point
(`assemble_system_prompt`), the total-budget enforcement, and the "Right
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
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable

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
    DIRECTIVES_BODY_PREVIEW_CHARS,
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
    _age_from_iso,
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


# Total assembled-prompt soft ceiling, over prose and tool schemas together.
# Which blocks are shed when it is exceeded, and in what order, is `SECTIONS`
# — stating it twice is how the two lists drifted apart before.
MAX_TOTAL_CHARS = 100_000

#: The labels the `Right now` block carries, in order. `OPERATING.md` teaches
#: the model how to read them and that paragraph is GENERATED from this tuple
#: — a field renamed here used to leave the document describing a key that
#: never arrives, and the model quoting it back.
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

    Everything here comes from `identity.yaml`. It used to take SOUL.md's
    frontmatter and had been deleting the argument unread since the config
    became authoritative — which cost a YAML parse of SOUL.md on every
    prompt build for nothing.
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
    own — two of them used to resolve the memory store independently.
    """

    workspace: Path
    memory_store: Path
    channel_name: str | None
    failures_scope: str | None
    registry: Any


class Tier(Enum):
    """Whether the budget may take a section away.

    Two values, not three. A section that is only *sometimes* present — the
    channel overlay off-channel, the diary on a fresh install, the operator
    block before USER.md exists — is a builder returning `""`, which every
    tier already handles. Conditional presence is not a budget policy, and
    giving it a tier of its own would have made "pinned" and "conditional"
    two names for identical behaviour.
    """

    PINNED = "pinned"
    DROPPABLE = "droppable"


@dataclass(frozen=True)
class Section:
    """One block of the system prompt.

    `order` is drop priority within `DROPPABLE`, lowest shed first. It is
    deliberately not the append order: the prompt reads best with the diary
    beside the memory capsule, and sheds best with the diary first.
    """

    name: str
    build: Callable[[PromptInputs], str]
    tier: Tier
    order: int = 0
    label: str = ""
    #: The workspace file this section inlines whole, if it is one. Declared
    #: rather than inferred so the CI ceilings in
    #: `instruction_surface_IS_4` can read the roster instead of grepping
    #: this module's source for a path expression.
    document: str | None = None
    #: Shed by TRIMMING to fit rather than by removal. Declared on the one
    #: section where the two differ enough to matter: the memory capsule is
    #: the largest block in the prompt and the last one shed, so an
    #: all-or-nothing drop pays 20,000 chars to recover a few hundred and
    #: leaves the turn well under budget with no memory at all. It is also
    #: ordered — MEMORY.md, then today, then yesterday, then the derived
    #: trees — so a cut from the tail loses the least valuable part first.
    trimmable: bool = False

    @property
    def drop_label(self) -> str:
        return self.label or self.name


def _document(name: str, filename: str, *, title: str | None = None) -> Section:
    """A workspace document, inlined whole and never dropped.

    Read UNCAPPED. `_read_capped` truncates at `PER_FILE_CAP` and reports it
    at INFO, which is not a report anyone reads — `workspace/AGENTS.md` sat
    1,233 chars over and lost the rule that a new agent waits for operator
    approval, mid-word, for weeks. These are `PINNED`, so a second budget
    that amputates them instead contradicts the tier they are in.
    `MAX_TOTAL_CHARS` is the one runtime budget; `instruction_surface_IS_4`
    holds each file to a stated ceiling in CI, where a build can fail instead
    of a sentence disappearing at runtime.
    """

    def build(ctx: PromptInputs) -> str:
        body = _strip_frontmatter(_read_file(ctx.workspace / filename))
        if not body:
            return ""
        return _section(title, body) if title else body

    return Section(name, build, tier=Tier.PINNED, document=filename)


def _build_manifest_section(ctx: PromptInputs) -> str:
    """Pointer block + the skills pointers, which ride its drop tier.

    One section rather than two because they are shed together: a skills
    list is a pointer list, and a payload that has run out of room for
    pointers has run out of room for both kinds.
    """
    manifest = _build_manifest_block(ctx.workspace)
    skills = _build_skills_block(ctx.workspace)
    if not skills:
        return manifest
    return f"{manifest}\n\n{skills}" if manifest else skills


#: The whole assembly, in append order. Nothing is appended outside it.
#:
#: `PINNED` is not a ranking of importance — it is the claim that losing the
#: block cannot be recovered by a tool call. A model that has lost track of
#: which project it is in, or that cannot name a capability, will not go
#: looking; a model without yesterday's diary can read it.
SECTIONS: tuple[Section, ...] = (
    # Who it is and how it sounds. IDENTITY.md merged in here: the two files
    # held each other's contents — IDENTITY carried the personality dials and
    # the register rules, which is what a soul file is FOR, while SOUL carried
    # a self-narrative that restated OPERATING.
    _document("soul", "SOUL.md"),
    _document("operator", "USER.md", title="Operator"),
    # Named OPERATING.md rather than AGENTS.md because three unrelated files
    # carried that one name: the repo-root Codex conventions file (fixed by the
    # `agents.md` convention, so it is the one that could not move), this file,
    # and the `# Sub-agents` section inside it.
    _document("operating", "OPERATING.md"),
    Section("project", lambda ctx: _build_project_block(), tier=Tier.PINNED),
    # The tool map sits above "Right now" so it rides inside the cacheable
    # static prefix: static per build, so after the first turn of a cache
    # window it costs nothing to send.
    Section("glossary", lambda ctx: _build_glossary_section(ctx.registry), tier=Tier.PINNED),
    # MEMORY.md synthesis + today/yesterday daily captures. Empty on a fresh
    # install. Last to be shed — the drop before identity is at risk.
    Section("capsule", lambda ctx: _build_memory_capsule(ctx.memory_store),
            tier=Tier.DROPPABLE, order=5, label="memory capsule", trimmable=True),
    Section("diary", lambda ctx: _build_diary_digest(ctx.memory_store),
            tier=Tier.DROPPABLE, order=1, label="diary digest"),
    # The operating rules survive inline in OPERATING.md; this block carries
    # operator-saved directives.
    Section("directives", lambda ctx: _build_directives_section(ctx.memory_store),
            tier=Tier.DROPPABLE, order=4, label="non-active directives"),
    Section("manifest", _build_manifest_section,
            tier=Tier.DROPPABLE, order=3, label="manifest pointers"),
    # The overlay rides BEFORE "Right now" so it stays inside the cacheable
    # static prefix. It is identical for every turn of a given channel adapter,
    # so it shares a cache slot with the base prompt. Cockpit
    # (`channel_name is None`) leaves the prompt unchanged.
    Section("channel", lambda ctx: build_channel_overlay(ctx.channel_name)
            if ctx.channel_name is not None else "", tier=Tier.PINNED),
    # Agenda + self-reflection cross-feed — useful, but re-derivable from a
    # tool call, so it sheds right after the diary. Adjacent to "Right now" so
    # open items and ambient failure signal reach every turn.
    Section("digest", lambda ctx: _build_autonomy_digest_section(ctx.memory_store, ctx.failures_scope),
            tier=Tier.DROPPABLE, order=2, label="autonomy digest"),
    Section("now", lambda ctx: _build_now_section(), tier=Tier.PINNED),
)

def _droppable() -> tuple[Section, ...]:
    """Drop priority, lowest first, read off `SECTIONS` on every call.

    Derived rather than declared, so the budget cannot hold an order the table
    disagrees with. Computed per call rather than once at import for the same
    reason: `assemble_system_prompt` reads `SECTIONS` live, so an import-time
    snapshot is a second list that drifts from the first the moment the table
    changes underneath it — a section added to the table would be built and
    then be invisible to the budget, which is the two-lists defect this table
    exists to end, one level down.
    """
    return tuple(
        sorted((s for s in SECTIONS if s.tier is Tier.DROPPABLE), key=lambda s: s.order)
    )


def _droppable_names() -> frozenset[str]:
    return frozenset(section.name for section in _droppable())


def assemble_system_prompt(
    workspace_dir: Path | None = None,
    memory_store_dir: Path | None = None,
    channel_name: str | None = None,
    failures_scope: str | None = None,
    tool_registry_provider: Callable[[], Any] | None = None,
) -> str:
    """Compose the system prompt by walking `SECTIONS` in order.

    What is inlined, in what order, and what the budget may take back is
    the table — this function resolves the inputs, runs each builder once,
    and joins whatever came back non-empty.

    ``tool_registry_provider`` is resolved at call time and renders the tool
    map (`glossary.py`). Optional: an entry point with no registry in scope
    assembles a prompt without the map rather than failing.

    ``failures_scope`` (whole-phase review fix, 2026-07-06) selects which
    `ChatSession`'s tool-error streak the autonomy digest shows. When not
    given explicitly, falls back to `failures_signal.active_scope()` — the
    contextvar `ChatSession._current_system_prompt` binds around its
    `prompt_builder()` call — so a shared, session-agnostic `prompt_builder`
    closure (the cockpit case) still renders the calling session's own
    streak, never a concurrent chat's. Still `None` for a boot/frozen
    prompt assembled outside any per-turn call — no streak line, correct
    (no turn history yet).
    """
    if workspace_dir is None:
        from tesseract.paths import workspace_dir as _resolve_workspace_dir
        root = _resolve_workspace_dir()
    else:
        root = workspace_dir
    store = memory_store_dir or _default_memory_store()

    if failures_scope is None:
        from tesseract.brain import failures_signal
        failures_scope = failures_signal.active_scope()

    ctx = PromptInputs(
        workspace=root,
        memory_store=store,
        channel_name=channel_name,
        failures_scope=failures_scope,
        registry=_resolve_registry(tool_registry_provider),
    )

    built: dict[str, str] = {}
    for section in SECTIONS:
        text = section.build(ctx)
        if text:
            built[section.name] = text

    if not built:
        return "You are the assistant, the operator's personal AI assistant."

    prompt = "\n\n".join(built[s.name] for s in SECTIONS if s.name in built)
    return _apply_total_budget(
        prompt,
        schema_chars=_core_schema_chars(ctx.registry),
        **{name: text for name, text in built.items() if name in _droppable_names()},
    )


def _apply_total_budget(prompt: str, *, schema_chars: int = 0, **blocks: str) -> str:
    """Enforce ``MAX_TOTAL_CHARS`` on everything that rides the turn.

    ``schema_chars`` is the size of the tool schemas the adapter sends
    alongside this prompt. They are not part of the string, and for a long
    time they were not part of the budget either — so a 100,000-char ceiling
    governed 67,000 chars of a 137,000-char payload, and the half that grew
    was the half nothing watched. The ceiling applies to the sum, which is
    what the model actually receives.

    ``blocks`` is keyed by section name, and only `DROPPABLE` names are
    accepted — handing this function a pinned section is the bug it now
    raises on rather than silently obeying. The order they are shed in comes
    from `SECTIONS`, not from this function; each drop is logged at WARNING
    so the operator can spot prompt-bloat creep.
    """
    unknown = sorted(set(blocks) - _droppable_names())
    if unknown:
        raise ValueError(
            f"_apply_total_budget: {unknown} is not a droppable section. "
            "Only sections declared DROPPABLE in SECTIONS may be shed."
        )
    ceiling = MAX_TOTAL_CHARS - max(schema_chars, 0)
    for section in _droppable():
        if len(prompt) <= ceiling:
            return prompt
        block = blocks.get(section.name, "")
        over_by = len(prompt) - ceiling
        if section.trimmable:
            prompt = _trim_block(prompt, block, section.drop_label, over_by=over_by)
        else:
            prompt = _drop_block(prompt, block, section.drop_label, over_by=over_by)
    if len(prompt) > ceiling:
        logger.warning(
            "prompt: %d chars + %d chars of tool schemas exceeds the %d "
            "budget after all soft drops — keeping pinned sections (identity "
            "preferred over budget)",
            len(prompt), schema_chars, MAX_TOTAL_CHARS,
        )
    return prompt


#: Left standing when a trimmable block is cut, so what survives is not read as
#: the whole of what was retrieved.
_TRIM_MARKER = "\n\n[…truncated to fit the context budget]"

#: Below this a trimmed block carries no useful memory and the marker is most of
#: it, so the block is dropped instead.
_TRIM_FLOOR = 1_000


def _trim_block(prompt: str, block: str, label: str, *, over_by: int) -> str:
    """Cut ``block``'s tail to recover ``over_by`` chars instead of removing it.

    Dropping is the wrong last resort for the largest block in the prompt: the
    capsule was shed whole — 22,587 chars — to recover 617, leaving the turn
    14,000 chars under the ceiling with no retrieved memory at all, on a
    channel and never at the cockpit. The same turn, trimmed, keeps all but
    the tail. What the tail holds is the derived trees, which
    `_build_memory_capsule` appends after MEMORY.md and the daily notes.

    Falls back to a full drop when what would survive is under `_TRIM_FLOOR`.
    """
    if not block or block not in prompt:
        return prompt
    keep = len(block) - over_by - len(_TRIM_MARKER)
    if keep < _TRIM_FLOOR:
        return _drop_block(prompt, block, label, over_by=over_by)
    head = block[:keep]
    cut = head.rfind("\n")
    if cut > _TRIM_FLOOR:
        head = head[:cut]
    trimmed = head.rstrip() + _TRIM_MARKER
    logger.warning(
        "prompt: %d chars over budget — trimming %s (%d → %d chars)",
        over_by, label, len(block), len(trimmed),
    )
    return prompt.replace(block, trimmed, 1)


def _drop_block(prompt: str, block: str, label: str, *, over_by: int) -> str:
    """Remove ``block`` from ``prompt`` exactly once.

    Tries the section-joined form (``\\n\\n`` + block) first; only falls
    back to a bare match if the joined form is not present. Avoids the
    double-replace footgun where chaining both replacements
    unconditionally can strip duplicate substrings from base content.
    """
    if not block or block not in prompt:
        return prompt
    with_join = "\n\n" + block
    if with_join in prompt:
        out = prompt.replace(with_join, "", 1)
    else:
        out = prompt.replace(block, "", 1)
    logger.warning(
        "prompt: %d chars over budget — dropping %s (%d chars)",
        over_by, label, len(block),
    )
    return out

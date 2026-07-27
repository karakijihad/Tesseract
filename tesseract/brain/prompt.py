"""System prompt assembly for TARS.

Reads the declarations in `workspace/` and composes a system prompt in one
of two modes:

- **manifest (default)** — inline IDENTITY.md, SOUL.md, USER.md, AGENTS.md
  (operating rules), plus a memory capsule (MEMORY.md synthesis + today's
  and yesterday's `memory-store/daily/*.md` captures) and a small pointer
  block for the remaining workspace files. TARS reads the pointed-at
  files via `file_read` on demand. Char caps prevent runaway size.

- **full** — inline every workspace file (IDENTITY, FOUNDATION, SOUL,
  USER, BOOT, AGENTS, TOOLS). Useful for debugging or when running
  against a model without tool support.

The "Right now" section (today's date) is always appended last so the
static prefix above it stays byte-identical and cache-eligible.

Char caps follow the OpenClaw agent-workspace defaults:
`PER_FILE_CAP` = 12_000 chars per file, `TOTAL_CAP` = 60_000 chars for
the memory-capsule block (identity/soul/user/agents are loaded unconditionally
since they are load-bearing for behavior; caps only apply to them as a
per-file truncation safeguard, not to the block total).

If any workspace file is missing, degrade gracefully: skip that section
and log a warning. Never raise.

Module-size cleanup (Task 7.5) split this file's supporting responsibilities
into sibling modules — this file keeps the orchestrating entry point
(`assemble_system_prompt`), the total-budget enforcement, and the "Right
now" temporal block (kept here, not in `prompt_time.py`, because several
tests patch `_now_local` / `_IDENTITY_CONFIG_PATH` / `_TEMPORAL_FALLBACK_WARNED`
directly on `tesseract.brain.prompt` — see `prompt_time.py`'s docstring):

- `prompt_rules.py` — operating-rules loader + the `# Trio` config block.
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
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from tesseract.brain.prompt_autonomy import (
    AUTONOMY_DIGEST_LEAD,
    OPEN_AGENDA_STATUSES,
    UNVETTED_AGENDA_STATUS,
    _build_autonomy_digest_section,
    _count_unvetted_agenda_items,
    _ranked_agenda_reader,
    _read_agenda_entries,
    _read_failures_snapshot,
    _read_reflection_entries,
)
from tesseract.brain.prompt_content import (
    CHANNEL_OVERLAY_HEADER,
    CHANNEL_OVERLAY_TEMPLATE,
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
    _parse_frontmatter,
    _read_capped,
    _read_file,
    _section,
    _strip_frontmatter,
    build_channel_overlay,
)
from tesseract.brain.prompt_rules import (
    RULES_DIR,
    _RULE_NAME_TO_FILE,
    _build_trio_block,
    _legacy_rule_attr,
    _load_rules,
)
from tesseract.brain.prompt_time import (
    _DEFAULT_CONSCIENCE_DIR,
    _DEFAULT_TOD_BUCKETS,
    _age_from_iso,
    _compute_age_days,
    _drift_snippet,
    _load_identity_config,
    _load_last_line,
    _parse_hhmm,
    _time_of_day_bucket,
)

logger = logging.getLogger(__name__)

_DEFAULT_MEMORY_STORE = Path(__file__).resolve().parent.parent / "memory-store"
_IDENTITY_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "identity.yaml"

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


PromptMode = Literal["manifest", "full"]

# CR-2 — total assembled-prompt soft ceiling. When exceeded,
# lower-priority blocks are dropped in this order: diary digest,
# autonomy digest, manifest pointers, non-active directives, memory
# capsule. The base identity sections (IDENTITY / SOUL / USER / AGENTS /
# rules / now) are NEVER dropped — truncating identity is worse than
# overrunning the budget. See `_apply_total_budget` for the exact order.
MAX_TOTAL_CHARS = 100_000

# Lazy back-compat for the legacy `_*_TEXT` rule constants (CR-2). See
# `prompt_rules._legacy_rule_attr` for the resolution logic.
__getattr__ = _legacy_rule_attr


def _build_now_section(soul_frontmatter: dict[str, Any]) -> str:
    """Ephemeral context block — rebuilt each call.

    Emits a `<temporal_context>` block (date, local time, time-of-day, age)
    plus the existing optional drift line. Kept small and at the end of the
    prompt so the static prefix stays cache-eligible. Fail-open: any error
    sourcing temporal data falls back to the legacy one-line `- Today:` form.
    """
    del soul_frontmatter
    lines: list[str]
    try:
        cfg = _load_identity_config(_IDENTITY_CONFIG_PATH)
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

        lines = [
            f"- Today: {now:%Y-%m-%d %A}",
            f"- Local time: {now:%H:%M} ({tod})",
            f"- Age: day {age_days} (born {born_date})",
        ]
    except Exception:
        _warn_temporal_fallback_once()
        now = _now_local()
        lines = [f"- Today: {now:%Y-%m-%d %A}"]

    drift_line = _drift_snippet()
    if drift_line:
        lines.append(drift_line)
    return _section("Right now", "\n".join(lines))


def assemble_system_prompt(
    workspace_dir: Path | None = None,
    memory_store_dir: Path | None = None,
    mode: PromptMode = "manifest",
    channel_name: str | None = None,
    failures_scope: str | None = None,
) -> str:
    """Compose the system prompt from workspace declarations + memory capsule.

    In `manifest` mode (default), inlines IDENTITY + SOUL + USER + AGENTS
    (operating rules) + MCP + memory capsule (MEMORY.md + today/yesterday
    daily) + a pointer block for FOUNDATION / TOOLS / HEARTBEAT / WORKSHOP /
    BOOT / any `workspace/skills/*/SKILL.md`. TARS reads the pointed-at
    files with `file_read` when relevant.

    In `full` mode every workspace file is inlined (legacy behavior, useful
    for models without tool support or for debugging). Memory capsule still
    applies.

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
    store = memory_store_dir or _DEFAULT_MEMORY_STORE
    sections: list[str] = []

    if failures_scope is None:
        from tesseract.brain import failures_signal
        failures_scope = failures_signal.active_scope()

    identity = _strip_frontmatter(_read_capped(root / "IDENTITY.md"))
    if identity:
        sections.append(identity)

    if mode == "full":
        foundation = _strip_frontmatter(_read_capped(root / "FOUNDATION.md"))
        if foundation:
            sections.append(foundation)

    soul_raw = _read_capped(root / "SOUL.md")
    soul_front = _parse_frontmatter(soul_raw)
    soul_body = _strip_frontmatter(soul_raw)
    if soul_body:
        sections.append(soul_body)

    user = _strip_frontmatter(_read_capped(root / "USER.md"))
    if user:
        sections.append(_section("Operator", user))

    # Operating rules are load-bearing — inline in both modes (OpenClaw AGENTS).
    agents = _strip_frontmatter(_read_capped(root / "AGENTS.md"))
    if agents:
        sections.append(agents)

    # CR-2 — operating rules loaded from `tesseract/brain/rules/*.md` in
    # name-sorted (numbered) order. Same order as the 15 inline constants
    # that used to live here.
    sections.extend(_load_rules(RULES_DIR))

    # trio W2 — config-driven companion to the trio-verification rules card:
    # lane names/kinds + relay tunables rendered from cockpit.yaml so the
    # card stays value-free (config is SoT; roles are pillars). Static per
    # config edit → rides the cacheable prefix with the rules.
    trio_block = _build_trio_block()
    if trio_block:
        sections.append(trio_block)

    if mode == "full":
        boot = _strip_frontmatter(_read_capped(root / "BOOT.md"))
        if boot:
            sections.append(boot)
        tools = _strip_frontmatter(_read_capped(root / "TOOLS.md"))
        if tools:
            sections.append(tools)
    else:
        mcp = _strip_frontmatter(_read_capped(root / "MCP.md"))
        if mcp:
            sections.append(mcp)

    # Memory capsule — MEMORY.md synthesis + today/yesterday daily captures.
    # Applies in both modes. Empty on a fresh install (no daily files yet).
    capsule = _build_memory_capsule(store)
    if capsule:
        sections.append(capsule)

    diary = _build_diary_digest(store)
    if diary:
        sections.append(diary)

    directives = _build_directives_section(store)
    if directives:
        sections.append(directives)

    # `directives` value is captured for the budget pass below — when
    # the prompt exceeds the soft ceiling, non-active directives can be
    # the next-to-drop after diary and manifest. We capture the full
    # block here because dropping selectively from inside is brittle.
    directives_block = directives or ""

    manifest = ""
    if mode == "manifest":
        manifest = _build_manifest_block(root)
        # P6 Task 4/4b — skills pointer block rides the same drop-priority
        # tier as the rest of the manifest pointers (see _apply_total_budget).
        skills_block = _build_skills_block(root)
        if skills_block:
            manifest = f"{manifest}\n\n{skills_block}" if manifest else skills_block
        if manifest:
            sections.append(manifest)

    # CR-3 — channel overlay rides BEFORE the per-turn ``_build_now_section``
    # so it stays inside the cacheable static prefix. A channel session's
    # manifest cache key spans everything up to "Right now"; the overlay
    # is identical for every turn of a given channel adapter, so it shares
    # a cache slot with the base manifest. Cockpit (``channel_name is None``)
    # leaves the prompt unchanged.
    if channel_name is not None:
        sections.append(build_channel_overlay(channel_name))

    # Autonomy digest — adjacent to "Right now" so background thinking
    # (open agenda items, recent self-reflection) reaches every turn.
    digest = _build_autonomy_digest_section(store, failures_scope)
    if digest:
        sections.append(digest)

    sections.append(_build_now_section(soul_front))

    if not sections:
        return "You are TARS, the operator's personal AI assistant."

    prompt = "\n\n".join(sections)
    return _apply_total_budget(
        prompt,
        diary=diary,
        digest=digest,
        manifest=manifest,
        directives=directives_block,
        capsule=capsule,
    )


def _apply_total_budget(
    prompt: str,
    *,
    diary: str = "",
    digest: str = "",
    manifest: str = "",
    directives: str = "",
    capsule: str = "",
) -> str:
    """Enforce ``MAX_TOTAL_CHARS`` soft ceiling on the assembled prompt.

    Lower-priority blocks are dropped in this order until the budget
    is met:

      1. diary digest
      2. autonomy digest (agenda + self-reflection cross-feed — useful,
         but re-derivable from a tool call; drops right after diary)
      3. manifest pointers (model can still ``file_read`` on demand)
      4. non-active directives block (active rules survive in
         ``brain/rules/``; this block carries operator-saved directives)
      5. memory capsule (yesterday's daily file is the highest-cost
         non-identity content; dropping the whole capsule is the last
         resort before identity is at risk)

    Base identity sections (IDENTITY / SOUL / USER / AGENTS / rules /
    now) are never dropped — truncating identity is worse than
    overrunning the budget. Each drop is logged at WARNING so the
    operator can spot prompt-bloat creep.
    """
    if len(prompt) <= MAX_TOTAL_CHARS:
        return prompt
    drop_order: list[tuple[str, str]] = [
        (diary, "diary digest"),
        (digest, "autonomy digest"),
        (manifest, "manifest pointers"),
        (directives, "non-active directives"),
        (capsule, "memory capsule"),
    ]
    for block, label in drop_order:
        if len(prompt) <= MAX_TOTAL_CHARS:
            return prompt
        prompt = _drop_block(
            prompt, block, label, over_by=len(prompt) - MAX_TOTAL_CHARS,
        )
    if len(prompt) > MAX_TOTAL_CHARS:
        logger.warning(
            "prompt: %d chars exceeds %d budget after all soft drops — "
            "keeping base sections (identity preferred over budget)",
            len(prompt), MAX_TOTAL_CHARS,
        )
    return prompt


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

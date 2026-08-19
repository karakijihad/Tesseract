"""Content-block builders for the assistant's system prompt — file-read helpers,
the manifest/skills pointer blocks, the memory capsule, the diary digest,
the operator-directives section, and the channel-adapter prompt overlay.

Split out of `tesseract/brain/prompt.py` (module-size cleanup, Task 7.5).
"""

from __future__ import annotations

import datetime as _dt
import logging
import re
from pathlib import Path
from typing import Any

from tesseract.brain.skills import load_skills

# Logger name pinned to "tesseract.brain.prompt" — see prompt_time.py's
# module docstring for why this is hardcoded rather than `__name__`.
logger = logging.getLogger("tesseract.brain.prompt")

_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)

# Bootstrap caps for the workspace documents inlined into every prompt. Two
# ceilings rather than one: a single runaway file cannot crowd out the rest,
# and the capsule as a whole stays inside a budget the smallest chat_brain in
# `roles.yaml` can still carry a conversation inside.
PER_FILE_CAP = 12_000
MEMORY_CAPSULE_TOTAL_CAP = 60_000
DAILY_FILES_TO_LOAD = 2  # today + yesterday
# AU-16 derived trees — number of freshest topic hubs (red) + source
# rollups (yellow) to inline into the memory capsule. Each is _read_capped
# so the total bound stays MEMORY_CAPSULE_TOTAL_CAP regardless.
TOPIC_HUBS_TO_LOAD = 3
SOURCE_ROLLUPS_TO_LOAD = 3

# Diary digest — surface the most recent first-person diary entries into the
# per-turn capsule. Librarian still distills the full diary into SOUL Growth
# on cron; this just lets the assistant *see* its own recent reflections in the moment.
DIARY_DIGEST_DAYS = 3
DIARY_DIGEST_CHAR_BUDGET = 2_000

# Operator Directives — feedback + user-type rule records rendered into every
# system prompt. `MemoryStore.list_active_directives` does the floor-filter +
# auto_links dedup across both types; this just decides how much of the
# result fits before the budget cuts in.
DIRECTIVES_CHAR_BUDGET = 6_000
DIRECTIVES_BODY_PREVIEW_CHARS = 200


CHANNEL_OVERLAY_HEADER = "# Channel overlay"

#: The document the overlay is read from, in the operator's workspace beside
#: the three that inline on every turn.
CHANNEL_DOCUMENT = "CHANNEL.md"

#: Substituted at render time so the overlay caches with the static prefix for
#: a given (kind, channel) pair across turns. `str.replace`, not `str.format` —
#: the document is markdown full of braces (`/api/home/{downloads,vault,...}`)
#: and formatting it would either raise or eat them.
_CHANNEL_NAME_TOKEN = "{channel_name}"


def build_channel_overlay(
    channel_name: str | None,
    *,
    workspace_root: Path | None = None,
) -> str:
    """The channel surface contract, read from `CHANNEL.md`.

    It was a string constant in this module until the facts inside it drifted
    twice in two days — an event kind named as a tool, and two attachment
    statuses the runtime had emitted for weeks with nothing explaining them.
    Three of its blocks are now generated from the code that owns them (the
    outbound verbs from the registry, the statuses from
    `_channel_attachment`, the gate from `channels.yaml` through its own
    loader) and the rest is authored prose the operator can edit like any
    other workspace document.

    Empty / missing ``channel_name`` falls back to a generic phrase so the
    overlay still reads naturally for an adapter with no display name wired.

    A missing document renders a visible marker rather than an empty string:
    assembly never dies on this block, and a channel turn that silently lost
    its surface contract would answer in the cockpit's format with no sign
    that anything was wrong.
    """
    if workspace_root is not None:
        root = workspace_root
    else:
        from tesseract.paths import workspace_dir

        root = workspace_dir()
    text = _read_file(root / CHANNEL_DOCUMENT)
    if not text.strip():
        # The shipped default, when the operator's workspace has not been
        # seeded yet — a fresh home, or a test that points `TESSERACT_HOME` at
        # a temp tree. The seed copies this same file, so falling back to it
        # gives the contract the install would have had, rather than dropping
        # the surface rules on a turn that needs them.
        from tesseract.paths import TESSERACT_DIR

        text = _read_file(TESSERACT_DIR / "workspace" / "_shipping" / CHANNEL_DOCUMENT)
    if not text.strip():
        logger.error("channel overlay: %s is missing or empty", root / CHANNEL_DOCUMENT)
        return (
            f"{CHANNEL_OVERLAY_HEADER}\n\n**{CHANNEL_DOCUMENT} is missing from the "
            "workspace, so the rules for this surface are not loaded. Say so "
            "rather than guessing at them.**"
        )
    label = (channel_name or "").strip() or "a remote messaging channel"
    return text.strip().replace(_CHANNEL_NAME_TOKEN, label)


def _strip_frontmatter(text: str) -> str:
    return _FRONTMATTER_RE.sub("", text, count=1).lstrip()


def _read_file(path: Path) -> str:
    if not path.exists():
        logger.warning("workspace file missing: %s", path)
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning("workspace file unreadable: %s (%s)", path, e)
        return ""


def _read_capped(path: Path, cap: int = PER_FILE_CAP) -> str:
    """Read a file and truncate to `cap` chars with a visible marker.

    Silent truncation corrupts meaning; the marker tells the assistant the file
    was cut and it can `file_read` the full version if needed.
    """
    text = _read_file(path)
    if len(text) > cap:
        logger.info("truncated %s to %d chars (was %d)", path.name, cap, len(text))
        return text[:cap] + "\n\n[truncated — read the file directly with file_read for the rest]"
    return text


def _section(title: str, body: str) -> str:
    body = body.strip()
    if not body:
        return ""
    return f"# {title}\n\n{body}" if title else body


_WORKSPACE_POINTER_PREFIX = "tesseract/workspace/"


def _pointer_exists(root: Path, pointer: str) -> bool:
    """Existence check for a manifest pointer entry.

    `root` is the caller's workspace root (`workspace_dir()` in
    production). Pointers under `tesseract/workspace/...` resolve against
    `root` directly — never against a fixed on-disk layout relative to it,
    since `root` moves with `TESSERACT_HOME`. Any other pointer (e.g.
    `Guide/README.md`) is code-tree-relative and resolves against
    `tesseract.paths.ROOT`, which travels with the install.
    """
    if pointer.startswith(_WORKSPACE_POINTER_PREFIX):
        return (root / pointer[len(_WORKSPACE_POINTER_PREFIX):]).exists()
    from tesseract.paths import ROOT, home_dir, readable_state_prefix

    # A pointer under a readable state prefix is resolved the way `file_read`
    # resolves it — against the STATE root. `autonomy/WHAT-RUNS.md` lives
    # beside the reports the runtime writes about itself, not in the code tree,
    # and checking it against `ROOT` would drop the pointer on every packaged
    # install while finding it in a dev checkout, where the two coincide.
    if readable_state_prefix(pointer):
        return (home_dir() / pointer).exists()
    return (ROOT / pointer).exists()


def _published_guide_line() -> str:
    """Where the Guide is published, when `identity.yaml` names it.

    Absent is not an error, and this is the one place in config handling where
    that is the right call. `identity.yaml` lives in the operator's own config
    tree and seeding is file-granular — `config_seed` copies files an install
    lacks and never rewrites one it has — so a key added in a later release
    reaches new installs only. Requiring it would break every existing install
    on update, to state two links the assistant can do without.

    The links are for HANDING TO SOMEONE. The Guide the assistant reads is the
    one inside the app, which the update replaces wholesale and which therefore
    describes the version actually running; the site describes whatever was
    last published, and the two are not the same thing on any day a release is
    in flight.
    """
    from tesseract.paths import home_dir

    path = home_dir() / "config" / "identity.yaml"
    try:
        import yaml

        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        published = (doc.get("documentation") or {}) if isinstance(doc, dict) else {}
        site = str(published.get("site") or "").strip()
    except (OSError, ValueError):
        return ""
    if not site:
        return ""

    return (
        f"The same Guide is published at {site} — give a person that link "
        "rather than pasting the file at them. Read your own copy, not the "
        "site: yours ships inside this app and matches the version you are "
        "running."
    )


def _build_manifest_block(root: Path) -> str:
    """Pointer block for manifest mode — tells the assistant what else exists.

    SOUL.md, USER.md and OPERATING.md are inlined in manifest mode (operating rules
    and the ethics are load-bearing), so neither is in this pointer list.
    """
    pointers: list[tuple[str, str, str]] = [
        ("Guide/README.md",                   "the guide written for the people who use this — what it is, how a turn works, how memory, voice, autonomy and delegation fit together, with a drawing per mechanism", "read when the operator asks how some part of you works, or when you need to explain yourself to someone who has never seen this before"),
        ("Guide/reference/permissions.md",    "which of your tools stop and ask and which run without asking — generated from permissions.yaml itself, so it cannot disagree with the gate", "read before telling the operator what you will or will not do unattended, rather than reasoning about it"),
        ("autonomy/WHAT-RUNS.md",             "what runs on this machine on its own and whether it actually ran — the app's schedules and the operator's, each with what it does, how it fires, when it last ran and how that went; re-derived every hour from the schedule, the run manifest and the run log, so it is never out of date", "read when the operator asks what is running, whether something fired, or why something did not"),
        ("tesseract/workspace/WORKSHOP.md",   "workshop/ layout and naming conventions", "read before writing any task artifact — every task gets its own dated folder"),
        ("tesseract/workspace/DIARY.md",      "first-person reflection log — write via diary_append; librarian distills into SOUL Growth", "read before deciding whether to log a self-observation, or when reviewing your own pattern of behaviour"),
    ]
    present = [(p, d, w) for (p, d, w) in pointers if _pointer_exists(root, p)]
    if not present:
        return ""

    lines = [
        f"You have {len(present)} reference files beyond what's inlined here. Read them with `file_read` when relevant; don't guess from memory.",
        "",
    ]
    for rel, desc, when in present:
        lines.append(f"- `{rel}` — {desc}. *{when}.*")
    lines.append("")
    published = _published_guide_line()
    if published and any(p.startswith("Guide/") for p, _, _ in present):
        lines.append(published)
        lines.append("")
    lines.append(
        "You have a Workspace tab in the Mirror where the operator reviews "
        "your autonomous decisions, scheduler proposals, and any notes you "
        "post via `workspace_post`. Operator-side traffic reaches you on your "
        "next turn as one of two injection blocks: `[workspace_comment_on_<event_id>]` "
        "(reply on a agent-initiated event — comment_id is in the block) and "
        "`[workspace_post_on_<event_id>]` (operator started a new thread from "
        "the workspace — no comment_id, the event_id IS the thread root). "
        "Reply via the `workspace_reply` tool with the `event_id` you saw, and "
        "either the comment_id from the block or the event_id again for an "
        "operator_post. Replies render in the workspace thread, not chat."
    )
    lines.append("")
    lines.append(
        "**Terminal & delegation (operator: \"act like a person working\").** "
        "`delegate_coder` / `delegate_auditor` default to `background=True` — they "
        "return a `spawn_handle` immediately; keep chatting with the operator, "
        "use `spawn_check(handle)` to poll and `spawn_await(handle)` to pick up "
        "the result. For work heavier than an inline delegate call — long "
        "edits, multi-step audits, anything the operator should watch live — "
        "use `start_controller_session(task=..., launch_terminal=True)` to "
        "spawn a background controller session with a viewer pane the "
        "operator can attach to with `agent --session <id>`. `lane_open` / "
        "`lane_named_ensure` open a persistent Claude/Codex lane for "
        "multi-turn work via `lane_send` / `lane_read`. The terminal itself "
        "is operator-manual — the assistant does not type into or read arbitrary "
        "panes."
    )
    return _section("Available reference", "\n".join(lines))


def _build_skills_block(root: Path) -> str:
    """Pointer block for the assistant's markdown skills (P6 Task 4/4b "workshop").

    Same shape as `_build_manifest_block`'s pointer list — name +
    description only; the assistant `file_read`s the SKILL.md body on demand.
    Bundled `scripts/` content (Task 4b) is never inlined here, and
    listing a skill registers nothing — script execution stays on the
    existing bash/subprocess ASK path. Empty/missing
    `workspace/skills/` → "" (section omitted, zero noise).
    """
    skills = load_skills(root / "skills")
    if not skills:
        return ""
    lines = [
        f"You have {len(skills)} skill(s) — prose self-extensions you (or a "
        "delegate) drafted for a repeated chore or capability gap. Read the "
        "`SKILL.md` body with `file_read` before using one; don't guess "
        "behavior from the name alone.",
        "",
    ]
    for skill in skills:
        path = f"tesseract/workspace/skills/{skill.dirname}/SKILL.md"
        lines.append(f"- `{skill.name}` — {skill.description} *(`{path}`)*")
    return _section("Skills", "\n".join(lines))


def _build_memory_capsule(memory_store_dir: Path) -> str:
    """Inline MEMORY.md + today's and yesterday's daily/*.md, capped.

    Order: curated MEMORY.md first (shortest + highest-signal), then today's
    daily, then yesterday's. Stop adding files once TOTAL_CAP would be
    exceeded — remaining files are logged and skipped. An empty capsule
    (no MEMORY.md, no daily files) returns an empty string.
    """
    parts: list[str] = []
    total = 0

    def _add(label: str, path: Path) -> bool:
        nonlocal total
        if not path.exists():
            return True  # missing is fine — continue
        content = _read_capped(path)
        if not content.strip():
            return True
        if total + len(content) > MEMORY_CAPSULE_TOTAL_CAP:
            logger.info("memory capsule cap reached; skipping %s and later", path.name)
            return False
        parts.append(f"<!-- {label} -->\n{content}")
        total += len(content)
        return True

    if not _add("MEMORY.md", memory_store_dir / "MEMORY.md"):
        return _section("Memory capsule", "\n\n".join(parts)) if parts else ""

    daily_dir = memory_store_dir / "daily"
    today = _dt.date.today()
    for delta in range(DAILY_FILES_TO_LOAD):
        day = today - _dt.timedelta(days=delta)
        if not _add(f"daily/{day.isoformat()}.md", daily_dir / f"{day.isoformat()}.md"):
            break

    # AU-16 derived trees — surface the consolidated views in the
    # capsule so the assistant sees "what he's been thinking about" without
    # having to call ``memory_search`` first. Order = global digest
    # (today's whole-system rollup) → freshest topic hubs (red nodes)
    # → freshest source rollups (yellow nodes). Each cap'd by
    # _read_capped; total still bounded by MEMORY_CAPSULE_TOTAL_CAP.
    global_dir = memory_store_dir / "trees" / "global"
    if global_dir.exists():
        if not _add(
            f"trees/global/{today.isoformat()}.md",
            global_dir / f"{today.isoformat()}.md",
        ):
            return _section("Memory capsule", "\n\n".join(parts)) if parts else ""

    topic_dir = memory_store_dir / "trees" / "topic"
    if topic_dir.exists():
        topic_files = sorted(
            topic_dir.glob("*.md"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:TOPIC_HUBS_TO_LOAD]
        for path in topic_files:
            if not _add(f"trees/topic/{path.name}", path):
                break

    source_dir = memory_store_dir / "trees" / "source"
    if source_dir.exists():
        source_files = sorted(
            source_dir.glob("*.md"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:SOURCE_ROLLUPS_TO_LOAD]
        for path in source_files:
            if not _add(f"trees/source/{path.name}", path):
                break

    if not parts:
        return ""
    return _section("Memory capsule", "\n\n".join(parts))


def _build_diary_digest(memory_store_dir: Path) -> str:
    """Inline the last DIARY_DIGEST_DAYS of diary entries, capped.

    The librarian distills the full diary into SOUL Growth on its cron pass;
    this digest just surfaces the assistant's most recent first-person reflections to
    the per-turn capsule so they're visible *in the moment*, not only after
    the next consolidation. Empty diary dir returns "" — no section emitted.
    """
    diary_dir = memory_store_dir / "diary"
    if not diary_dir.exists():
        return ""

    cutoff = _dt.date.today() - _dt.timedelta(days=DIARY_DIGEST_DAYS)
    files: list[tuple[str, Path]] = []
    for path in diary_dir.glob("*.md"):
        try:
            stem_date = _dt.date.fromisoformat(path.stem)
        except ValueError:
            continue
        if stem_date < cutoff:
            continue
        files.append((path.stem, path))
    if not files:
        return ""
    files.sort(key=lambda x: x[0], reverse=True)

    chunks: list[str] = []
    budget = DIARY_DIGEST_CHAR_BUDGET
    for stem, path in files:
        try:
            body = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if not body:
            continue
        block = f"--- {stem} ---\n{body}"
        if len(block) > budget:
            chunks.append(block[:budget] + "\n[truncated]")
            break
        chunks.append(block)
        budget -= len(block)
        if budget <= 0:
            break

    if not chunks:
        return ""
    return _section("Recent diary", "\n\n".join(chunks))


def _build_directives_section(
    memory_store_dir: Path,
    *,
    char_budget: int = DIRECTIVES_CHAR_BUDGET,
) -> str:
    """Render `## Operator Directives` from active feedback memories.

    Imports `MemoryStore` lazily so the prompt module doesn't hard-depend
    on the memory layer at import time (keeps unit tests that stub the
    workspace dir but skip memory cheap to set up). Empty store, missing
    dir, or no records over the importance floor → "" (no section emitted,
    no whitespace bloat).

    When the rendered block exceeds `char_budget`, drop the lowest-importance
    / oldest entries first and log a WARN — this is the operator-visible
    pulse signal that consolidation is overdue.
    """
    if not memory_store_dir.exists():
        return ""
    try:
        from tesseract.memory.store import MemoryStore
        store = MemoryStore(memory_store_dir)
        records = store.list_active_directives()
    except Exception:
        logger.warning("operator-directives: failed to load directives", exc_info=True)
        return ""
    if not records:
        return ""

    def _line(fm: Any) -> str:
        body = (fm.summary or "").strip()
        if not body:
            preview = ""
        else:
            preview = body.splitlines()[0].strip()
        if len(preview) > DIRECTIVES_BODY_PREVIEW_CHARS:
            preview = preview[:DIRECTIVES_BODY_PREVIEW_CHARS].rstrip() + "…"
        title = fm.title.strip() or fm.id
        return f"- [imp {fm.importance}] {title}: {preview}" if preview else f"- [imp {fm.importance}] {title}"

    lines = [_line(fm) for fm in records]
    body = "\n".join(lines)
    dropped = 0
    while len(body) > char_budget and lines:
        lines.pop()
        dropped += 1
        body = "\n".join(lines)
    if dropped:
        logger.warning(
            "operator-directives: dropped %d / %d records over %d-char budget — running consolidator at next tick",
            dropped, dropped + len(lines), char_budget,
        )
    if not lines:
        return ""
    return _section("Operator Directives", body)

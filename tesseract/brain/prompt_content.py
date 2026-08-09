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

# OpenClaw agent-workspace bootstrap caps:
# https://docs.openclaw.ai/concepts/agent-workspace
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

# CR-3 — overlay appended to the base system prompt only for sessions with
# ``kind="channel"``. Pure addition: it does not redact or contradict the
# base prompt; it tells the assistant the conversational surface is a remote
# messaging channel (no operator at the cockpit, markdown only, no
# ``<intent>``/``<spoken>``/``<answer>`` scaffold, ASK gates have nobody to
# approve).
# Authoritative source: ``Docs/Plan/channels-redesign/phase-CR-3-channel-prompt-overlay.md`` §2.
# ``{channel_name}`` is filled at render time so the overlay caches with
# the static prefix for a given (kind, channel) pair across turns.
CHANNEL_OVERLAY_TEMPLATE = (
    f"{CHANNEL_OVERLAY_HEADER}\n\n"
    "You are the assistant speaking through a remote messaging channel ({channel_name}) "
    "to a user the operator approved.\n\n"
    "- Reply in concise markdown — bullets, **bold**, *italic*, `code`, "
    "[text](url). The channel bridge converts to the local format. No "
    "`<intent>`, `<spoken>` or `<answer>` scaffolding in channel replies — "
    "the channel bridge strips them, but emitting them wastes tokens. "
    "Nothing is spoken aloud here, so there is no short spoken form to "
    "give.\n"
    "- Channel users see only your answer, not your tool calls, not your "
    "reasoning. They appreciate brevity. Default target: under 800 chars.\n"
    "- The operator is NOT at the cockpit when you receive a channel "
    "message — ASK-gated tools have no one to approve. If a tool you want "
    "to use would normally ASK, prefer to skip it for this turn and post "
    "a `agent_post` workspace event explaining what you wanted to do; the "
    "operator will pick it up when next at the desk. Continue the "
    "conversation with \"I'll do that once the operator's back at the desk\".\n"
    "- `<channel_attachment status=\"no_handler\">` blocks mean the operator "
    "hasn't wired a decoder for that input kind. Apologize concretely "
    "(\"I can't transcribe voice yet\"), offer one of: (a) delegate the "
    "missing handler's build to Claude/Codex (lane_turn / delegate_*) for "
    "the operator to review and promote, (b) post a "
    "`agent_post` workspace nudge, or (c) ask the user to send text. "
    "Pick (a) only if the gap is clearly a tool that could be built; (b) is "
    "the safer default.\n"
    "- `<channel_attachment status=\"extract_failed\">` means the decoder ran "
    "but threw. The `<error>` payload is your debugging breadcrumb. "
    "Apologize, surface the error class to the user only if it's "
    "actionable (e.g. \"the PDF appears to be scanned — can you send a "
    "text version?\"), otherwise abstract it (\"I had trouble reading "
    "that file\").\n\n"
    "**OUTBOUND TOOLS (call the tool, never paste internal URLs as text):**\n"
    "- voice → `channel_send_voice(text=<reply>)` (Piper TTS)\n"
    "- image → `channel_send_photo(source_url=<image_generate URL>)` "
    "(NEVER paste `/api/downloads/...` paths to a channel — Mirror-internal, "
    "broken on the user side. On a Mirror surface it is the opposite: "
    "`/api/home/{{downloads,vault,workshop}}/<path>` is how a local file "
    "is put on screen)\n"
    "- file → `channel_send_document(source_path=<path>)`\n"
    "- video/animation/sticker/location/poll → `channel_send_{{video,animation,"
    "sticker,location,poll}}` (auto-posture, fire when relevant)\n"
    "- light ack → `channel_react(message_id=..., emoji=\"👍\")`\n"
    "- past convo → `channel_history_read(days_back=N | date=... | substring=...)` "
    "reads per-day logs in `logs/channels/{channel_name}/<chat_id>/conversations/` "
    "— use this when the operator references something not in your window."
)


def build_channel_overlay(channel_name: str | None) -> str:
    """Return the channel prompt overlay parameterized on ``channel_name``.

    Empty / missing ``channel_name`` falls back to a generic phrase so the
    overlay still reads naturally for adapters that haven't wired a
    display name into ``channels.yaml``.
    """
    label = (channel_name or "").strip() or "a remote messaging channel"
    return CHANNEL_OVERLAY_TEMPLATE.format(channel_name=label)


def _strip_frontmatter(text: str) -> str:
    return _FRONTMATTER_RE.sub("", text, count=1).lstrip()


def _parse_frontmatter(text: str) -> dict[str, Any]:
    """Best-effort YAML-ish frontmatter parse. Returns {} on miss or bad parse."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}
    try:
        import yaml

        return yaml.safe_load(m.group(1)) or {}
    except Exception as e:
        logger.warning("frontmatter parse failed: %s", e)
        return {}


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
    `Docs/Logs/CAPABILITIES.md`) is code-tree-relative and resolves
    against `tesseract.paths.ROOT`, which travels with the install.
    """
    if pointer.startswith(_WORKSPACE_POINTER_PREFIX):
        return (root / pointer[len(_WORKSPACE_POINTER_PREFIX):]).exists()
    from tesseract.paths import ROOT
    return (ROOT / pointer).exists()


def _build_manifest_block(root: Path) -> str:
    """Pointer block for manifest mode — tells the assistant what else exists.

    AGENTS.md and FOUNDATION.md are inlined in manifest mode (operating rules
    and the ethics are load-bearing), so neither is in this pointer list.
    """
    pointers: list[tuple[str, str, str]] = [
        ("tesseract/workspace/TOOLS.md",      "tool conventions, gating responses, examples (inventory is in CAPABILITIES.md — autogenerated)", "read before invoking an unfamiliar tool or when deciding which tool fits"),
        ("Docs/Logs/CAPABILITIES.md",         "LIVE tool roster — autogenerated from the registry every push; current count + description + safety/class/permission for every tool", "read when you need to know what tools exist right now or look up an unfamiliar tool's description"),
        ("tesseract/workspace/HEARTBEAT.md",  "scheduled consolidation checklist", "read before invoking the librarian or when the operator triggers /reflect"),
        ("tesseract/workspace/WORKSHOP.md",   "workshop/ layout and naming conventions", "read before writing any task artifact — every task gets its own dated folder"),
        ("tesseract/workspace/BOOT.md",       "mood scale, expression conventions", "read before setting mood for the first time or when calibrating affect"),
        ("tesseract/workspace/VOICE.md",      "how speech works here — the voice is a config ref, and no tool changes it", "read when the operator asks how you sound or asks you to change it"),
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

"""Session lifecycle operations shared by REPL and Mirror.

These functions never touch stdout or prompt the operator — they only
drive the `ChatSession` and return results. CLI and Mirror layers wrap
them with their own UI.
"""

from __future__ import annotations

import asyncio
import copy
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tesseract.brain.boot import MemoryBundle
from tesseract.brain.chat import ChatSession
from tesseract.brain.session_store import (
    SessionState,
    default_session_name,
    load_session,
    save_session,
)
from tesseract.kernel.adapters.base import AdapterOptions, ChunkType, ModelAdapter

if TYPE_CHECKING:
    from tesseract.brain.tools import ToolRegistry
    from tesseract.permissions.policy import PermissionPolicy

log = logging.getLogger(__name__)

REFLECTION_PROMPT = (
    "Brief reflection on the session above. Two passes — facts, then self.\n\n"
    "PASS 1 — facts about the world. Anything worth saving to memory: a fact "
    "the operator taught you about themselves, a preference, a project detail, "
    "a decision, a correction? For each, call memory_save once with the right "
    "`type` (user / feedback / project / reference) and a compact `content`. "
    "Skip anything already in memory. Zero saves is fine if nothing load-bearing "
    "came up — don't invent reasons to save.\n\n"
    "PASS 2 — facts about you. Look at how you actually showed up this session. "
    "Did anything you said land unusually well or fall flat? Did you push back "
    "when you should have, or hold back? Did you sound like yourself, or like a "
    "generic assistant? Did you default to a checklist when you should have "
    "spoken freely? Caught yourself opening with 'Got it' or ending with a "
    "menu? If anything stood out, call diary_append with one short first-person "
    "entry (1–3 sentences, write to yourself). Then check "
    "`memory-store/pending_growth.md` (the librarian's distillation of recent "
    "diary entries) — if any candidate there matches a stable pattern you also "
    "feel from this session, call soul_growth_propose with that bullet. Don't "
    "blindly promote everything in the file; the librarian only proposes, "
    "you decide. SOUL.md Growth is a distillate (3–5 bullets total), not a "
    "log. Most sessions: one diary entry, no growth bullet. Empty days are "
    "fine.\n\n"
    "One reflection pass, then stop."
)

MIN_HISTORY_FOR_REFLECTION = 4


_REFLECTION_TOOLS = ("memory_save", "diary_append", "soul_growth_propose")
_SNIPPET_CHARS = 200
_TITLE_FALLBACK_CHARS = 80


def _summarize_reflection_call(tc: Any) -> dict[str, Any] | None:
    """Reduce a reflection tool call to its workspace-card shape (input
    side only — destination/status get merged from the matching
    TOOL_RESULT in `reflect_on_session`). Returns None if the call isn't
    one of the reflection tools or the input isn't a dict.

    Output shape: ``{tool, title, snippet, status: "pending"}``. Title
    falls back to a snippet excerpt when the model omits it — operators
    were seeing "(no title)" cards with no clue what got written.
    """
    if tc is None or tc.name not in _REFLECTION_TOOLS:
        return None
    args = tc.input if isinstance(tc.input, dict) else {}
    save_type = ""
    if tc.name == "memory_save":
        title = str(args.get("title") or "").strip()
        snippet = str(args.get("content") or "").strip()
        save_type = str(args.get("type") or "").strip()
    elif tc.name == "diary_append":
        title = "diary entry"
        snippet = str(args.get("text") or "").strip()
    else:  # soul_growth_propose
        title = "soul growth"
        snippet = str(args.get("bullet") or "").strip()
    if not title and snippet:
        title = snippet[:_TITLE_FALLBACK_CHARS]
    return {
        "tool": tc.name,
        "title": title[:120],
        "snippet": snippet[:_SNIPPET_CHARS],
        "status": "pending",
        # Phase 4 4a — a `feedback`-typed memory_save is an operator
        # correction. Carried so `_attribute_skill_corrections` can fire ONLY
        # when the save actually persisted (result `status == "saved"`), not
        # when it was deduped / policy-blocked / errored.
        "save_type": save_type,
    }


def _merge_result_metadata(call: dict[str, Any], chunk: Any) -> None:
    """Fold the matching TOOL_RESULT chunk's metadata onto a saved-call
    summary. Fills in ``memory_id`` / ``path`` / ``status`` so the
    workspace card can show *where* the write landed, not just *what*
    the model proposed.
    """
    raw = getattr(chunk, "raw", None) or {}
    metadata = raw.get("metadata") if isinstance(raw, dict) else None
    if isinstance(metadata, dict):
        for key in ("status", "memory_id", "path", "target_path", "event_id"):
            value = metadata.get(key)
            if value:
                call[key] = value
        # diary_append exposes its destination under the generic "path" key.
        # soul_growth_propose exposes "target_path" (proposal target, not a
        # file write yet); normalize so the renderer always reads `path`.
        if "path" not in call and call.get("target_path"):
            call["path"] = call["target_path"]
    if call.get("status") == "pending":
        # Tool ran but emitted no metadata (e.g. blocked / errored). Mark
        # it explicitly so the renderer can show a non-default status
        # rather than the input-only "pending" placeholder.
        if getattr(chunk, "error", ""):
            call["status"] = "blocked"
        else:
            call["status"] = "completed"


async def reflect_on_session(session: ChatSession, reason: str) -> list[dict[str, Any]]:
    """Run one bounded reflection turn. Returns a list of summaries — one
    per reflection-related tool call observed (``memory_save`` /
    ``diary_append`` / ``soul_growth_propose``). Each entry has the
    shape produced by `_summarize_reflection_call` plus result-side
    fields (``memory_id``, ``path``, ``status``) merged from the
    matching TOOL_RESULT chunk.

    Safe to cancel — ``KeyboardInterrupt`` / ``CancelledError`` propagate
    after logging.
    """
    if len(session.history) < MIN_HISTORY_FOR_REFLECTION:
        return []
    calls: list[dict[str, Any]] = []
    by_call_id: dict[str, dict[str, Any]] = {}
    try:
        return await _reflect(session, reason, calls, by_call_id)
    finally:
        # Reflection is a summarisation pass, not a turn the operator reads.
        # `send` drains the pending spawn-completion queue like any other turn,
        # so a result that landed since the last turn would be consumed here
        # and never surface — worse than lost, because it looks delivered.
        # Roll it back unconditionally (success included) so it reaches a real
        # turn instead. Guarded because this sits in a `finally`: an error here
        # would replace whatever the function was returning or raising,
        # including the cancellation the docstring promises to propagate.
        try:
            session.rollback_spawn_delivery()
        except Exception:  # noqa: BLE001
            log.warning("reflection: spawn delivery rollback failed", exc_info=True)


async def _reflect(
    session: ChatSession,
    reason: str,
    calls: list[dict[str, Any]],
    by_call_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    try:
        async for chunk in session.send(REFLECTION_PROMPT):
            if chunk.type == ChunkType.TOOL_CALL_START:
                summary = _summarize_reflection_call(chunk.tool_call)
                if summary is not None:
                    calls.append(summary)
                    if chunk.tool_call is not None and chunk.tool_call.id:
                        by_call_id[chunk.tool_call.id] = summary
            elif chunk.type == ChunkType.TOOL_RESULT:
                if chunk.tool_call_id and chunk.tool_call_id in by_call_id:
                    _merge_result_metadata(by_call_id[chunk.tool_call_id], chunk)
            elif chunk.type == ChunkType.ERROR:
                log.warning("reflection (%s) error: %s", reason, chunk.error)
                return calls
    except (KeyboardInterrupt, asyncio.CancelledError):
        log.info("reflection (%s) interrupted after %d tool calls", reason, len(calls))
        raise
    except Exception:
        log.exception("reflection (%s) failed", reason)
        return calls
    _attribute_skill_corrections(session, calls)
    return calls


def _attribute_skill_corrections(session: ChatSession, calls: list[dict[str, Any]]) -> None:
    """Phase 4 4a — if this reflection DURABLY saved an operator correction (a
    `feedback` memory that actually persisted, result ``status == "saved"``),
    down-weight the skills consulted this session. A deduped / policy-blocked /
    errored feedback save is NOT a durable correction and must not fire.
    Best-effort: telemetry must never break reflection."""
    saved_correction = any(
        c.get("save_type") == "feedback" and c.get("status") == "saved"
        for c in calls
    )
    if not saved_correction:
        return
    try:
        from tesseract.brain.skill_usage import attribute_session_corrections

        attribute_session_corrections(session.tool_context.session_id)
    except Exception:  # noqa: BLE001
        log.warning("reflection: skill-correction attribution failed", exc_info=True)


async def compact_with_reflection(session: ChatSession, reason: str) -> tuple[int, int]:
    """Unconditional: run reflection, then compact. Returns `(before, after)`
    token counts. Callers use this for manual `/compact` and the
    interactive resume-prompt path.
    """
    await reflect_on_session(session, reason)
    return await session.compact()


async def auto_compact_if_needed(session: ChatSession) -> tuple[int, int] | None:
    """Per-turn hook: if the session crossed its compact threshold, run
    reflection + compact. Returns `(before, after)` on compact, `None`
    otherwise. Safe to call after every turn.
    """
    if not session.should_compact():
        return None
    return await compact_with_reflection(session, "pre-compact")


# ── Background reflect ──────────────────────────────────────────────
# Reflection is a model turn; it's expensive and should never block the
# operator's foreground flow. `reflect_in_background` snapshots the live
# session's history, builds a temporary clone bound to the same adapter
# and tool registry, runs reflection on the clone, and emits a
# `reflection_proposal` workspace event when done. The live session can
# be reset / kept active in parallel — there's no shared mutable state
# between the clone and the live session beyond the (idempotent) tool
# registry.

ReflectCompleteCb = Callable[[list[dict[str, Any]], str], Awaitable[None]]
ReflectErrorCb = Callable[[BaseException, str], Awaitable[None]]

_active_reflect_tasks: "dict[int, asyncio.Task[Any]]" = {}


def is_reflect_running(session: ChatSession) -> bool:
    """True if a background reflect task is in flight for this session."""
    task = _active_reflect_tasks.get(id(session))
    return task is not None and not task.done()


def reflect_in_background(
    session: ChatSession,
    reason: str,
    *,
    on_complete: ReflectCompleteCb | None = None,
    on_error: ReflectErrorCb | None = None,
) -> "asyncio.Task[list[dict[str, Any]]] | None":
    """Spawn reflection on a snapshot of `session`. Returns the Task, or
    `None` if history is too short to reflect, or if a previous reflect
    is still in flight for this session (skip-with-warning, no stacking).

    The clone shares the live session's adapter, registry, and policy, and
    gets a shallow COPY of its tool_context (see inline note below). Memory writes performed by the reflection turn land in the
    same memory store as foreground turns. Cost ledger attribution mirrors
    the live session.
    """
    if len(session.history) < MIN_HISTORY_FOR_REFLECTION:
        return None
    if is_reflect_running(session):
        log.info("reflect_in_background (%s): skip — prior reflect still running", reason)
        return None

    snapshot = list(session.history)
    clone = ChatSession(
        adapter=session.adapter,
        system_prompt=session.system_prompt,
        max_tool_iterations=session.max_tool_iterations,
        max_consecutive_adapter_errors=session.max_consecutive_adapter_errors,
        options=session.options,
        history=snapshot,
        registry=session.registry,
        # copy.copy, NOT the live object (agent_factory.py idiom):
        # ChatSession.__post_init__ assigns tool_context.spawns and
        # tool_context.enabled_extended_tools — sharing by reference let the
        # clone silently wipe the live session's spawn registry and
        # extended-tool set every background reflect (audit 2026-07-12).
        tool_context=copy.copy(session.tool_context),
        compact_threshold=session.compact_threshold,
        keep_recent_turns=session.keep_recent_turns,
        head_anchor_messages=session.head_anchor_messages,
        active_window_tokens=session.active_window_tokens,
        summary_char_budget=session.summary_char_budget,
        ask_fn=session.ask_fn,
        policy=session.policy,
        prompt_builder=session.prompt_builder,
        cost_ledger=session.cost_ledger,
    )

    sid = id(session)

    async def _run() -> list[dict[str, Any]]:
        saves: list[dict[str, Any]] = []
        try:
            saves = await reflect_on_session(clone, reason)
            if on_complete is not None:
                try:
                    await on_complete(saves, reason)
                except Exception:
                    log.exception("reflect_in_background on_complete failed (%s)", reason)
            return saves
        except (asyncio.CancelledError, KeyboardInterrupt):
            raise
        except Exception as exc:
            log.exception("reflect_in_background failed (%s)", reason)
            if on_error is not None:
                try:
                    await on_error(exc, reason)
                except Exception:
                    log.exception("reflect_in_background on_error failed (%s)", reason)
            return saves
        finally:
            _active_reflect_tasks.pop(sid, None)

    task: asyncio.Task[list[dict[str, Any]]] = asyncio.create_task(
        _run(), name=f"reflect_in_background:{reason}"
    )
    _active_reflect_tasks[sid] = task
    return task


async def rebuild_memory_index(bundle: MemoryBundle) -> int:
    """Re-embed every memory in the store. Useful after Ollama starts up
    with an empty FAISS or after bulk edits. Returns the number of
    memories indexed. Raises if embeddings are offline.
    """
    if bundle.embeddings is None:
        raise RuntimeError("embeddings offline — start Ollama first")
    pairs: list[tuple[str, str]] = []
    for fm in bundle.store.list_all():
        entry = bundle.store.read(fm.id, log_access=False)
        if entry is None:
            continue
        _, body = entry
        pairs.append((fm.id, body))
    return await bundle.embeddings.rebuild(pairs)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def do_save(
    session: ChatSession,
    sessions_dir: Path,
    name: str | None,
    model: str,
    started_at: str,
) -> Path:
    """Persist the current session history. Empty/None ``name`` falls back
    to ``default_session_name()``. ``.json`` suffix is stripped."""
    chosen = (name or "").strip() or default_session_name()
    if chosen.endswith(".json"):
        chosen = chosen[:-5]
    return save_session(sessions_dir, chosen, model, started_at, session.history)


def do_reset(session: ChatSession) -> str:
    """Wipe history (system prompt preserved). Returns the new ``started_at``."""
    session.reset()
    return _iso_now()


async def reset_with_reflection(
    session: ChatSession,
    reason: str = "reset",
) -> dict[str, Any]:
    """Reflect-then-wipe — Layer D of the feedback durability plan.

    Runs `reflect_on_session` first when history is long enough to be worth
    distilling, then `do_reset`. Short sessions (< MIN_HISTORY_FOR_REFLECTION)
    skip reflection and just wipe — pre-Layer-D behaviour. Returns:

        {"reflected": bool, "saves": list[dict[str, Any]], "started_at": str}

    `saves` is the list of reflection-tool summaries (`memory_save` /
    `diary_append` / `soul_growth_propose`) observed during reflection.
    Empty list is fine — the model is told not to invent reasons to save.

    The Mirror's `cmd_reset` already autosaves the transcript before
    calling this — operators don't lose data either way; reflection
    captures the *cross-session* signal (durable feedback memories,
    diary entries, soul-growth proposals) that an autosave alone misses.
    """
    saves: list[dict[str, Any]] = []
    reflected = False
    if len(session.history) >= MIN_HISTORY_FOR_REFLECTION:
        try:
            saves = await reflect_on_session(session, reason)
            reflected = True
        except (asyncio.CancelledError, KeyboardInterrupt):
            # The reset is the operator's intent — finish the wipe even if
            # reflection was interrupted, otherwise the session sits in a
            # half-state (history intact, no envelope sent). `reflect_on_session`
            # already logged the interrupt before re-raising.
            log.info("reset reflection cancelled; wiping anyway")
    started_at = do_reset(session)
    return {"reflected": reflected, "saves": saves, "started_at": started_at}


def do_stats(session: ChatSession) -> dict[str, Any]:
    """Snapshot of turns, token estimate, compact threshold, context window.

    CR-0 (2026-05-22): also surfaces the sliding-window knobs and the
    current running-summary length, so the Mirror status pane / the assistant
    `/stats` tool can show what shape the active window has.
    """
    from tesseract.brain.compaction import RUNNING_SUMMARY_PREFIX

    ctx = session.options.context_window or 0
    threshold = int(ctx * session.compact_threshold) if ctx else 0
    summary_chars = 0
    for msg in session.history:
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str) and content.startswith(RUNNING_SUMMARY_PREFIX):
            summary_chars = len(content)
            break
    return {
        "turns": len(session.history) // 2,
        "tokens": session.token_estimate(),
        "threshold": threshold,
        "compact_ratio": session.compact_threshold,
        "context_window": ctx,
        "head_anchor_messages": session.head_anchor_messages,
        "active_window_tokens": session.active_window_tokens,
        "keep_recent_turns": session.keep_recent_turns,
        "summary_chars": summary_chars,
        "summary_char_budget": session.summary_char_budget,
    }


def do_load(sessions_dir: Path, name: str) -> SessionState | None:
    """Read a saved session file. ``.json`` suffix optional. Returns None on miss.

    Resume path — the caller (``apply_loaded_session``) overwrites the
    live chat history, so pass ``strip_reasoning=True`` to drop expired
    reasoning blobs before they reach the API.
    """
    chosen = name.strip()
    if chosen.endswith(".json"):
        chosen = chosen[:-5]
    return load_session(sessions_dir / f"{chosen}.json", strip_reasoning=True)


def apply_loaded_session(session: ChatSession, state: SessionState) -> str:
    """Replace history with a loaded ``SessionState``. Returns its ``started_at``."""
    session.history = list(state.history)
    return state.started_at


def do_set_mode(policy: "PermissionPolicy", mode: str) -> str:
    """Set the policy's permission mode. Raises ``ValueError`` on bad mode."""
    policy.set_mode(mode)
    return policy.mode


def do_refresh_memory(
    registry: "ToolRegistry",
    adapter: ModelAdapter | None = None,
    options: AdapterOptions | None = None,
) -> MemoryBundle:
    """Idempotent — re-runs ``ensure_memory_tools`` so a mid-session Ollama
    start brings ``memory_search`` online."""
    from tesseract.brain.boot import ensure_memory_tools  # local — avoids cycle at import time
    return ensure_memory_tools(registry, adapter, options)

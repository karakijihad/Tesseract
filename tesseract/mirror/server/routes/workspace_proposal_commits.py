"""Commit handlers for the file-edit and memory-facing decision cards.

`change_proposal`, `yaml_change_proposal`, `soul_proposal`, `feedback_proposal`
(and `feedback_sweep`, which reaches the same handler), and
`vault_raw_ingest_batch` — every kind whose approve moves a file or a
memory-store record through a seam the job that filed the card never
touched. Split out of `routes/workspace.py`; see
`workspace_skill_commits.py` for the identity/skill half,
`workspace_dial_commits.py` for the two dial cards (`tuning_proposal`,
`working_set_proposal`), and `workspace.py` for why every one of these is
safely re-exported from there for existing test imports.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from aiohttp import web

from tesseract.kernel.workspace_changes import (
    ConcurrentModificationError,
    ProposeError,
    apply_change,
    compute_diff,
    hash_text,
    resolve_proposable_path,
    validate_growth_section,
)
from tesseract.mirror.server.routes.workspace_shared import _broadcast_envelope, _store
from tesseract.paths import ROOT, workspace_dir
from tesseract.workspace_events import WorkspaceEvent

log = logging.getLogger(__name__)


async def _commit_change_proposal(
    app: web.Application,
    ev: WorkspaceEvent,
) -> tuple[dict[str, Any], int] | None:
    """Perform the file commit for a `change_proposal` event. Returns
    `(error_payload, status_code)` on failure (caller returns it as the
    JSON response); returns None on success."""
    payload = ev.payload or {}
    target_path = str(payload.get("target_path") or "")
    action = str(payload.get("action") or "")
    content = str(payload.get("content") or "")
    section = payload.get("section")
    if section is not None:
        section = str(section)
    expected_hash_before = payload.get("expected_hash_before")
    if expected_hash_before is not None:
        expected_hash_before = str(expected_hash_before)

    try:
        applied = await asyncio.to_thread(
            apply_change,
            repo_root=workspace_dir(),
            target_path=target_path,
            action=action,  # type: ignore[arg-type]
            content=content,
            section=section,
            expected_hash_before=expected_hash_before,
        )
    except ConcurrentModificationError as exc:
        # File changed under us. Recompute the diff against current bytes
        # so the operator can re-review with fresh context. The event
        # stays pending — Approve again to commit against the new hash.
        try:
            current = resolve_proposable_path(target_path).read_text(encoding="utf-8")
            from tesseract.kernel.workspace_changes import preview_change
            new_after = preview_change(
                current_text=current,
                action=action,  # type: ignore[arg-type]
                content=content,
                section=section,
            )
            fresh_diff = compute_diff(
                current, new_after, target_label=str(payload.get("label") or "file"),
            )
            new_hash = hash_text(current)
            fresh_bytes = (
                len(current.encode("utf-8")),
                len(new_after.encode("utf-8")),
            )
        except Exception:  # noqa: BLE001 — diagnostics only
            fresh_diff = ""
            new_hash = ""
            fresh_bytes = None
        # Write the recomputed snapshot back onto the card, which is what makes
        # "Approve again" true. Without it the payload keeps the hash it was
        # born with, so every later Approve loses the same race and the event
        # is pending forever with no way out but Reject.
        #
        # The whole snapshot moves together — hash, diff and the two sizes the
        # card renders — because half of it would describe the file as it was
        # and half as it is.
        #
        # Only when the recompute produced a hash: merging an empty one would
        # leave `expected_hash_before` falsy, and the next Approve would skip
        # the drift check entirely and commit against bytes nobody reviewed.
        if new_hash and fresh_bytes is not None:
            _store(app).merge_event_payload(
                ev.event_id,
                {
                    "expected_hash_before": new_hash,
                    "diff": fresh_diff,
                    "bytes_before": fresh_bytes[0],
                    "bytes_after": fresh_bytes[1],
                },
            )
        return {
            "error": "concurrent_modification",
            "detail": str(exc),
            "expected_hash_before": exc.expected,
            "actual_hash": exc.actual,
            "fresh_diff": fresh_diff,
            "fresh_expected_hash_before": new_hash,
        }, 409
    except ProposeError as exc:
        return {"error": "invalid_proposal", "detail": str(exc)}, 400
    except OSError as exc:
        return {"error": "commit_failed", "detail": str(exc)}, 500

    await _broadcast_envelope(
        app,
        "soul_updated" if target_path == "tesseract/workspace/SOUL.md" else "workspace_file_updated",
        {
            "path": target_path,
            "label": str(payload.get("label") or ""),
            "content": resolve_proposable_path(target_path).read_text(encoding="utf-8"),
            "source": "workspace_decision",
            "hash_after": applied.hash_after,
            # `no_op_reason` is set when apply_change short-circuited
            # because the proposed content was already present (idempotent
            # commit). Frontend uses it to render a "duplicate, no-op"
            # toast instead of a silent success.
            "no_op_reason": applied.no_op_reason,
        },
    )
    return None


async def _commit_yaml_change_proposal(
    app: web.Application,
    ev: WorkspaceEvent,
) -> tuple[dict[str, Any] | None, tuple[dict[str, Any], int] | None]:
    """Apply a ``yaml_change_proposal`` event via :func:`apply_yaml_change`.

    Returns ``(result, None)`` on success, ``(None, (error_payload, status))``
    on failure. On success the caller writes the event status as
    ``applied`` and triggers the roles SUMMARY regen for catalog edits.
    """
    from tesseract.kernel.workspace_changes import apply_yaml_change

    payload = ev.payload or {}
    target_path = str(payload.get("target_path") or "")
    action = str(payload.get("action") or "")
    yaml_path = str(payload.get("yaml_path") or "")
    content = payload.get("content")
    expected_hash_before = str(payload.get("expected_hash_before") or "")
    if not target_path or not action or not yaml_path or not expected_hash_before:
        return None, ({"error": "invalid_proposal", "detail": "missing required fields"}, 400)

    try:
        result = await asyncio.to_thread(
            apply_yaml_change,
            repo_root=ROOT,
            target_path=target_path,
            action=action,  # type: ignore[arg-type]
            yaml_path=yaml_path,
            content=content,
            expected_hash_before=expected_hash_before,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("yaml_change_proposal commit crashed")
        return None, ({"error": "commit_failed", "detail": str(exc)}, 500)

    if not result.ok:
        # Pre-write check failure — return 409 so the inbox renders the
        # reason inline (drift / schema / parse). Event stays pending so
        # the operator can re-emit / approve again after fixing it.
        return None, ({
            "error": "apply_refused",
            "reason": result.reason,
            "target_path": result.target_path,
        }, 409)

    meta = {
        "target_path": result.target_path,
        "bytes_before": result.bytes_before,
        "bytes_after": result.bytes_after,
        "hash_before": result.hash_before,
        "hash_after": result.hash_after,
        "no_op_reason": result.no_op_reason,
    }

    if result.target_path in {"tesseract/config/roles.yaml", "tesseract/config/providers.yaml"}:
        try:
            from tesseract.scripts.regenerate_roles_summary import regenerate

            await asyncio.to_thread(regenerate)
        except Exception:  # noqa: BLE001
            log.exception("yaml_change_proposal: SUMMARY regen failed (non-fatal)")

    return meta, None


_SOUL_REL = "tesseract/workspace/SOUL.md"


async def _commit_soul_proposal(
    app: web.Application,
    ev: WorkspaceEvent,
) -> tuple[dict[str, Any] | None, tuple[dict[str, Any], int] | None]:
    """Apply a ``soul_proposal`` event by appending the bullet to SOUL.md.

    Operator-attended single approval: the consolidator card already
    rendered the bullet text, so this commits straight to SOUL.md instead
    of round-tripping through ``soul_growth_propose`` (which would queue
    a second ``change_proposal`` card for the same decision). On success
    the caller flips the event status to ``applied`` and we broadcast
    ``soul_updated`` so any attached Mirror session refreshes the Soul
    tab without a manual reload.
    """
    payload = ev.payload or {}
    bullet = str(payload.get("bullet") or "").strip().lstrip("-*").strip()
    if not bullet:
        return None, ({"error": "invalid_proposal", "detail": "missing bullet"}, 400)

    # The section the proposal named, not a fixed one: SOUL holds a section per
    # kind of growth, and committing every approved bullet to one heading would
    # undo that at the last step.
    try:
        section = validate_growth_section(str(payload.get("section") or "").strip())
    except ProposeError as exc:
        return None, ({"error": "invalid_proposal", "detail": str(exc)}, 400)

    bullet_line = f"- {bullet}\n"
    try:
        applied = await asyncio.to_thread(
            apply_change,
            repo_root=workspace_dir(),
            target_path=_SOUL_REL,
            action="append_to_section",
            content=bullet_line,
            section=section,
        )
    except ProposeError as exc:
        return None, ({"error": "invalid_proposal", "detail": str(exc)}, 400)
    except OSError as exc:
        log.exception("soul_proposal commit failed")
        return None, ({"error": "commit_failed", "detail": str(exc)}, 500)

    try:
        content_after = resolve_proposable_path(_SOUL_REL).read_text(encoding="utf-8")
    except OSError:
        content_after = ""
    await _broadcast_envelope(
        app,
        "soul_updated",
        {
            "path": _SOUL_REL,
            "label": "Soul",
            "content": content_after,
            "source": "workspace_decision",
            "hash_after": applied.hash_after,
            "no_op_reason": applied.no_op_reason,
        },
    )
    return {
        "target_path": _SOUL_REL,
        "hash_after": applied.hash_after,
        "no_op_reason": applied.no_op_reason,
    }, None


async def _commit_feedback_proposal(
    app: web.Application,
    ev: WorkspaceEvent,
) -> tuple[dict[str, Any] | None, tuple[dict[str, Any], int] | None]:
    """Apply a ``feedback_proposal`` (merge_into / archive) or a
    ``feedback_sweep`` (memory_save) via the tool that action names.

    One handler for both kinds: a sweep card is a
    feedback proposal shaped for a save instead of a merge, and it reaches
    this same function rather than a twelfth handler. Resolves the live tool
    off the Mirror's tool registry so the call reuses the bound
    `MemoryStore` + `MemoryIndex`. If the registry isn't wired (early-boot,
    CLI smoke test), we surface 503 rather than silently marking the event
    applied — the operator's decision is durable as ``pending`` and they can
    retry once the registry is up.
    """
    from tesseract.kernel.tools.base import ToolContext
    from tesseract.kernel.tools.memory_promote import MemoryPromoteInput
    from tesseract.kernel.tools.memory_save import MemorySaveInput

    payload = ev.payload or {}
    action = str(payload.get("action") or "").strip()
    if action not in {"merge_into", "archive", "memory_save"}:
        return None, (
            {"error": "unsupported_action", "detail": f"action {action!r} not handled"},
            400,
        )

    registry = app.get("tool_registry") if hasattr(app, "get") else None
    tool_name = "memory_save" if action == "memory_save" else "memory_promote"
    tool = registry.get(tool_name) if registry is not None and hasattr(registry, "get") else None
    if tool is None:
        return None, (
            {
                "error": "tool_unavailable",
                "detail": f"{tool_name} tool not registered; retry once Mirror finishes booting",
            },
            503,
        )

    ctx = ToolContext(session_id="workspace", current_call_id=ev.event_id)

    if action == "memory_save":
        # The sweep proposes a title/summary/importance/quote, not a memory
        # id: it read a transcript, not the store. `memory_save`'s own
        # required shape (type, title, content) is adapted here, once, rather
        # than teaching the job to speak `MemorySaveInput` or writing a
        # second save path.
        proposed = payload.get("proposed")
        if not isinstance(proposed, dict):
            return None, (
                {"error": "invalid_proposal", "detail": "no proposed record to save"}, 400,
            )
        title = str(proposed.get("title") or "").strip()
        summary = str(proposed.get("summary") or "").strip()
        if not title or not summary:
            return None, (
                {"error": "invalid_proposal", "detail": "memory_save requires a title and a summary"},
                400,
            )
        source_quote = str(proposed.get("source_quote") or "").strip()
        content = summary
        if source_quote:
            content = f"{summary}\n\nThe operator's own words: \"{source_quote}\""
        try:
            importance = int(proposed.get("importance", 6))
        except (TypeError, ValueError):
            importance = 6
        res = await tool.run(
            MemorySaveInput(
                # A sweep proposal is a directive-shaped operator statement
                # the module docstring says it looks for; `feedback` is the
                # memory type that governs behaviour from then on, which is
                # what such a statement is for.
                type="feedback",
                title=title,
                content=content,
                summary=summary,
                importance=importance,
            ),
            ctx,
        )
        if res.is_error:
            return None, ({"error": "memory_save_failed", "detail": res.output}, 500)
        meta = res.metadata or {}
        return {
            "action": "memory_save",
            "memory_id": meta.get("memory_id"),
            "output": res.output,
        }, None

    if action == "merge_into":
        keep = str(payload.get("keep") or "").strip()
        absorb_raw = payload.get("absorb") or []
        absorb = [str(x).strip() for x in absorb_raw if str(x).strip()] if isinstance(absorb_raw, list) else []
        if not keep or not absorb:
            return None, (
                {"error": "invalid_proposal", "detail": "merge_into requires keep + absorb"},
                400,
            )

        # All-or-nothing: if any source fails the event stays pending so
        # the operator can investigate without the inbox card disappearing
        # to History. ``memory_promote._merge`` is idempotent on already-
        # archived sources (see its docstring), so retrying after a
        # partial failure does not double-append bodies for the ones that
        # already succeeded.
        results: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        for src in absorb:
            res = await tool.run(
                MemoryPromoteInput(memory_id=src, action="merge_into", target=keep),
                ctx,
            )
            entry = {"source": src, "ok": not res.is_error, "output": res.output}
            results.append(entry)
            if res.is_error:
                failures.append(entry)
        if failures:
            return None, (
                {"error": "merge_failed", "failures": failures, "results": results},
                500,
            )
        return {
            "action": "merge_into",
            "target": keep,
            "merged": len(results),
            "results": results,
        }, None

    # action == "archive"
    memory_id = str(payload.get("memory_id") or "").strip()
    if not memory_id:
        return None, ({"error": "invalid_proposal", "detail": "archive requires memory_id"}, 400)
    res = await tool.run(
        MemoryPromoteInput(memory_id=memory_id, action="archive"),
        ctx,
    )
    if res.is_error:
        return None, ({"error": "archive_failed", "detail": res.output}, 500)
    return {"action": "archive", "memory_id": memory_id, "output": res.output}, None


async def _commit_vault_raw_ingest_batch(
    app: web.Application | None,
    ev: WorkspaceEvent,
    *,
    deny_all: bool,
    per_file: dict[str, str] | None = None,
) -> tuple[dict[str, Any] | None, tuple[dict[str, Any], int] | None]:
    """Apply (or deny) a ``vault_raw_ingest_batch`` event.

    Approve → run ``vault_raw_watch.apply_ask_batch`` with every file
    marked approved (operator may pass `decisions: {relpath: "denied"}`
    in the body to override per-file). Reject → log every file denied.

    The handler reuses the live VaultManager + VaultIndexer attached to
    the Mirror's tool registry; falls back to constructing fresh ones
    when the registry is not yet wired (CLI / test harness paths).
    """
    from tesseract.scheduler.tasks.vault_raw_watch import apply_ask_batch, cursor_path

    payload = ev.payload or {}
    files = payload.get("files") or []
    if not isinstance(files, list):
        return None, ({"error": "invalid_batch", "detail": "files must be a list"}, 400)

    # Handed in rather than read off a request. The cockpit sends a verdict
    # per file in its decision body; a caller with no request (a tool, a
    # channel) sends none and every file follows the batch verdict.
    decisions: dict[str, str] = {
        relpath: verdict.strip().lower()
        for relpath, verdict in (per_file or {}).items()
        if isinstance(relpath, str) and isinstance(verdict, str)
        and verdict.strip().lower() in {"approved", "denied"}
    }
    if deny_all:
        for entry in files:
            relpath = entry.get("relpath") if isinstance(entry, dict) else None
            if isinstance(relpath, str):
                decisions[relpath] = "denied"

    vault_manager, indexer, librarian = _resolve_vault_dependencies(app)
    # `cursor_path()` honours a TESSERACT_HOME override unconditionally. This
    # route used to honour it only when `app` was not None, which disagreed with
    # the job writing the same file: `_resolve_home`'s own reason for reading the
    # environment is that a test must not touch the production tree. One file,
    # one answer, and the unified one is the job's.
    cursor = cursor_path()

    try:
        summary = await apply_ask_batch(
            files=files,
            decisions=decisions,
            vault_manager=vault_manager,
            indexer=indexer,
            cursor_path=cursor,
            librarian=librarian,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("vault_raw_ingest_batch apply crashed")
        return None, ({"error": "apply_crashed", "detail": str(exc)}, 500)

    return summary, None


def _resolve_vault_dependencies(app: Any) -> tuple[Any, Any, Any]:
    """Pull live VaultManager + VaultIndexer + VaultLibrarian off the Mirror
    tool registry so the approve handler reuses the same handles the runtime
    configured (FAISS + FTS + wiki compile). Falls back to fresh instances
    when the registry is not wired (CLI / test harness)."""
    from tesseract.memory.vault_indexer import VaultIndexer
    from tesseract.memory.vault_manager import VaultManager
    from tesseract.paths import TESSERACT_HOME

    if app is not None and hasattr(app, "get"):
        registry = app.get("tool_registry")
        if registry is not None:
            tool = getattr(registry, "get", lambda _name: None)("vault_ingest")
            if tool is not None:
                vm = getattr(tool, "_manager", None)
                idx = getattr(tool, "_indexer", None)
                librarian = getattr(tool, "_librarian", None)
                if isinstance(vm, VaultManager):
                    return (
                        vm,
                        idx if isinstance(idx, VaultIndexer) else None,
                        librarian,
                    )
    home_override = os.environ.get("TESSERACT_HOME")
    home = Path(home_override).resolve() if home_override else TESSERACT_HOME
    return VaultManager(vault_root=home / "vault"), None, None

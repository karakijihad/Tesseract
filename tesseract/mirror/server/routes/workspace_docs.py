"""Operator direct editing of the workspace documents.

The assistant proposes; the operator writes. Both land through the same
`apply_change` — the operator path skips only the proposal card, not the
hash check or the atomic commit, so a direct save racing a pending
proposal settles the same way a second Approve would: whoever wrote
first wins and the loser re-reviews against fresh bytes.

Reachable only from the Mirror (local-only, no auth) and only for the
`PROPOSABLE_PATHS` allowlist. `file_write` still cannot touch these
files, so this does not widen what a tool can reach.

Split out of `routes/workspace.py`: the decision dispatcher and its
commit handlers are one domain (`workspace.py`,
`workspace_skill_commits.py`, `workspace_proposal_commits.py`); the three
GET/POST handlers here, reading and writing a document directly rather than
through a proposal, are the other.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from aiohttp import web

from tesseract.kernel.workspace_changes import (
    PROPOSABLE_PATHS,
    ConcurrentModificationError,
    ProposeError,
    apply_change,
    compute_diff,
    hash_text,
    resolve_proposable_path,
)
from tesseract.mirror.server.routes.workspace_shared import _broadcast_envelope
from tesseract.paths import workspace_dir
from tesseract.permissions.approval_log import record_ask

log = logging.getLogger(__name__)

_SOUL_REL = "tesseract/workspace/SOUL.md"


def _doc_row(target_path: str, spec: dict[str, object]) -> dict[str, Any]:
    path = resolve_proposable_path(target_path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {
            "path": target_path,
            "label": str(spec.get("label") or ""),
            "exists": False,
            "bytes": 0,
            "lines": 0,
            "hash": "",
            "modified_at": None,
        }
    try:
        modified_at = path.stat().st_mtime
    except OSError:
        modified_at = None
    return {
        "path": target_path,
        "label": str(spec.get("label") or ""),
        "exists": True,
        "bytes": len(text.encode("utf-8")),
        "lines": text.count("\n") + (0 if text.endswith("\n") or not text else 1),
        "hash": hash_text(text),
        "modified_at": modified_at,
    }


async def list_docs(request: web.Request) -> web.Response:
    """GET /api/workspace/docs — the editable workspace documents.

    One row per `PROPOSABLE_PATHS` entry, present or not: a doc missing
    from the operator's workspace is a fact the tab should show, not a
    row it should silently drop.
    """
    rows = await asyncio.to_thread(
        lambda: [_doc_row(path, spec) for path, spec in PROPOSABLE_PATHS.items()]
    )
    return web.json_response({"docs": rows, "count": len(rows)})


async def get_doc(request: web.Request) -> web.Response:
    """GET /api/workspace/doc?path=tesseract/workspace/SOUL.md — read one.

    `hash` is the concurrency token the save must echo back.
    """
    target_path = (request.query.get("path") or "").strip().replace("\\", "/")
    if target_path not in PROPOSABLE_PATHS:
        return web.json_response(
            {"error": "not_editable", "detail": f"path {target_path!r} is not an editable workspace document"},
            status=400,
        )
    path = resolve_proposable_path(target_path)
    try:
        content = await asyncio.to_thread(path.read_text, encoding="utf-8")
    except OSError as exc:
        return web.json_response(
            {"error": "read_failed", "detail": str(exc)}, status=404,
        )
    return web.json_response({
        "path": target_path,
        "label": str(PROPOSABLE_PATHS[target_path].get("label") or ""),
        "content": content,
        "hash": hash_text(content),
    })


async def save_doc(request: web.Request) -> web.Response:
    """POST /api/workspace/doc — operator-authored replacement of one doc.

    Body: ``{path, content, expected_hash}``. `expected_hash` is the
    `hash` from the read that seeded the editor; a mismatch means the file
    moved underneath the operator (the assistant's proposal was approved,
    an external editor saved) and returns 409 with the current bytes so
    they re-review rather than clobber.
    """
    app = request.app
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "body must be an object"}, status=400)

    target_path = str(body.get("path") or "").strip().replace("\\", "/")
    if target_path not in PROPOSABLE_PATHS:
        return web.json_response(
            {"error": "not_editable", "detail": f"path {target_path!r} is not an editable workspace document"},
            status=400,
        )
    content = body.get("content")
    if not isinstance(content, str):
        return web.json_response({"error": "content must be a string"}, status=400)
    expected_hash = body.get("expected_hash")
    if not isinstance(expected_hash, str) or not expected_hash:
        return web.json_response(
            {"error": "expected_hash is required — re-open the document to get one"},
            status=400,
        )

    try:
        applied = await asyncio.to_thread(
            apply_change,
            repo_root=workspace_dir(),
            target_path=target_path,
            action="replace",
            content=content,
            expected_hash_before=expected_hash,
        )
    except ConcurrentModificationError as exc:
        try:
            current = resolve_proposable_path(target_path).read_text(encoding="utf-8")
        except OSError:
            current = ""
        return web.json_response({
            "error": "concurrent_modification",
            "detail": str(exc),
            "expected_hash_before": exc.expected,
            "actual_hash": exc.actual,
            "current_content": current,
            "diff": compute_diff(
                current, content,
                target_label=str(PROPOSABLE_PATHS[target_path].get("label") or "file"),
            ),
        }, status=409)
    except ProposeError as exc:
        return web.json_response({"error": "invalid_edit", "detail": str(exc)}, status=400)
    except OSError as exc:
        log.exception("workspace doc save failed")
        return web.json_response({"error": "save_failed", "detail": str(exc)}, status=500)

    label = str(PROPOSABLE_PATHS[target_path].get("label") or "")
    await _broadcast_envelope(
        app,
        "soul_updated" if target_path == _SOUL_REL else "workspace_file_updated",
        {
            "path": target_path,
            "label": label,
            "content": content,
            "source": "operator_edit",
            "hash_after": applied.hash_after,
            "no_op_reason": applied.no_op_reason,
        },
    )

    try:
        await record_ask(
            session_id="workspace",
            call_id=applied.hash_after,
            tool_name="workspace_doc_save",
            input_summary={
                "target_path": target_path,
                "bytes_before": applied.bytes_before,
                "bytes_after": applied.bytes_after,
            },
            posture_source="operator_edit",
            result="allow_once",
            actor="operator",
        )
    except Exception:
        log.exception("workspace: doc-save ledger record failed")

    return web.json_response({
        "path": target_path,
        "label": label,
        "hash": applied.hash_after,
        "bytes": applied.bytes_after,
        "no_op_reason": applied.no_op_reason,
    })

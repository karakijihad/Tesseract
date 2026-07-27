"""FeedbackConsolidatorJob — Layer B of the feedback durability plan.

Weekly LLM pass over the active feedback set. Emits *proposals* — never
mutates a memory. Operator approves via the Workspace Inbox; the
``memory_promote`` tool then executes the approved action.

Three proposal kinds:

- ``merges``    — pairs/groups whose intent overlaps. Operator picks
                  which is the keeper; the rest archive into it.
- ``soul``      — patterns recurring across ≥3 records that describe
                  stable identity/tone, not transient corrections.
                  Becomes a SOUL.md Growth bullet on approval.
- ``archives``  — superseded or stale records.

Disabled by default in ``schedule.yaml``. Operator flips it on once
Layer A is verified dry. Mirrors ``feedback_sweep``'s shape: single
read-only LLM turn, JSONL output, WS broadcast.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tesseract.kernel.adapters.base import AdapterOptions, ModelAdapter
from tesseract.memory.store import MemoryStore
from tesseract.memory.types import MemoryFrontmatter
from tesseract.paths import TESSERACT_HOME
from tesseract.scheduler.base_job import BaseJob
from tesseract.scheduler.role_chain import build_chain_for_job
from tesseract.scheduler.tasks.feedback_sweep import _extract_first_json_object
from tesseract.scheduler.types import JobContext, JobResult

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 60.0
DEFAULT_MIN_RECORDS = 3
ENVELOPE_KIND = "feedback_proposals"

_PROMPT = (
    "You are reviewing the active operator-feedback memories that TARS "
    "uses to keep its behavior aligned. Your job is to keep this set "
    "*sharp, not big*: identify duplicates that should merge, patterns "
    "that have hardened into identity (and belong in SOUL.md Growth), "
    "and stale records that should archive.\n\n"
    "Return ONLY a JSON object with this shape, no preamble:\n"
    "{\n"
    '  "merges":   [{"keep": "<id>", "absorb": ["<id>", ...], "reason": "<why>"}],\n'
    '  "soul":     [{"bullet": "<≤240 chars>", "supporting_ids": ["<id>", ...]}],\n'
    '  "archives": [{"id": "<id>", "reason": "<why>"}]\n'
    "}\n\n"
    "Rules:\n"
    "- Only propose a merge when intent genuinely overlaps. A merge is\n"
    "  not 'these touch the same area' — it's 'the operator would not\n"
    "  notice if one disappeared into the other'.\n"
    "- Only propose a soul bullet when ≥3 records describe the same\n"
    "  stable pattern about working with the operator. One-offs and\n"
    "  recent corrections do NOT belong in SOUL.\n"
    "- Only propose archive when a record is contradicted, superseded,\n"
    "  or describes a workflow that no longer exists.\n"
    "- If nothing qualifies in a category, return an empty list — do not\n"
    "  invent proposals.\n"
)


class FeedbackConsolidatorJob(BaseJob):
    uses_llm = True
    default_model_role = "feedback_consolidator"

    async def run(self, ctx: JobContext) -> JobResult:
        t0 = time.monotonic()
        try:
            target_date = ctx.fired_at.date()
            store_dir = _resolve_store_dir(ctx)
            # floor=1 inside the helper — consolidator reviews ALL active
            # candidates; the prompt-side floor (DIRECTIVES_IMPORTANCE_FLOOR=6)
            # is Layer A's surfacing concern, not consolidation's.
            records = _load_active_feedback(store_dir)
            min_records = int(ctx.config.get("min_records", DEFAULT_MIN_RECORDS))
            if len(records) < min_records:
                return _ok(
                    ctx, t0, target_date, len(records), 0,
                    f"only {len(records)} active records — below floor {min_records}",
                )

            chain = build_chain_for_job(
                ctx,
                default_role=FeedbackConsolidatorJob.default_model_role,
                log_label="feedback_consolidator",
            )
            if not chain:
                return _ok(
                    ctx, t0, target_date, len(records), 0,
                    "role unavailable — skipped",
                )

            prompt = _build_prompt(records)
            raw = await _call_with_fallback(prompt, chain, DEFAULT_TIMEOUT_S)
            proposals = _parse_proposals(raw)
            total = (
                len(proposals["merges"])
                + len(proposals["soul"])
                + len(proposals["archives"])
            )

            log_dir = _resolve_log_dir(ctx)
            log_path = _write_jsonl(log_dir, target_date, proposals, records)
            _emit_inbox_events(ctx, target_date, proposals, log_path)

            await _broadcast(ctx, target_date, proposals, log_path)

            return _ok(
                ctx, t0, target_date, len(records), total,
                f"records={len(records)} proposals={total}",
                payload_extra={"log_path": str(log_path), "kinds": {
                    "merges": len(proposals["merges"]),
                    "soul": len(proposals["soul"]),
                    "archives": len(proposals["archives"]),
                }},
            )
        except Exception as exc:  # noqa: BLE001 — handler contract forbids raising
            log.exception("feedback_consolidator crashed")
            return JobResult(
                job_name=ctx.job_name,
                run_id=ctx.run_id,
                ok=False,
                detail=f"unhandled: {exc!r}",
                duration_ms=(time.monotonic() - t0) * 1000.0,
            )


def _ok(
    ctx: JobContext,
    t0: float,
    target_date: Any,
    records: int,
    proposals: int,
    detail: str,
    *,
    payload_extra: dict[str, Any] | None = None,
) -> JobResult:
    payload = {
        "target_date": target_date.isoformat(),
        "records": records,
        "proposals": proposals,
    }
    if payload_extra:
        payload.update(payload_extra)
    return JobResult(
        job_name=ctx.job_name,
        run_id=ctx.run_id,
        ok=True,
        detail=detail,
        payload=payload,
        duration_ms=(time.monotonic() - t0) * 1000.0,
    )


def _resolve_store_dir(ctx: JobContext) -> Path:
    override = ctx.config.get("store_dir")
    if override:
        return Path(override)
    app = ctx.app
    if app is not None and hasattr(app, "get"):
        tdir = app.get("tesseract_dir")
        if tdir is not None:
            return Path(tdir) / "memory-store"
    return TESSERACT_HOME / "memory-store"


def _resolve_log_dir(ctx: JobContext) -> Path:
    override = ctx.config.get("log_dir")
    if override:
        return Path(override)
    app = ctx.app
    if app is not None and hasattr(app, "get"):
        tdir = app.get("tesseract_dir")
        if tdir is not None:
            return Path(tdir) / "logs" / "consolidator"
    return TESSERACT_HOME / "logs" / "consolidator"


def _load_active_feedback(store_dir: Path) -> list[MemoryFrontmatter]:
    """Active operator-directive records (``feedback`` ∪ ``user``).

    The consolidator treats both subdirs as one pool — the operator may
    have saved a durable rule under ``user`` (identity / preference shape)
    or ``feedback`` (correction shape); either way it's directive material
    that drifts and overlaps the same way. ``importance_floor=1`` keeps
    everything for the LLM's review (the prompt-side floor lives in
    Layer A; consolidation should consider all candidates, not just the
    surfaced ones).
    """
    if not store_dir.exists():
        return []
    try:
        store = MemoryStore(store_dir)
        return store.list_active_directives(importance_floor=1)
    except Exception:
        log.exception("feedback_consolidator: failed to load directives")
        return []


def _build_prompt(records: list[MemoryFrontmatter]) -> str:
    lines = []
    for fm in sorted(records, key=lambda f: (-f.importance, f.id)):
        lines.append(
            f"- id={fm.id} importance={fm.importance} title={fm.title!r} "
            f"summary={fm.summary!r}"
        )
    return (
        f"{_PROMPT}\n--- ACTIVE FEEDBACK ({len(records)} records) ---\n"
        + "\n".join(lines)
        + "\n--- END ---\n"
    )


async def _call_with_fallback(
    prompt: str,
    chain: list[tuple[ModelAdapter, AdapterOptions]],
    timeout_s: float,
) -> str:
    for adapter, options in chain:
        label = f"{options.provider or '?'}/{options.model or '?'}"
        try:
            out = await asyncio.wait_for(
                adapter.generate(prompt, options),
                timeout=timeout_s,
            )
        except asyncio.TimeoutError:
            log.warning("feedback_consolidator: %s timed out after %.1fs", label, timeout_s)
            continue
        except Exception as exc:  # noqa: BLE001
            log.warning("feedback_consolidator: %s call failed (%s)", label, exc)
            continue
        if out and out.strip():
            return out
        log.warning("feedback_consolidator: %s returned empty", label)
    return ""


def _parse_proposals(raw: str) -> dict[str, list[dict[str, Any]]]:
    """Return ``{merges, soul, archives}`` lists; missing kinds become empty."""
    empty = {"merges": [], "soul": [], "archives": []}
    if not raw or not raw.strip():
        return empty
    blob = _extract_first_json_object(raw)
    if blob is None:
        log.warning("feedback_consolidator: no JSON object in adapter output")
        return empty
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        log.warning("feedback_consolidator: JSON parse failed")
        return empty
    if not isinstance(data, dict):
        return empty
    return {
        "merges": _clean_merges(data.get("merges")),
        "soul": _clean_soul(data.get("soul")),
        "archives": _clean_archives(data.get("archives")),
    }


def _clean_merges(items: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not isinstance(items, list):
        return out
    for item in items:
        if not isinstance(item, dict):
            continue
        keep = (item.get("keep") or "").strip()
        absorb = item.get("absorb")
        if not keep or not isinstance(absorb, list):
            continue
        absorb_clean = [a.strip() for a in absorb if isinstance(a, str) and a.strip() and a.strip() != keep]
        if not absorb_clean:
            continue
        out.append({
            "keep": keep,
            "absorb": absorb_clean,
            "reason": (item.get("reason") or "").strip(),
        })
    return out


def _clean_soul(items: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not isinstance(items, list):
        return out
    for item in items:
        if not isinstance(item, dict):
            continue
        bullet = (item.get("bullet") or "").strip()
        if not bullet or len(bullet) > 240:
            continue
        ids = item.get("supporting_ids")
        if not isinstance(ids, list) or len(ids) < 3:
            continue
        ids_clean = [i.strip() for i in ids if isinstance(i, str) and i.strip()]
        if len(ids_clean) < 3:
            continue
        out.append({"bullet": bullet, "supporting_ids": ids_clean})
    return out


def _clean_archives(items: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not isinstance(items, list):
        return out
    for item in items:
        if not isinstance(item, dict):
            continue
        rec_id = (item.get("id") or "").strip()
        if not rec_id:
            continue
        out.append({"id": rec_id, "reason": (item.get("reason") or "").strip()})
    return out


def _write_jsonl(
    log_dir: Path,
    target_date: Any,
    proposals: dict[str, list[dict[str, Any]]],
    records: list[MemoryFrontmatter],
) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"{target_date.isoformat()}.jsonl"
    written_at = datetime.now(timezone.utc).isoformat()
    record_ids = [fm.id for fm in records]
    with path.open("w", encoding="utf-8") as fh:
        for kind, items in proposals.items():
            for prop in items:
                entry = {
                    "written_at": written_at,
                    "target_date": target_date.isoformat(),
                    "kind": kind,
                    "active_record_ids": record_ids,
                    **prop,
                }
                fh.write(json.dumps(entry) + "\n")
    return path


def _emit_inbox_events(
    ctx: JobContext,
    target_date: Any,
    proposals: dict[str, list[dict[str, Any]]],
    log_path: Path,
) -> None:
    """One Workspace Inbox event per proposal — operator approves there.

    Best-effort: if the EventStore is unavailable (test harness, missing
    logs dir), the JSONL on disk is still authoritative and the
    ``feedback_proposals`` WS envelope still fires.
    """
    try:
        from tesseract.workspace_events import EventStore
    except ImportError:
        return
    try:
        log_dir = _resolve_log_dir(ctx)
        store = EventStore(log_dir.parent)
    except Exception:
        log.exception("feedback_consolidator: EventStore init failed")
        return

    for prop in proposals["merges"]:
        keep = prop.get("keep", "?")
        absorb = prop.get("absorb", [])
        title = f"Merge {len(absorb)} record(s) → {keep}"
        summary = (
            prop.get("reason") or "feedback_consolidator proposes a merge"
        )[:1200]
        try:
            from tesseract.workspace_events import WorkspaceEvent
            store.append_event(WorkspaceEvent.new(
                kind="feedback_proposal",
                source="feedback_consolidator",
                title=title,
                summary=summary,
                payload={
                    "action": "merge_into",
                    "keep": keep,
                    "absorb": absorb,
                    "target_date": target_date.isoformat(),
                    "log_path": str(log_path),
                },
            ))
        except Exception:
            log.exception("feedback_consolidator: append merge event failed")

    # Layer-2 dedup: skip emitting a `soul_proposal` if a pending event
    # with the same normalized bullet text is already in the inbox.
    # Stops weekly re-runs (or back-to-back manual fires) from stacking
    # identical cards in the operator's queue while a prior proposal is
    # still awaiting decision. The kernel-side `_append_to_named_section`
    # is the authoritative dedup at commit time; this is the upstream
    # noise filter so the operator never sees the duplicate to begin with.
    from tesseract.kernel.workspace_changes import _normalize_bullet
    pending_soul = store.list_events(kinds=("soul_proposal",), status="pending")
    pending_norms = {
        _normalize_bullet(str((ev.payload or {}).get("bullet", "")))
        for ev in pending_soul
    }
    pending_norms.discard("")

    for prop in proposals["soul"]:
        bullet = prop.get("bullet", "")
        if _normalize_bullet(bullet) in pending_norms:
            log.info(
                "feedback_consolidator: skipped duplicate soul_proposal (%d-char bullet)",
                len(bullet),
            )
            continue
        title = f"Soul-growth bullet (×{len(prop.get('supporting_ids', []))})"
        try:
            from tesseract.workspace_events import WorkspaceEvent
            store.append_event(WorkspaceEvent.new(
                kind="soul_proposal",
                source="feedback_consolidator",
                title=title,
                summary=bullet[:1200],
                payload={
                    "action": "propose_soul_growth",
                    "bullet": bullet,
                    "supporting_ids": prop.get("supporting_ids", []),
                    "target_date": target_date.isoformat(),
                    "log_path": str(log_path),
                },
            ))
            pending_norms.add(_normalize_bullet(bullet))
        except Exception:
            log.exception("feedback_consolidator: append soul event failed")

    for prop in proposals["archives"]:
        rec_id = prop.get("id", "?")
        title = f"Archive {rec_id}"
        summary = (
            prop.get("reason") or "feedback_consolidator proposes archive"
        )[:1200]
        try:
            from tesseract.workspace_events import WorkspaceEvent
            store.append_event(WorkspaceEvent.new(
                kind="feedback_proposal",
                source="feedback_consolidator",
                title=title,
                summary=summary,
                payload={
                    "action": "archive",
                    "memory_id": rec_id,
                    "target_date": target_date.isoformat(),
                    "log_path": str(log_path),
                },
            ))
        except Exception:
            log.exception("feedback_consolidator: append archive event failed")


async def _broadcast(
    ctx: JobContext,
    target_date: Any,
    proposals: dict[str, list[dict[str, Any]]],
    log_path: Path,
) -> None:
    app = ctx.app
    if app is None or not hasattr(app, "get"):
        return
    sessions = app.get("server_sessions") or {}
    if not sessions:
        return
    try:
        from tesseract.mirror.server.envelope import make_envelope
        from tesseract.mirror.server.session import send_envelope
    except ImportError:
        return
    payload = {
        "source": "feedback_consolidator",
        "target_date": target_date.isoformat(),
        "proposals": proposals,
        "log_path": str(log_path),
    }
    for sess in sessions.values():
        env = make_envelope(
            ENVELOPE_KIND, "background",
            getattr(sess, "session_id", ""), payload,
        )
        try:
            await send_envelope(sess, env)
        except Exception:
            log.exception(
                "feedback_consolidator: send_envelope failed for %s",
                getattr(sess, "session_id", "?"),
            )

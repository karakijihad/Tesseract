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
from types import SimpleNamespace
from typing import Any

from tesseract.lib.clock import to_local
from tesseract.kernel.adapters.base import AdapterOptions, ModelAdapter
from tesseract.kernel.workspace_changes import SOUL_GROWTH_SECTIONS
from tesseract.memory.store import MemoryStore
from tesseract.memory.types import MemoryFrontmatter
from tesseract.paths import TESSERACT_HOME, log_dir, workspace_dir
from tesseract.scheduler.base_job import BaseJob
from tesseract.scheduler.role_chain import build_chain_for_job
from tesseract.scheduler.tasks.feedback_sweep import _extract_first_json_object
from tesseract.scheduler.types import JobContext, JobResult

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 60.0
DEFAULT_MIN_RECORDS = 3
ENVELOPE_KIND = "feedback_proposals"

_PROMPT = (
    "You are reviewing the active operator-feedback memories that the assistant "
    "uses to keep its behavior aligned. Your job is to keep this set "
    "*sharp, not big*: identify duplicates that should merge, patterns "
    "that have hardened into identity (and belong in SOUL.md), "
    "and stale records that should archive.\n\n"
    "Return ONLY a JSON object with this shape, no preamble:\n"
    "{\n"
    '  "merges":   [{"keep": "<id>", "absorb": ["<id>", ...], "reason": "<why>"}],\n'
    '  "soul":     [{"section": "<one named below>", "bullet": "<≤240 chars>", '
    '"supporting_ids": ["<id>", ...]}],\n'
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
    "\n"
    "Which part of the soul a bullet belongs to, and what each is for:\n"
    # From the one list that declares them, so a renamed section reaches the
    # model without anybody remembering this prompt exists.
    + "".join(f"- {name}: {purpose}\n" for name, purpose in SOUL_GROWTH_SECTIONS.items())
)


class FeedbackConsolidatorJob(BaseJob):
    uses_llm = True
    # A chain, not a role — see `feedback_sweep`, which shares both the chain
    # and the reason.
    default_model_chain = "chain_1"

    async def run(self, ctx: JobContext) -> JobResult:
        t0 = time.monotonic()
        try:
            target_date = to_local(ctx.fired_at).date()
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
                default_role=None,
                default_chain=FeedbackConsolidatorJob.default_model_chain,
                log_label="feedback_consolidator",
            )
            if not chain:
                return _ok(
                    ctx, t0, target_date, len(records), 0,
                    "no model reachable — skipped",
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
    return log_dir("consolidator")


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
        # Which part of the soul it belongs to. Dropped rather than defaulted
        # when the model names something that is not a section: SOUL holds a
        # section per kind of growth, and filing a bullet under one of them
        # because the answer was unreadable is the drift the sections exist to
        # stop. The operator sees one card fewer, not a miscategorised one.
        section = (item.get("section") or "").strip()
        if section not in SOUL_GROWTH_SECTIONS:
            log.info(
                "feedback_consolidator: soul bullet named no known section (%r); dropped",
                section,
            )
            continue
        out.append({
            "section": section,
            "bullet": bullet,
            "supporting_ids": ids_clean,
        })
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


_SOUL_REL = "tesseract/workspace/SOUL.md"


def _emit_soul_proposals(
    ctx: JobContext,
    store: Any,
    proposals: list[dict[str, Any]],
    target_date: Any,
    log_path: Path,
) -> None:
    """A soul bullet from this job goes through the door every other one does.

    It used to file its own `soul_proposal` kind, whose apply path called
    `apply_change` directly and consulted no posture, so a bullet from here sat
    waiting for an approval while the identical bullet proposed in a chat turn
    applied itself. `permissions.yaml::workspace_documents` is the operator's
    statement of which documents are theirs to hold, and one producer ignoring
    it makes the statement untrue rather than partly true.

    So: the same `change_proposal` event, the same `document_posture` reader,
    the same `settle_proposal` door. SOUL.md is unheld, so under `free` this
    now applies itself and files the card `applied` with the whole diff on it.
    OPERATING, WORKSHOP and CHANNEL stay held, and would still ask if a
    producer ever proposed one.
    """
    from tesseract.kernel.workspace_changes import (
        PROPOSABLE_PATHS,
        _normalize_bullet,
        compute_diff,
        document_posture,
        hash_text,
        preview_change,
        settle_proposal,
        validate_action,
        validate_target,
    )
    from tesseract.workspace_events import WorkspaceEvent

    if not proposals:
        return

    # The live policy, which is what `/mode` changes. A job with no app has no
    # operator to ask either, and `document_posture` answers `ask` for that.
    context = SimpleNamespace(policy=None)
    app = getattr(ctx, "app", None)
    if app is not None:
        try:
            context = SimpleNamespace(policy=app["config"].permissions)
        except (KeyError, AttributeError, TypeError):
            log.warning("feedback_consolidator: no live permission policy; bullets will ask")

    try:
        full_path = validate_target(workspace_dir(), _SOUL_REL)
        action = validate_action(_SOUL_REL, "append_to_section")
        label = str(PROPOSABLE_PATHS[_SOUL_REL]["label"])
    except Exception:
        log.exception("feedback_consolidator: SOUL.md is not proposable")
        return

    # Still pending from an earlier run, so a card is not raised twice while
    # the operator has not answered the first.
    pending = {
        _normalize_bullet(str((ev.payload or {}).get("summary", "")))
        for ev in store.list_events(kinds=("change_proposal",), status="pending")
        if (ev.payload or {}).get("target_path") == _SOUL_REL
    }
    pending.discard("")

    for prop in proposals:
        bullet = prop.get("bullet", "")
        section = prop.get("section", "")
        if not bullet or not section:
            continue
        if _normalize_bullet(bullet) in pending:
            log.info("feedback_consolidator: bullet already waiting, not raised again")
            continue

        bullet_line = f"- {bullet}\n"
        try:
            before = full_path.read_text(encoding="utf-8")
            after = preview_change(
                current_text=before,
                action=action,
                content=bullet_line,
                section=section,
            )
        except Exception:
            log.exception("feedback_consolidator: could not prepare a soul bullet")
            continue

        # The soul already says this. Reading the document before proposing a
        # change to it is the difference between a card the operator dismisses
        # and a card they never see: the dedup below this used to be the only
        # one, and it fires at apply time, which is after the interruption.
        if after == before:
            log.info("feedback_consolidator: bullet is already in the soul, not proposed")
            continue

        expected_hash_before = hash_text(before)
        event = WorkspaceEvent.new(
            kind="change_proposal",
            source="feedback_consolidator",
            title=f"Soul · {section} — {bullet[:70]}",
            summary=bullet,
            payload={
                "target_path": _SOUL_REL,
                "label": label,
                "action": action,
                "content": bullet_line,
                "section": section,
                "summary": bullet,
                "expected_hash_before": expected_hash_before,
                "bytes_before": len(before.encode("utf-8")),
                "bytes_after": len(after.encode("utf-8")),
                "diff": compute_diff(before, after, target_label=label),
                "kind_origin": "soul_growth",
                "supporting_ids": prop.get("supporting_ids", []),
                "target_date": target_date.isoformat(),
                "log_path": str(log_path),
            },
        )

        event, applied, error = settle_proposal(
            event=event,
            target_path=_SOUL_REL,
            action=action,
            content=bullet_line,
            section=section,
            expected_hash_before=expected_hash_before,
            posture=document_posture(context, _SOUL_REL),
        )
        if error is not None:
            log.warning("feedback_consolidator: soul bullet not settled: %s", error)
            continue

        try:
            store.append_event(event)
        except Exception:
            log.exception("feedback_consolidator: append soul event failed")
            continue
        if applied is None:
            pending.add(_normalize_bullet(bullet))


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

    _emit_soul_proposals(ctx, store, proposals["soul"], target_date, log_path)

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

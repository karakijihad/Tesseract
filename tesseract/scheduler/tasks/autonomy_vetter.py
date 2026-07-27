"""AutonomyVetterJob — Task 2B — LLM agenda quality gate.

Batched scheduled job: judges pre-queue ``AgendaStatus.UNVETTED``
proposals via the ``autonomy_vetter`` role and promotes / rejects /
merges them.

Each tick:

1. Collects up to ``DEFAULT_MAX_BATCH`` UNVETTED items (oldest first).
2. Idle short-circuit when there are none (no model bill).
3. Asks the role-chained adapter for one verdict per item.
4. Applies each verdict:
   - ``promote`` -> item.vet_score set, transitioned to PROPOSED.
   - ``reject`` -> recorded in the prune ledger (LOW_VALUE), transitioned
     to CANCELLED.
   - ``merge`` (valid ``merge_into``) -> recorded in the prune ledger
     (DUPLICATE), transitioned to SUPERSEDED. An unresolvable
     ``merge_into`` is treated as ``reject``.

Fail-safe: on missing/garbage model output (no chain, empty response, or
a hallucinated id), the corresponding item(s) stay UNVETTED and are
retried on the next tick — never auto-rejected on no data.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from tesseract.kernel.adapters.base import AdapterOptions, ModelAdapter
from tesseract.orchestrator.autonomy.agenda_store import AgendaStore
from tesseract.orchestrator.autonomy.models import AgendaStatus
from tesseract.orchestrator.autonomy.prune_ledger import (
    PruneRecord,
    PruneStage,
    record_prune,
)
from tesseract.orchestrator.autonomy.vetter.parse import VetVerdict, parse_vet_response
from tesseract.orchestrator.autonomy.vetter.prompt import build_vet_prompt
from tesseract.scheduler.base_job import BaseJob
from tesseract.scheduler.role_chain import build_chain_for_job
from tesseract.scheduler.types import JobContext, JobResult

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 45.0
DEFAULT_MAX_BATCH = 20


class AutonomyVetterJob(BaseJob):
    uses_llm = True
    default_model_role = "autonomy_vetter"

    async def run(self, ctx: JobContext) -> JobResult:
        t0 = time.monotonic()
        try:
            store = _resolve_store(ctx.app)

            unvetted = sorted(
                (i for i in store.iter_active() if i.status == AgendaStatus.UNVETTED),
                key=lambda i: i.created_at,
            )[:DEFAULT_MAX_BATCH]

            if not unvetted:
                # Idle short-circuit — never bill the model.
                return _ok(ctx, t0, detail="idle", payload={"unvetted": 0})

            chain = build_chain_for_job(
                ctx,
                default_role=AutonomyVetterJob.default_model_role,
                log_label="autonomy_vetter",
            )
            if not chain:
                # Fail-safe: items stay UNVETTED, retried next tick.
                return _ok(
                    ctx, t0,
                    detail="role_unavailable",
                    payload={"unvetted": len(unvetted)},
                )

            prompt = build_vet_prompt(
                [
                    {
                        "id": item.id,
                        "source": item.source.value,
                        "goal": item.goal,
                        "rationale": item.rationale,
                    }
                    for item in unvetted
                ]
            )
            raw = await _call_with_fallback(prompt, chain, DEFAULT_TIMEOUT_S)
            if not raw or not raw.strip():
                # Fail-safe: no data -> never auto-reject; items stay UNVETTED.
                return _ok(
                    ctx, t0,
                    detail="empty_response",
                    payload={"unvetted": len(unvetted)},
                )

            batch = parse_vet_response(raw)
            counts = _apply_verdicts(store, unvetted, batch.verdicts, fired_at=ctx.fired_at)

            detail = (
                f"unvetted={len(unvetted)} promoted={counts['promoted']} "
                f"rejected={counts['rejected']} merged={counts['merged']}"
            )
            return _ok(
                ctx, t0,
                detail=detail,
                payload={"unvetted": len(unvetted), **counts},
            )
        except Exception as exc:  # noqa: BLE001 — handler contract forbids raising
            log.exception("autonomy_vetter crashed")
            return JobResult(
                job_name=ctx.job_name,
                run_id=ctx.run_id,
                ok=False,
                detail=f"unhandled: {exc!r}",
                duration_ms=(time.monotonic() - t0) * 1000.0,
            )


def _ok(ctx: JobContext, t0: float, *, detail: str, payload: dict) -> JobResult:
    return JobResult(
        job_name=ctx.job_name,
        run_id=ctx.run_id,
        ok=True,
        detail=detail,
        payload=payload,
        duration_ms=(time.monotonic() - t0) * 1000.0,
    )


def _resolve_store(app: Any) -> AgendaStore:
    """Prefer the app's live AgendaStore instance — keeps the WS
    broadcast hook wired so the Mirror live-updates on promote/reject."""
    if app is not None and hasattr(app, "get"):
        store = app.get("agenda_store")
        if store is not None:
            return store
    return AgendaStore()


def _apply_verdicts(
    store: AgendaStore,
    unvetted: list,
    verdicts: list,
    *,
    fired_at,
) -> dict[str, int]:
    by_id = {item.id: item for item in unvetted}
    promoted = rejected = merged = 0
    for result in verdicts:
        item = by_id.get(result.id)
        if item is None:
            # Hallucinated id (not in this batch) — ignore, item(s) it
            # was meant for stay UNVETTED.
            continue
        if result.verdict == VetVerdict.PROMOTE:
            item.vet_score = result.score
            store.transition(
                item, AgendaStatus.PROPOSED,
                reason=f"vet promote {result.score:.2f}", by="kernel",
            )
            promoted += 1
        elif result.verdict == VetVerdict.REJECT:
            _reject(store, item, reason=result.reason or "vet reject", fired_at=fired_at)
            rejected += 1
        elif result.verdict == VetVerdict.MERGE:
            merge_into = result.merge_into
            if (
                merge_into is None
                or merge_into == item.id
                or store.get(merge_into) is None
            ):
                # Unresolvable (missing/self-referential) merge target -> reject.
                _reject(
                    store, item,
                    reason=result.reason or "vet reject (invalid merge target)",
                    fired_at=fired_at,
                )
                rejected += 1
            else:
                store.transition(
                    item, AgendaStatus.SUPERSEDED,
                    reason=f"merged into {merge_into}", by="kernel",
                )
                record_prune(
                    PruneRecord(
                        item_id=item.id,
                        source=item.source,
                        goal=item.goal[:500],
                        stage=PruneStage.DUPLICATE,
                        reason=f"merged into {merge_into}",
                        ts=fired_at,
                    )
                )
                merged += 1
    return {"promoted": promoted, "rejected": rejected, "merged": merged}


def _reject(store: AgendaStore, item, *, reason: str, fired_at) -> None:
    store.transition(item, AgendaStatus.CANCELLED, reason="vet reject", by="kernel")
    record_prune(
        PruneRecord(
            item_id=item.id,
            source=item.source,
            goal=item.goal[:500],
            stage=PruneStage.LOW_VALUE,
            reason=reason,
            ts=fired_at,
        )
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
            log.warning("autonomy_vetter: %s timed out after %.1fs", label, timeout_s)
            continue
        except Exception as exc:  # noqa: BLE001
            log.warning("autonomy_vetter: %s call failed (%s)", label, exc)
            continue
        if out and out.strip():
            return out
        log.warning("autonomy_vetter: %s returned empty", label)
    return ""


__all__ = ["AutonomyVetterJob"]

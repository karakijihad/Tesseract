"""MemoryScrubJob — heal the dangling-ref findings memory_lint reports.

Companion to ``memory_lint`` (read-only detector). Two modes:

  - ``mode: report`` — re-runs the linter and returns the same payload, with
    a ``scrubbable`` count of findings this job *would* repair if flipped to
    ``fix``. Default — safe to run anywhere.
  - ``mode: fix``    — actually repairs:
      * broken frontmatter ``auto_links`` / ``links`` — removes the dead id
        and re-renders the auto-managed ``## Related`` block from the
        updated list.
      * orphan zero-byte stubs — ``path.unlink()``. Idempotent: a deleted
        stub doesn't show up next run.
    Leaves untouched:
      * ``stale_source_paths`` — pointer-into-vault rot, operator-attended
        because the right repair is to move/restore the source file, not
        blank the field.
      * ``broken_wikilinks`` of kind ``missing_path`` — paths into the
        operator workspace / project root the operator should reconcile.

Wired off by default in ``schedule.yaml`` (operator opt-in). Pairs with the
``delete()`` cascade in ``MemoryStore`` — cascade prevents new dangling
refs at delete time, scrub heals refs that pre-date the cascade landing.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import ClassVar

from tesseract.memory.memory_lint import MemoryLinter
from tesseract.memory.related_block import RelatedItem, replace_related_block
from tesseract.memory.store import MemoryStore
from tesseract.paths import ROOT, TESSERACT_HOME
from tesseract.scheduler.base_job import BaseJob
from tesseract.scheduler.types import JobContext, JobResult

log = logging.getLogger(__name__)

_VALID_MODES = ("report", "fix")


class MemoryScrubJob(BaseJob):
    uses_llm: ClassVar[bool] = False

    async def run(self, ctx: JobContext) -> JobResult:
        t0 = time.monotonic()
        try:
            bundle = _resolve_bundle(ctx)
            if bundle is None:
                return JobResult(
                    job_name=ctx.job_name,
                    run_id=ctx.run_id,
                    ok=False,
                    detail="memory_bundle unavailable",
                    duration_ms=(time.monotonic() - t0) * 1000.0,
                )

            store = getattr(bundle, "store", None)
            store_dir = getattr(store, "store_dir", None) if store is not None else None
            if store_dir is None:
                return JobResult(
                    job_name=ctx.job_name,
                    run_id=ctx.run_id,
                    ok=False,
                    detail="memory_bundle.store.store_dir unavailable",
                    duration_ms=(time.monotonic() - t0) * 1000.0,
                )

            mode = str(ctx.config.get("mode", "report")).strip().lower()
            if mode not in _VALID_MODES:
                return JobResult(
                    job_name=ctx.job_name,
                    run_id=ctx.run_id,
                    ok=False,
                    detail=f"invalid mode {mode!r}; expected one of {_VALID_MODES}",
                    duration_ms=(time.monotonic() - t0) * 1000.0,
                )

            report_before = MemoryLinter(
                store_dir=store_dir,
                project_root=TESSERACT_HOME,
                repo_root=ROOT,
            ).lint()

            scrubbable = (
                len(report_before.broken_frontmatter_links)
                + len(report_before.orphan_stubs)
            )

            if mode == "report":
                detail = (
                    f"mode=report scrubbable={scrubbable} "
                    f"fm_links={len(report_before.broken_frontmatter_links)} "
                    f"orphans={len(report_before.orphan_stubs)}"
                )
                return JobResult(
                    job_name=ctx.job_name,
                    run_id=ctx.run_id,
                    ok=True,
                    detail=detail,
                    payload={
                        "mode": "report",
                        "scrubbable": scrubbable,
                        "report": report_before.as_dict(),
                    },
                    duration_ms=(time.monotonic() - t0) * 1000.0,
                )

            # mode == "fix"
            fixed_fm, fixed_orphans = _scrub(store, report_before, store_dir)

            report_after = MemoryLinter(
                store_dir=store_dir,
                project_root=TESSERACT_HOME,
                repo_root=ROOT,
            ).lint()

            detail = (
                f"mode=fix fixed_fm={fixed_fm} fixed_orphans={fixed_orphans} "
                f"residual={report_after.total}"
            )
            return JobResult(
                job_name=ctx.job_name,
                run_id=ctx.run_id,
                # Residual non-zero is expected (stale_source_paths /
                # missing_path are out of scope); fix-mode succeeds when
                # the scrubbable subset reaches zero.
                ok=(
                    len(report_after.broken_frontmatter_links) == 0
                    and len(report_after.orphan_stubs) == 0
                ),
                detail=detail,
                payload={
                    "mode": "fix",
                    "fixed_frontmatter_links": fixed_fm,
                    "fixed_orphan_stubs": fixed_orphans,
                    "report_after": report_after.as_dict(),
                },
                duration_ms=(time.monotonic() - t0) * 1000.0,
            )
        except Exception as exc:
            log.exception("memory_scrub job crashed")
            return JobResult(
                job_name=ctx.job_name,
                run_id=ctx.run_id,
                ok=False,
                detail=f"unhandled: {exc!r}",
                duration_ms=(time.monotonic() - t0) * 1000.0,
            )


def _resolve_bundle(ctx: JobContext):
    app = ctx.app
    if app is None or not hasattr(app, "get"):
        return None
    return app.get("memory_bundle")


def _scrub(store: MemoryStore, report, store_dir: Path) -> tuple[int, int]:
    """Apply repairs to broken frontmatter links + zero-byte stubs.

    Returns ``(fixed_frontmatter_links, fixed_orphan_stubs)``.
    """
    # 1. Frontmatter sweep — group findings by host file so each entry is
    #    rewritten once even when several missing ids share it.
    by_path: dict[str, list[tuple[str, str]]] = {}
    for f in report.broken_frontmatter_links:
        by_path.setdefault(f.path, []).append((f.kind, f.detail))

    fixed_fm = 0
    for rel, removals in by_path.items():
        host = store_dir / rel
        if not host.is_file():
            continue
        try:
            text = host.read_text(encoding="utf-8")
        except OSError:
            continue
        mem_id = host.stem
        read_result = store.read(mem_id, log_access=False)
        if read_result is None:
            continue
        fm, body = read_result

        new_auto_links = list(fm.auto_links or [])
        new_links = list(fm.links or [])
        changed = False
        for kind, ref in removals:
            if kind == "auto_links" and ref in new_auto_links:
                new_auto_links = [x for x in new_auto_links if x != ref]
                changed = True
            elif kind == "links" and ref in new_links:
                new_links = [x for x in new_links if x != ref]
                changed = True
        if not changed:
            continue

        items: list[RelatedItem] = []
        for lid in new_auto_links:
            nbr = store.read(lid, log_access=False)
            title = nbr[0].title if nbr is not None else ""
            items.append((lid, title))
        updated_fm = fm.model_copy(
            update={"auto_links": new_auto_links, "links": new_links}
        )
        new_body = replace_related_block(body, items)
        # Trusted-internal write: scrub must repair frontmatter even when
        # the body would otherwise trip WhatNotToSave (same rationale as
        # MemoryStore.delete's cascade — see `skip_wnts_check` docstring).
        if store.write(updated_fm, new_body, skip_wnts_check=True):
            fixed_fm += 1
            store.log_event("writes.jsonl", {
                "memory_id": mem_id,
                "status": "scrubbed",
                "removed_refs": [r for _, r in removals],
            })

    # 2. Zero-byte stub sweep — unlink only files the linter actually
    #    flagged so we never delete a non-empty file due to a race.
    fixed_orphans = 0
    for f in report.orphan_stubs:
        if f.kind != "zero_byte":
            continue
        target = store_dir / f.path
        if not target.is_file():
            continue
        try:
            if target.stat().st_size != 0:
                continue
            target.unlink()
        except OSError:
            continue
        fixed_orphans += 1
        store.log_event("writes.jsonl", {
            "memory_id": target.stem,
            "status": "orphan_unlinked",
            "path": f.path,
        })

    return fixed_fm, fixed_orphans

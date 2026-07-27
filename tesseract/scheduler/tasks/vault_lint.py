"""VaultLintJob — scheduled five-pass vault wiki lint.

Reuses the already-wired `VaultLintTool` from `app["tool_registry"]` so
the job runs with the same agent + config the REPL/Mirror operator sees.
Breakers are NOT shared: `VaultLibrarian` owns `name="vault_librarian"`
(ingest + synthesis paths), while `VaultLinter` (`tesseract/memory/vault_lint.py`)
owns a separate `name="vault_lint"` breaker. A lint failure does not
trip the librarian; an ingest failure does not trip the linter — audit-1
m2 (2026-04-24) corrected the earlier comment that claimed shared state.

When `tool_registry` or the vault_lint tool entry is unavailable (e.g.
boot race), the job reports ok=False with a diagnostic detail instead of
crashing the scheduler tick.
"""

from __future__ import annotations

import logging
import time

from tesseract.kernel.tools.base import ToolContext
from tesseract.kernel.tools.vault_lint import VaultLintInput
from tesseract.scheduler.base_job import BaseJob
from tesseract.scheduler.types import JobContext, JobResult

log = logging.getLogger(__name__)


class VaultLintJob(BaseJob):
    async def run(self, ctx: JobContext) -> JobResult:
        t0 = time.monotonic()
        try:
            tool = _resolve_lint_tool(ctx)
            if tool is None:
                return JobResult(
                    job_name=ctx.job_name,
                    run_id=ctx.run_id,
                    ok=False,
                    detail="tool_registry or vault_lint tool unavailable",
                    duration_ms=(time.monotonic() - t0) * 1000.0,
                )

            tool_ctx = ToolContext(session_id=ctx.run_id, current_call_id=ctx.run_id)
            tool_result = await tool.run(VaultLintInput(dry_run=False), tool_ctx)
            metadata = tool_result.metadata or {}
            if tool_result.is_error or "lint_report" not in metadata:
                return JobResult(
                    job_name=ctx.job_name,
                    run_id=ctx.run_id,
                    ok=False,
                    detail=(
                        f"vault_lint tool returned no lint_report "
                        f"(is_error={tool_result.is_error}): {tool_result.output[:200]}"
                    ),
                    duration_ms=(time.monotonic() - t0) * 1000.0,
                )
            report = metadata["lint_report"]

            failures = report.get("failures") or []
            detail = (
                f"orphans={len(report.get('orphans', []))} "
                f"stale={len(report.get('stale', []))} "
                f"contradictions={len(report.get('contradictions', []))} "
                f"missing_hubs={len(report.get('missing_hubs', []))} "
                f"scale={'alarm' if report.get('scale_alarm') else 'ok'} "
                f"failures={len(failures)}"
            )
            return JobResult(
                job_name=ctx.job_name,
                run_id=ctx.run_id,
                ok=not failures,
                detail=detail,
                payload=dict(report),
                duration_ms=(time.monotonic() - t0) * 1000.0,
            )
        except Exception as exc:  # noqa: BLE001 — handler contract forbids raising
            log.exception("vault_lint job crashed")
            return JobResult(
                job_name=ctx.job_name,
                run_id=ctx.run_id,
                ok=False,
                detail=f"unhandled: {exc!r}",
                duration_ms=(time.monotonic() - t0) * 1000.0,
            )


def _resolve_lint_tool(ctx: JobContext):
    app = ctx.app
    if app is None or not hasattr(app, "get"):
        return None
    registry = app.get("tool_registry")
    if registry is None:
        return None
    return registry.tools.get("vault_lint")

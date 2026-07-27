"""RepoUpgradeResearchJob — P7 Task 2 — Codex read-only repo-upgrade research.

Each run, for every configured target ``{path, focus}``:

  1. Run a READ-ONLY Codex research pass (headless ``codex exec`` subprocess
     — mirrors ``delegate_codex_exec``'s headless fallback shape; scheduled
     jobs do not go through the kernel Tool layer) asking Codex to report
     outdated dependencies, upstream releases, and applicable improvements —
     including, when the target is one of TARS's own agent-card/rule dirs,
     concrete self-improvement proposals.
  2. Distill the output into one agenda proposal payload and publish it via
     ``publish_to_bus(AgendaSource.REPO_UPGRADE, ...)`` with an id stable per
     target+day, so a same-day re-run does not mint a second item.

Nothing here writes files or applies anything — Codex is instructed
explicitly to stay read-only, and the published proposal is operator/
vetter-reviewed only (``REPO_UPGRADE`` sits in
``agenda.yaml::vetter.vet_required`` so every draft mints ``UNVETTED``
first). All config lives in ``schedule.yaml::jobs[].config``. The handler
raises nothing — the scheduler contract forbids it.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from datetime import datetime
from typing import Any

from tesseract.kernel.adapters.cli_utils import (
    codex_subscription_env,
    resolve_codex_executable,
)
from tesseract.orchestrator.autonomy.models import AgendaSource
from tesseract.orchestrator.autonomy.publishers import publish_to_bus
from tesseract.paths import ROOT
from tesseract.scheduler.base_job import BaseJob
from tesseract.scheduler.types import JobContext, JobResult

log = logging.getLogger(__name__)

_MAX_FINDINGS_CHARS = 4000


class RepoUpgradeResearchJob(BaseJob):
    uses_llm = False

    async def run(self, ctx: JobContext) -> JobResult:
        t0 = time.monotonic()
        try:
            cfg = dict(ctx.config or {})
            targets = _normalize_targets(cfg.get("targets"))
            if not targets:
                return _result(ctx, t0, ok=False, detail="missing config: targets")
            timeout_s = cfg.get("timeout_s")
            if timeout_s is None:
                return _result(ctx, t0, ok=False, detail="missing config: timeout_s")
            timeout_s = float(timeout_s)

            runner = _make_codex_runner()
            results = await asyncio.gather(
                *(runner(_build_prompt(t), timeout_s) for t in targets),
                return_exceptions=True,
            )

            published = 0
            failures: list[str] = []
            for target, res in zip(targets, results):
                if isinstance(res, BaseException):
                    log.warning(
                        "repo_upgrade_research: codex failed for %s (%s)",
                        target["path"], res,
                    )
                    failures.append(f"{target['path']}: {res}")
                    continue
                findings = str(res).strip()
                if not findings:
                    failures.append(f"{target['path']}: empty codex output")
                    continue
                _publish(target, findings, when=ctx.fired_at)
                published += 1

            if published == 0:
                return _result(
                    ctx, t0, ok=False,
                    detail=f"all {len(targets)} target(s) failed: {'; '.join(failures)}",
                )
            detail = f"published {published}/{len(targets)} target(s)"
            if failures:
                detail += f"; failed: {'; '.join(failures)}"
            return JobResult(
                job_name=ctx.job_name,
                run_id=ctx.run_id,
                ok=True,
                detail=detail,
                payload={"published": published, "failed": len(failures)},
                duration_ms=(time.monotonic() - t0) * 1000.0,
            )
        except Exception as exc:  # noqa: BLE001 — handler contract forbids raising
            log.exception("repo_upgrade_research crashed")
            return _result(ctx, t0, ok=False, detail=f"unhandled: {exc!r}")


def _normalize_targets(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "").strip()
        if not path:
            continue
        out.append({"path": path, "focus": str(item.get("focus") or "").strip()})
    return out


def _build_prompt(target: dict[str, str]) -> str:
    lines = [
        "You are Codex performing a READ-ONLY research pass. Do not write, "
        "edit, delete, or run any command that mutates files, git state, or "
        "the environment — read-only inspection and reporting only.",
        "",
        f"TARGET: {target['path']}",
    ]
    if target["focus"]:
        lines.append(f"FOCUS: {target['focus']}")
    lines.extend([
        "",
        "Begin your final answer with exactly one line, formatted as:",
        "PROPOSAL: <one-line summary of the single most important finding>",
        "Then give full details below that line. Do not restate these "
        "instructions or acknowledge them before the PROPOSAL line.",
        "",
        "Report, concisely:",
        "- outdated dependencies and available upstream releases",
        "- applicable improvements grounded in what you find",
        "- if the target is one of TARS's own agent cards or rule files, "
        "propose concrete updates to those cards/rules (name the file and "
        "the change)",
        "- a proposal MAY take the shape of a draft skill "
        "(workspace/skills/<name>/SKILL.md plus optional scripts), described "
        "as PROPOSED TEXT ONLY — do not create any file yourself",
        "",
        "Do not apply any change. Output your findings as plain text for a "
        "human to review.",
        "",
        "REMINDER: your response MUST start with a 'PROPOSAL: <summary>' "
        "line before anything else — no acknowledgement, no restating that "
        "you will stay read-only, no preamble of any kind.",
    ])
    return "\n".join(lines)


def _make_codex_runner():
    """Return an async ``run(prompt, timeout_s) -> str`` headless `codex
    exec` call. Mirrors ``delegate_codex_exec``'s subprocess-exec fallback
    (no ``ToolContext`` in a cron job) — raises on any failure so the
    caller's ``asyncio.gather(..., return_exceptions=True)`` can isolate a
    dead target. ``cwd`` is pinned to the repo root so a target path given
    relative to it (e.g. `tesseract/scheduler`) resolves the same
    regardless of the scheduler daemon's own working directory. Injectable
    for tests, like `daily_job_search`'s fetcher."""

    async def _run(prompt: str, timeout_s: float) -> str:
        executable = resolve_codex_executable()
        env = codex_subscription_env()
        try:
            proc = await asyncio.create_subprocess_exec(
                executable, "exec", prompt,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=str(ROOT),
            )
        except (FileNotFoundError, OSError) as exc:
            raise RuntimeError(f"failed to spawn codex exec: {exc}") from exc

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_s,
            )
        except asyncio.TimeoutError:
            proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass
            raise

        if proc.returncode != 0:
            stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()
            stdout_text = stdout_bytes.decode("utf-8", errors="replace").strip()
            tail = (stderr_text or stdout_text)[-2000:]
            raise RuntimeError(f"codex exec returned {proc.returncode}: {tail}")

        return stdout_bytes.decode("utf-8", errors="replace")

    return _run


_MAX_SUMMARY_CHARS = 200

_PROPOSAL_MARKER_RE = re.compile(r"(?im)^\s*PROPOSAL:\s*(.+)$")

# Obedience-preamble / instruction-echo lines Codex sometimes opens with
# instead of (or in addition to) the requested PROPOSAL marker — the P7
# live-gate bug that let one of these become the agenda item's goal.
_PREAMBLE_LINE_RE = re.compile(
    r"^(understood|ok|okay|sure|got it|noted|acknowledged|"
    r"i'll|i will|i'm going to|i am going to|as (requested|instructed))\b",
    re.IGNORECASE,
)

# Broader semantic guard: a line that merely CONTAINS an acknowledgement /
# consent / instruction-restatement phrase — not necessarily at its start —
# is still preamble, not a finding. This is what the REOPENED live-gate bug
# needed: Codex's actual response was a single line ("Understood. I'll
# perform only read-only inspection and report findings, with no writes,
# edits, deletes, generated files, environment changes, or git operations.")
# with no substantive content after it at all.
_PREAMBLE_MENTION_RE = re.compile(
    r"read-only inspection|read.only research pass|stay read.only|"
    r"no (writes|edits|deletes)|will (not|only) (write|edit|delete|modify|mutate|perform)",
    re.IGNORECASE,
)


def _is_preamble_line(line: str) -> bool:
    return bool(_PREAMBLE_LINE_RE.match(line) or _PREAMBLE_MENTION_RE.search(line))


def _distill_summary(findings: str, path: str) -> str:
    """Extract a one-line proposal summary out of raw Codex output.

    Prefers an explicit ``PROPOSAL:`` marker line (exact lift). Falling
    back, skips leading obedience-preamble / instruction-echo lines and
    uses the first substantive line — never the preamble itself. When
    nothing substantive survives that filter (e.g. Codex's entire response
    was one acknowledgement sentence — the live P7 gate bug), a
    deterministic fallback title is used instead of echoing the preamble.
    """
    marker = _PROPOSAL_MARKER_RE.search(findings)
    if marker:
        return marker.group(1).strip()[:_MAX_SUMMARY_CHARS]
    for line in findings.splitlines():
        line = line.strip()
        if not line or _is_preamble_line(line):
            continue
        return line[:_MAX_SUMMARY_CHARS]
    return f"repo-upgrade findings for {path} (unstructured output)"


def _publish(target: dict[str, str], findings: str, *, when: datetime) -> None:
    payload = {
        "path": target["path"],
        "focus": target["focus"],
        "findings": findings[:_MAX_FINDINGS_CHARS],
        "summary": _distill_summary(findings, target["path"]),
        "emitted_at": when.isoformat(),
        "source_handler": "repo_upgrade_research",
    }
    publish_to_bus(
        AgendaSource.REPO_UPGRADE, payload, event_id=_event_id(target["path"], when),
    )


def _event_id(path: str, when: datetime) -> str:
    """Stable per target+day — a same-day re-run of the same target does
    not mint a second bus event (kernel admission dedupes on it)."""
    digest = hashlib.sha1(path.encode("utf-8")).hexdigest()[:16]
    return f"evt_repo_upgrade_{digest}_{when.date().isoformat()}"


def _result(ctx: JobContext, t0: float, *, ok: bool, detail: str) -> JobResult:
    return JobResult(
        job_name=ctx.job_name,
        run_id=ctx.run_id,
        ok=ok,
        detail=detail,
        payload={},
        duration_ms=(time.monotonic() - t0) * 1000.0,
    )


__all__ = ["RepoUpgradeResearchJob"]

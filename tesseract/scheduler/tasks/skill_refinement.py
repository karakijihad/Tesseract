"""Which skills are letting the assistant down, and a rewrite of the worst.

Scans `logs/skills/usage.jsonl`. A skill whose
negative-outcome ratio (``error`` + ``correction`` over total loads) crosses a
configured threshold within the window is flagged: the job files a
``skill_refinement`` inbox card. When a model role resolves, the job also asks
it to propose a revised SKILL.md body so the card carries an applyable diff
(approve → the route overwrites the live skill); when no role is available the
card is flag-only (operator refines manually).

Detection needs no model — it is pure arithmetic over the usage log — so the
card always fires for a genuinely underperforming skill. The LLM proposal is
best-effort enrichment on top.

**Fired on volume, not on a clock.** Its row declares
``when: skill_usage_volume`` — it runs once enough new skill uses have been
logged to judge one, which is the question a cadence cannot answer: a weekly
cron reads two data points as readily as two hundred. The threshold is
``when_config.min_new_rows``.

Never raises — handler contract returns ``JobResult(ok=False, ...)`` on failure.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from tesseract.brain.skill_usage import read_usage
from tesseract.brain.skills import SKILL_FILENAME, list_skills_names
from tesseract.orchestrator.outcome import RunOutcome
from tesseract.paths import TESSERACT_HOME, home_logs_root
from tesseract.scheduler.base_job import BaseJob
from tesseract.scheduler.role_chain import build_chain_for_job
from tesseract.scheduler.types import JobContext, JobResult

log = logging.getLogger(__name__)

_NEGATIVE_OUTCOMES = frozenset({"error", "correction"})
_DEFAULT_TIMEOUT_S = 60.0

_PROMPT = (
    "You are refining an assistant skill (a markdown playbook) that has been "
    "underperforming — it was consulted but the work that followed failed or "
    "was corrected more often than it should. Read the current SKILL.md below "
    "and propose a REVISED, complete SKILL.md that fixes the likely cause "
    "(unclear steps, stale instructions, missing guardrails). Keep the YAML "
    "frontmatter's `name` identical. Return ONLY the full revised SKILL.md "
    "content (frontmatter + body), no preamble. If the skill looks fine and "
    "you cannot improve it, return exactly the single token NO_CHANGE."
)


class SkillRefinementJob(BaseJob):
    uses_llm = True
    default_model_role = "subagents_default"

    async def run(self, ctx: JobContext) -> JobResult:
        t0 = time.monotonic()
        try:
            cfg = ctx.config or {}
            window_days = int(cfg.get("window_days", 7))
            min_loads = int(cfg.get("min_loads", 3))
            ratio_threshold = float(cfg.get("ratio_threshold", 0.34))
            max_cards = int(cfg.get("max_cards", 3))

            # Off the loop: usage.jsonl accumulates one row per skill load
            # for the life of the install and is read whole here.
            usage_rows = await asyncio.to_thread(read_usage)
            rows = _rows_in_window(usage_rows, ctx.fired_at, window_days)
            stats = _aggregate(rows)
            skills_dir = _resolve_skills_dir(ctx)
            active = set(list_skills_names(skills_dir))

            candidates = [
                s for s in _rank_candidates(stats, min_loads, ratio_threshold)
                if s["skill"] in active  # only flag skills that still exist
            ]

            store = _resolve_store(ctx)
            already = _pending_refinement_skills(store)
            filed = 0
            flag_only = 0
            for cand in candidates:
                if filed >= max_cards:
                    break
                if cand["skill"] in already:
                    continue
                proposed = await self._file_card(ctx, store, skills_dir, cand)
                if proposed is None:
                    continue
                filed += 1
                flag_only += 0 if proposed else 1

            return JobResult(
                job_name=ctx.job_name,
                run_id=ctx.run_id,
                ok=True,
                detail=f"candidates={len(candidates)} filed={filed}",
                outcome=_outcome(candidates, filed, flag_only),
                outcome_reason=_reason(candidates, filed, flag_only, ratio_threshold),
                payload={
                    "candidates": [c["skill"] for c in candidates],
                    "filed": filed,
                    "window_days": window_days,
                },
                duration_ms=(time.monotonic() - t0) * 1000.0,
            )
        except Exception as exc:  # noqa: BLE001 — handler contract forbids raising
            log.exception("skill_refinement crashed")
            return JobResult(
                job_name=ctx.job_name,
                run_id=ctx.run_id,
                ok=False,
                detail=f"unhandled: {exc!r}",
                duration_ms=(time.monotonic() - t0) * 1000.0,
            )

    async def _file_card(
        self,
        ctx: JobContext,
        store: Any,
        skills_dir: Path,
        cand: dict[str, Any],
    ) -> bool | None:
        """File one skill_refinement card.

        `None` if it could not be filed; otherwise whether the card carries a
        proposed rewrite. The caller needs the difference: a card that is only
        a flag is the job's degraded path, not its output.
        """
        from tesseract.workspace_events import WorkspaceEvent

        name = cand["skill"]
        current = _read_skill_md(skills_dir, name)
        proposed = await self._propose_revision(ctx, current)
        ratio_pct = round(cand["neg"] / cand["total"] * 100)
        summary = (
            f"{name} — {cand['neg']}/{cand['total']} loads ({ratio_pct}%) ended "
            "in error/correction. "
            + ("A revised SKILL.md is proposed below." if proposed
               else "Review and refine it manually.")
        )
        event = WorkspaceEvent.new(
            kind="skill_refinement",
            source="agent",
            title=f"Skill needs refinement: {name}",
            summary=summary,
            payload={
                "name": name,
                "stats": {"total": cand["total"], "negative": cand["neg"]},
                "current_markdown": current,
                "proposed_markdown": proposed,
            },
        )
        try:
            store.append_event(event)
        except Exception:
            log.exception("skill_refinement: append card failed for %s", name)
            return None
        await _broadcast(ctx, event)
        return bool(proposed)

    async def _propose_revision(self, ctx: JobContext, current: str) -> str:
        """Best-effort LLM proposal of a revised SKILL.md. Empty on any miss
        (no role, timeout, NO_CHANGE) — the card then stays flag-only."""
        if not current.strip():
            return ""
        try:
            chain = build_chain_for_job(
                ctx,
                default_role=SkillRefinementJob.default_model_role,
                log_label="skill_refinement",
            )
        except Exception:
            log.warning("skill_refinement: role chain build failed", exc_info=True)
            return ""
        if not chain:
            return ""
        prompt = f"{_PROMPT}\n\n--- CURRENT SKILL.md ---\n{current}\n--- END ---\n"
        for adapter, options in chain:
            try:
                out = await asyncio.wait_for(
                    adapter.generate(prompt, options), timeout=_DEFAULT_TIMEOUT_S,
                )
            except Exception:  # noqa: BLE001
                continue
            text = (out or "").strip()
            if text and text != "NO_CHANGE" and text.startswith("---"):
                return text
        return ""


# ─── Helpers ─────────────────────────────────────────────


def _outcome(candidates: list[dict[str, Any]], filed: int, flag_only: int) -> RunOutcome:
    """Which of the closed states this run was.

    Nothing crossing the threshold is the healthy common case and says so.
    A card filed with no proposed rewrite is DEGRADED rather than succeeded:
    the job's contract is an applyable diff, and a flag the operator has to
    act on by hand is less than that — arriving quietly as a success is how a
    dead model role goes unnoticed for a month.
    """
    if not candidates or filed == 0:
        return RunOutcome.SKIPPED_NO_WORK
    return RunOutcome.DEGRADED if flag_only else RunOutcome.SUCCEEDED


def _reason(
    candidates: list[dict[str, Any]], filed: int, flag_only: int, threshold: float,
) -> str:
    if not candidates:
        return f"no skill failed more than {round(threshold * 100)}% of its loads"
    if filed == 0:
        return "every underperforming skill already has a card waiting"
    if flag_only:
        return (
            f"{flag_only} of {filed} card(s) carry no proposed rewrite — the model "
            "was unavailable, so they are flags to refine by hand"
        )
    return ""


def _rows_in_window(rows: list[dict[str, Any]], now: datetime, window_days: int) -> list[dict[str, Any]]:
    cutoff = now - timedelta(days=window_days)
    kept: list[dict[str, Any]] = []
    for r in rows:
        ts = r.get("ts")
        if not isinstance(ts, str):
            continue
        try:
            when = datetime.fromisoformat(ts)
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if when >= cutoff:
            kept.append(r)
    return kept


def _aggregate(rows: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """Per-skill {total, neg} counts."""
    out: dict[str, dict[str, int]] = {}
    for r in rows:
        skill = r.get("skill")
        if not skill:
            continue
        bucket = out.setdefault(skill, {"total": 0, "neg": 0})
        bucket["total"] += 1
        if r.get("outcome") in _NEGATIVE_OUTCOMES:
            bucket["neg"] += 1
    return out


def _rank_candidates(
    stats: dict[str, dict[str, int]], min_loads: int, ratio_threshold: float,
) -> list[dict[str, Any]]:
    cands = [
        {"skill": skill, "total": b["total"], "neg": b["neg"],
         "ratio": b["neg"] / b["total"] if b["total"] else 0.0}
        for skill, b in stats.items()
        if b["total"] >= min_loads and (b["neg"] / b["total"]) >= ratio_threshold
    ]
    cands.sort(key=lambda c: (-c["ratio"], -c["neg"], c["skill"]))
    return cands


def _resolve_skills_dir(ctx: JobContext) -> Path:
    override = (ctx.config or {}).get("skills_dir")
    if override:
        return Path(override)
    from tesseract.paths import workspace_dir
    return workspace_dir() / "skills"


def _resolve_logs_dir(ctx: JobContext) -> Path:
    override = (ctx.config or {}).get("logs_dir")
    if override:
        return Path(override)
    env = os.environ.get("TESSERACT_HOME")
    home = Path(env).resolve() if env else TESSERACT_HOME
    return home_logs_root()


def _resolve_store(ctx: JobContext) -> Any:
    from tesseract.workspace_events import EventStore

    return EventStore(_resolve_logs_dir(ctx))


def _pending_refinement_skills(store: Any) -> set[str]:
    try:
        return {
            (ev.payload or {}).get("name")
            for ev in store.list_events(kinds=("skill_refinement",), status="pending")
        } - {None}
    except Exception:
        return set()


def _read_skill_md(skills_dir: Path, name: str) -> str:
    try:
        return (skills_dir / name / SKILL_FILENAME).read_text(encoding="utf-8")
    except OSError:
        return ""


async def _broadcast(ctx: JobContext, event: Any) -> None:
    try:
        from tesseract.workspace_events.broadcast import broadcast_workspace_event

        if ctx.app is not None:
            await broadcast_workspace_event(ctx.app, event)
    except Exception:
        log.warning("skill_refinement: broadcast failed", exc_info=True)

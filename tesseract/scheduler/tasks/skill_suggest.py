"""SkillSuggestJob — Phase 4 (capability-growth) 4c, suggest-only.

The judgment path for drafting skills is the `skill_create` tool (TARS decides,
like `agent_create`). This job is the *optional* detector layer chosen by the
operator: it never drafts. It reads the recent daily digests
(`memory-store/daily/*.md`, where chat_digest lands session summaries), lists
the skills that already exist, and asks a model whether any repeated task shape
appears with NO covering skill. Each suggestion becomes a `nudge` card — the
operator (or TARS on its next turn) decides whether to `skill_create` it.

No auto-draft, ever. Disabled by default in ``schedule.yaml``. Never raises —
handler contract returns ``JobResult(ok=False, ...)`` on failure.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import timedelta, timezone
from pathlib import Path
from typing import Any

from tesseract.brain.skills import load_skills
from tesseract.paths import TESSERACT_HOME
from tesseract.scheduler.base_job import BaseJob
from tesseract.scheduler.role_chain import build_chain_for_job
from tesseract.scheduler.types import JobContext, JobResult

log = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_S = 60.0
_DIGEST_CHAR_CAP = 8000

_PROMPT = (
    "You review a TARS operator's recent work digests to spot REPEATED task "
    "shapes that have NO covering skill yet — a chore done the same way "
    "multiple times that a short markdown playbook (skill) would streamline. "
    "You are given the recent digests and the list of existing skills. "
    "Return ONLY a JSON array (possibly empty), each item "
    '{"name": "<slug-suggestion>", "why": "<one line: the repeated shape and '
    'why a skill helps>"}. Suggest a task shape ONLY if it recurs and is not '
    "already covered by an existing skill. Never suggest more than a couple. "
    "If nothing qualifies, return []."
)


class SkillSuggestJob(BaseJob):
    uses_llm = True
    default_model_role = "subagents_default"

    async def run(self, ctx: JobContext) -> JobResult:
        t0 = time.monotonic()
        try:
            cfg = ctx.config or {}
            lookback_days = int(cfg.get("lookback_days", 7))
            max_suggestions = int(cfg.get("max_suggestions", 2))

            digests = _recent_digests(ctx, lookback_days)
            if not digests.strip():
                return _ok(ctx, t0, 0, "no recent digests")

            skills_dir = _resolve_skills_dir(ctx)
            existing = load_skills(skills_dir)
            existing_names = {s.name for s in existing}

            chain = build_chain_for_job(
                ctx,
                default_role=SkillSuggestJob.default_model_role,
                log_label="skill_suggest",
            )
            if not chain:
                return _ok(ctx, t0, 0, "role unavailable — skipped")

            raw = await _call(chain, _build_prompt(digests, existing))
            suggestions = _parse(raw, existing_names)[:max_suggestions]
            if not suggestions:
                return _ok(ctx, t0, 0, "no uncovered shapes found")

            store = _resolve_store(ctx)
            already = _pending_suggested_names(store)
            filed = 0
            for sug in suggestions:
                if sug["name"] in already or sug["name"] in existing_names:
                    continue
                if await _file_nudge(ctx, store, sug):
                    filed += 1

            return _ok(ctx, t0, filed, f"suggested={len(suggestions)} filed={filed}")
        except Exception as exc:  # noqa: BLE001 — handler contract forbids raising
            log.exception("skill_suggest crashed")
            return JobResult(
                job_name=ctx.job_name,
                run_id=ctx.run_id,
                ok=False,
                detail=f"unhandled: {exc!r}",
                duration_ms=(time.monotonic() - t0) * 1000.0,
            )


def _ok(ctx: JobContext, t0: float, filed: int, detail: str) -> JobResult:
    return JobResult(
        job_name=ctx.job_name,
        run_id=ctx.run_id,
        ok=True,
        detail=detail,
        payload={"filed": filed},
        duration_ms=(time.monotonic() - t0) * 1000.0,
    )


def _build_prompt(digests: str, existing: list[Any]) -> str:
    skill_lines = "\n".join(f"- {s.name}: {s.description}" for s in existing) or "(none)"
    return (
        f"{_PROMPT}\n\n--- EXISTING SKILLS ---\n{skill_lines}\n\n"
        f"--- RECENT DIGESTS ---\n{digests}\n--- END ---\n"
    )


async def _call(chain: list[tuple[Any, Any]], prompt: str) -> str:
    for adapter, options in chain:
        try:
            out = await asyncio.wait_for(adapter.generate(prompt, options), timeout=_DEFAULT_TIMEOUT_S)
        except Exception:  # noqa: BLE001
            continue
        if out and out.strip():
            return out
    return ""


def _parse(raw: str, existing_names: set[str]) -> list[dict[str, str]]:
    """Extract the JSON array of suggestions. Tolerant of prose wrapping."""
    if not raw:
        return []
    start, end = raw.find("["), raw.rfind("]")
    if start == -1 or end <= start:
        return []
    try:
        data = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in data:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip().lower().replace(" ", "-")
        why = str(item.get("why") or "").strip()
        if not name or not why or name in seen or name in existing_names:
            continue
        seen.add(name)
        out.append({"name": name, "why": why})
    return out


async def _file_nudge(ctx: JobContext, store: Any, sug: dict[str, str]) -> bool:
    from tesseract.workspace_events import WorkspaceEvent

    event = WorkspaceEvent.new(
        kind="nudge",
        source="tars",
        title=f"Consider a skill: {sug['name']}",
        summary=sug["why"],
        payload={"suggested_skill": sug["name"], "why": sug["why"], "origin": "skill_suggest"},
    )
    try:
        store.append_event(event)
    except Exception:
        log.exception("skill_suggest: append nudge failed for %s", sug["name"])
        return False
    try:
        from tesseract.workspace_events.broadcast import broadcast_workspace_event

        if ctx.app is not None:
            await broadcast_workspace_event(ctx.app, event)
    except Exception:
        log.warning("skill_suggest: broadcast failed", exc_info=True)
    return True


def _pending_suggested_names(store: Any) -> set[str]:
    try:
        return {
            (ev.payload or {}).get("suggested_skill")
            for ev in store.list_events(kinds=("nudge",), status="pending")
            if (ev.payload or {}).get("origin") == "skill_suggest"
        } - {None}
    except Exception:
        return set()


def _recent_digests(ctx: JobContext, lookback_days: int) -> str:
    """Concatenate recent daily digest files, newest first, capped."""
    daily_dir = _resolve_daily_dir(ctx)
    if not daily_dir.exists():
        return ""
    cutoff = (ctx.fired_at - timedelta(days=lookback_days)).date()
    parts: list[str] = []
    total = 0
    try:
        files = sorted(daily_dir.glob("*.md"), reverse=True)
    except OSError:
        return ""
    for f in files:
        try:
            day = f.stem  # YYYY-MM-DD
            from datetime import date

            if date.fromisoformat(day) < cutoff:
                continue
        except (ValueError, OSError):
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except OSError:
            continue
        if total + len(text) > _DIGEST_CHAR_CAP:
            text = text[: max(0, _DIGEST_CHAR_CAP - total)]
        parts.append(f"# {f.stem}\n{text}")
        total += len(text)
        if total >= _DIGEST_CHAR_CAP:
            break
    return "\n\n".join(parts)


def _resolve_daily_dir(ctx: JobContext) -> Path:
    override = (ctx.config or {}).get("daily_dir")
    if override:
        return Path(override)
    env = os.environ.get("TESSERACT_HOME")
    home = Path(env).resolve() if env else TESSERACT_HOME
    return home / "memory-store" / "daily"


def _resolve_skills_dir(ctx: JobContext) -> Path:
    override = (ctx.config or {}).get("skills_dir")
    if override:
        return Path(override)
    from tesseract.paths import workspace_dir
    return workspace_dir() / "skills"


def _resolve_store(ctx: JobContext) -> Any:
    from tesseract.workspace_events import EventStore

    override = (ctx.config or {}).get("logs_dir")
    if override:
        return EventStore(Path(override))
    env = os.environ.get("TESSERACT_HOME")
    home = Path(env).resolve() if env else TESSERACT_HOME
    return EventStore(home / "logs")

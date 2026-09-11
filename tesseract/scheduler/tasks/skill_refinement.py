"""Which skills are letting the assistant down, and a rewrite of the worst.

Scans `logs/skills/usage.jsonl`. A skill whose corrections-per-load ratio on
the revision that is LIVE crosses a configured threshold within the window is
flagged: the job files a ``skill_refinement`` inbox card. When a model role
resolves, the job also asks it to propose a revised SKILL.md body so the card
carries an applyable diff (approve → the route overwrites the live skill);
when no role is available the card is flag-only (operator refines manually).

**What counts, and why it is not every row.** `_aggregate` carries the whole
argument and the measurements behind it. In short: a revision that has been
replaced is not evidence about the text a rewrite would edit; a ``correction``
is an extra row beside the load it is about, so counting it in the denominator
put one event on both sides of the ratio; and an ``error`` is a read that
FAILED, which is a fact about the file rather than about a revision, so it has
its own count and its own denominator and files a flag-only card of its own.

Detection needs no model — it is pure arithmetic over the usage log — so the
card always fires for a genuinely underperforming skill. The LLM proposal is
best-effort enrichment on top.

**A playbook revision that measures worse than the one before it is
retired.** Loads, the outcome of the turn each load was read in, corrections
and retries are counted per revision (`brain/playbook_reuse.py`), and a live
revision with more trouble per load than its predecessor over the window,
both with at least ``min_loads``, has its status set to ``retired``: it is not
offered again. The predecessor stays under ``<name>/history/<version>/`` and a
card says so. Returning to it is a person's act, on the card; nothing here
swaps one procedure for another unattended.

**Fired on a rate, not on a wall clock.** Its row declares ``cadence: 24h``.
It was ``when: skill_usage_volume`` at 40 new rows and never fired, because
the log held 27 rows in total and the count was over ALL playbooks while
``min_loads`` is a floor per skill. `schedule.yaml` carries that reasoning
beside the row. A pass that finds nothing costs file reads and no model call,
because the chain is built inside the candidate loop.

Never raises — handler contract returns ``JobResult(ok=False, ...)`` on failure.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from tesseract.brain.playbook_contract import version_number
from tesseract.brain.playbook_reuse import Reuse, measure, worse_than
from tesseract.brain.skill_usage import read_usage
from tesseract.brain.skills import (
    SKILL_FILENAME,
    SKIP_DIRNAMES,
    SkillEntry,
    list_history,
    load_skills,
    set_skill_status,
)
from tesseract.orchestrator.outcome import RunOutcome
from tesseract.paths import TESSERACT_HOME, home_logs_root
from tesseract.scheduler.base_job import BaseJob
from tesseract.scheduler.role_chain import build_chain_for_job
from tesseract.scheduler.types import JobContext, JobResult

log = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_S = 60.0

_PROMPT = (
    "You are refining an assistant skill (a markdown playbook). It was "
    "consulted and the work that followed was corrected afterwards more often "
    "than it should have been. What was measured is below: which step the "
    "turn had reached when it went wrong, and what the operator said where "
    "that was recorded. Address what the evidence shows, and where it quotes "
    "the operator, treat that as the strongest thing you have. It is still an "
    "observation and not a proof of cause: a step reached is how far "
    "execution got, not necessarily the step at fault, and the correction may "
    "have been about the assistant ignoring the playbook rather than the "
    "playbook being wrong. If the evidence does not support "
    "a change to the TEXT, return exactly the single token NO_CHANGE, which "
    "is a useful answer and not a failure. Otherwise propose a REVISED, "
    "complete SKILL.md. Keep the YAML frontmatter's `name` identical and "
    "leave `version` alone: the runtime numbers the revision. Return ONLY "
    "the full SKILL.md content (frontmatter + body), no preamble."
)


class SkillRefinementJob(BaseJob):
    uses_llm = True
    # A CHAIN, not a role. `agents_default` and `subagents_default` are seats
    # that also serve `invoke_agent`, so sharing one meant this job's model and
    # its spend moved whenever an agent was re-pointed. Naming the chain
    # directly severs that: what it spends bills to the entry, whose ceiling is
    # on its manifest entry.
    default_model_chain = "chain_1"

    async def run(self, ctx: JobContext) -> JobResult:
        t0 = time.monotonic()
        try:
            cfg = ctx.config or {}
            # Indexed, never `.get` with a default. Two of the four had already
            # drifted from `schedule.yaml` (7 against 14, 3 against 4), so a
            # missing key would have run this job on numbers nobody set and
            # said nothing. The handler contract still holds: a KeyError here
            # is caught below and returns `ok=False` naming the key, which is
            # louder than judging a playbook against the wrong floor.
            window_days = int(cfg["window_days"])
            min_loads = int(cfg["min_loads"])
            ratio_threshold = float(cfg["ratio_threshold"])
            max_cards = int(cfg["max_cards"])
            max_evidence_rows = int(cfg["max_evidence_rows"])
            max_correction_chars = int(cfg["max_correction_chars"])

            # Off the loop: usage.jsonl accumulates one row per skill load
            # for the life of the install and is read whole here.
            usage_rows = await asyncio.to_thread(read_usage)
            rows = _rows_in_window(usage_rows, ctx.fired_at, window_days)
            skills_dir = _resolve_skills_dir(ctx)
            # The revisions that are live NOW. Doubles as the active-skill
            # filter it replaces: a skill the tree no longer holds cannot be
            # refined, and one that is gone has no revision to count against.
            live_by_skill = await asyncio.to_thread(_live_revisions, skills_dir)
            stats = _aggregate(rows, live_by_skill)
            candidates = _rank_candidates(stats, min_loads, ratio_threshold)

            store = _resolve_store(ctx)
            already = _pending_refinement_skills(store)
            refused = _refused_bases(store)
            filed = 0
            flag_only = 0
            unreadable = 0
            refused_again = 0
            for cand in candidates:
                if filed >= max_cards:
                    break
                if cand["skill"] in already:
                    continue
                # Already said no to this exact text. Skipped rather than
                # re-asked, and counted so the run says why it did nothing.
                current = await asyncio.to_thread(
                    _read_skill_md, skills_dir, cand["skill"]
                )
                if (cand["skill"], _sha256(current)) in refused:
                    refused_again += 1
                    continue
                proposed = await self._file_card(
                    ctx, store, cand, current, window_days,
                    max_evidence_rows, max_correction_chars,
                )
                if proposed is None:
                    continue
                filed += 1
                flag_only += 0 if proposed else 1
                unreadable += 1 if cand["reason"] == "unreadable" else 0

            retired = await asyncio.to_thread(
                _retire_worse_revisions, skills_dir, window_days, min_loads, ctx.fired_at
            )
            for name, live, before in retired:
                await self._file_retirement(ctx, store, skills_dir, name, live, before)

            return JobResult(
                job_name=ctx.job_name,
                run_id=ctx.run_id,
                ok=True,
                detail=f"candidates={len(candidates)} filed={filed} retired={len(retired)}",
                outcome=_outcome(candidates, filed, flag_only, len(retired)),
                outcome_reason=_reason(
                    candidates, filed, flag_only, ratio_threshold, unreadable,
                    refused_again,
                ),
                payload={
                    "candidates": [c["skill"] for c in candidates],
                    "filed": filed,
                    "retired": [
                        {"name": name, "live": live.as_json(), "predecessor": before.as_json()}
                        for name, live, before in retired
                    ],
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
        cand: dict[str, Any],
        current: str,
        window_days: int,
        max_evidence_rows: int,
        max_correction_chars: int,
    ) -> bool | None:
        """File one skill_refinement card.

        `None` if it could not be filed; otherwise whether the card carries a
        proposed rewrite. The caller needs the difference: a card that is only
        a flag is the job's degraded path, not its output.
        """
        from tesseract.workspace_events import WorkspaceEvent

        name = cand["skill"]
        version = cand["version"]
        at = f" v{version}" if version else ""
        # Unreadable first: it spends no model call and wants no evidence, so
        # building the block above this branch walked the memory store for
        # something thrown away.
        if cand["reason"] == "unreadable":
            return await self._file_unreadable(
                ctx, store, name, at, cand, current,
            )
        # Off the loop: it walks the memory store once per shown failure,
        # and this job shares the backend's loop with WS heartbeats and
        # inbound turns. Every other file read here is threaded for that
        # reason and this one was the exception.
        evidence = await asyncio.to_thread(
            _evidence_block, cand, window_days, max_evidence_rows,
            max_correction_chars,
        )
        proposed = await self._propose_revision(ctx, current, evidence)
        ratio_pct = round(cand["neg"] / cand["total"] * 100)
        # What was NOT counted is said on the card, not just in the log. An
        # error is a read that failed and no rewrite of the prose fixes it;
        # an unattributable row named no revision. Both are reasons the
        # ratio is smaller than the operator's memory of the trouble.
        aside = []
        if cand["errors"]:
            aside.append(f"{cand['errors']} unreadable")
        if cand["unattributable"]:
            aside.append(f"{cand['unattributable']} naming no revision")
        tail = f" Not counted: {', '.join(aside)}." if aside else ""
        summary = (
            f"{name}{at}: {cand['neg']} of {cand['total']} loads ({ratio_pct}%) "
            "were corrected afterwards."
            + tail + " "
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
                # The revision this was measured against and the bytes it was
                # written against. `replace_skill_body` stamps the new number
                # from whatever is live when the card is APPROVED, so without
                # these a proposal written against v3 and approved after the
                # skill moved to v4 is silently rebased and reads as current.
                "version": version,
                "base_sha256": _sha256(current),
                "stats": {
                    "loads": cand["total"],
                    "corrections": cand["neg"],
                    "errors": cand["errors"],
                    "unattributable": cand["unattributable"],
                },
                "current_markdown": current,
                "proposed_markdown": proposed,
                # The same block the model was given. On the card so the
                # operator judges the proposal against what produced it,
                # rather than against the summary line.
                "evidence": evidence,
            },
        )
        try:
            store.append_event(event)
        except Exception:
            log.exception("skill_refinement: append card failed for %s", name)
            return None
        await _broadcast(ctx, event)
        return bool(proposed)

    async def _file_unreadable(
        self,
        ctx: JobContext,
        store: Any,
        name: str,
        at: str,
        cand: dict[str, Any],
        current: str,
    ) -> bool | None:
        """One card for a skill whose file keeps failing to read.

        Flag-only by construction and it costs no model call. The fault is
        that the SKILL.md is missing or will not parse, and a rewrite of its
        prose is not an answer to that: the card says what to go and look at.
        Returns the same `None` / `bool` the proposal path does, so the
        caller's tally does not learn a third case.
        """
        from tesseract.workspace_events import WorkspaceEvent

        ratio_pct = round(cand["errors"] / cand["reads"] * 100)
        event = WorkspaceEvent.new(
            kind="skill_refinement",
            source="agent",
            title=f"Skill will not read: {name}",
            summary=(
                f"{name}{at}: {cand['errors']} of {cand['reads']} reads "
                f"({ratio_pct}%) failed. The file is missing or does not "
                "parse, so nothing was proposed. Open "
                f"workspace/skills/{name}/SKILL.md and see what is wrong."
            ),
            payload={
                "name": name,
                "version": cand["version"],
                "base_sha256": _sha256(current),
                "stats": {
                    "loads": cand["total"],
                    "corrections": cand["neg"],
                    "errors": cand["errors"],
                    "unattributable": cand["unattributable"],
                },
                "current_markdown": current,
                "proposed_markdown": "",
            },
        )
        try:
            store.append_event(event)
        except Exception:
            log.exception("skill_refinement: append unreadable card failed for %s", name)
            return None
        await _broadcast(ctx, event)
        return False

    async def _file_retirement(
        self,
        ctx: JobContext,
        store: Any,
        skills_dir: Path,
        name: str,
        live: Reuse,
        before: Reuse,
    ) -> None:
        """One card per retirement: what was retired, against what, and where
        the predecessor is. Flag-only by construction, so the approve route
        has nothing to apply; the numbers are the point."""
        from tesseract.workspace_events import WorkspaceEvent

        summary = (
            f"{name} v{live.version} was retired: {live.failed} failed turns and "
            f"{live.corrections} corrections over {live.loads} loads, against "
            f"{before.failed} and {before.corrections} over {before.loads} for "
            f"v{before.version}. Version {before.version} is kept under "
            f"history/{before.version}/ to return to."
        )
        event = WorkspaceEvent.new(
            kind="skill_refinement",
            source="agent",
            title=f"Playbook revision retired: {name} v{live.version}",
            summary=summary,
            payload={
                "name": name,
                "retired_version": live.version,
                "predecessor_version": before.version,
                "reuse": {"live": live.as_json(), "predecessor": before.as_json()},
                "current_markdown": _read_skill_md(skills_dir, name),
                "proposed_markdown": "",
            },
        )
        try:
            store.append_event(event)
        except Exception:
            log.exception("skill_refinement: append retirement card failed for %s", name)
            return
        await _broadcast(ctx, event)

    async def _propose_revision(
        self, ctx: JobContext, current: str, evidence: str = "",
    ) -> str:
        """Best-effort LLM proposal of a revised SKILL.md. Empty on any miss
        (no role, timeout, NO_CHANGE) — the card then stays flag-only."""
        if not current.strip():
            return ""
        try:
            chain = build_chain_for_job(
                ctx,
                default_role=None,
                default_chain=SkillRefinementJob.default_model_chain,
                log_label="skill_refinement",
            )
        except Exception:
            log.warning("skill_refinement: role chain build failed", exc_info=True)
            return ""
        if not chain:
            return ""
        prompt = (
            f"{_PROMPT}\n\n{evidence}"
            f"\n--- CURRENT SKILL.md ---\n{current}\n--- END ---\n"
        )
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


def _outcome(
    candidates: list[dict[str, Any]], filed: int, flag_only: int, retired: int = 0,
) -> RunOutcome:
    """Which of the closed states this run was.

    Nothing crossing the threshold is the healthy common case and says so.
    A card filed with no proposed rewrite is DEGRADED rather than succeeded:
    the job's contract is an applyable diff, and a flag the operator has to
    act on by hand is less than that — arriving quietly as a success is how a
    dead model role goes unnoticed for a month.
    """
    if retired and not flag_only:
        return RunOutcome.SUCCEEDED
    if not candidates or filed == 0:
        return RunOutcome.SKIPPED_NO_WORK
    return RunOutcome.DEGRADED if flag_only else RunOutcome.SUCCEEDED


def _reason(
    candidates: list[dict[str, Any]], filed: int, flag_only: int, threshold: float,
    unreadable: int = 0, refused_again: int = 0,
) -> str:
    if not candidates:
        return (
            "no skill was corrected after more than "
            f"{round(threshold * 100)}% of its loads"
        )
    if filed == 0 and refused_again:
        return (
            f"{refused_again} skill(s) are still under the threshold, but you "
            "already turned down a rewrite of the text they have now"
        )
    if filed == 0:
        return "every underperforming skill already has a card waiting"
    # Two different reasons a card carries no rewrite, and saying the wrong
    # one is worse than saying neither: a skill whose file will not read was
    # never sent to a model, so reporting it as the model being unavailable
    # would send the operator looking at the wrong thing.
    if unreadable:
        return (
            f"{unreadable} of {filed} card(s) are for a skill whose file will "
            "not read, which no rewrite fixes"
        )
    if flag_only:
        return (
            f"{flag_only} of {filed} card(s) carry no proposed rewrite: the "
            "model was unavailable, so they are flags to refine by hand"
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


def _correction_text(memory_id: str, cap: int) -> str:
    """What the operator actually said, off the feedback memory that IS the
    correction, or "" when the row carries no reference to one.

    Read straight off the file rather than through `MemoryStore.read`, which
    logs an access: a background job counting evidence has not "recalled" the
    memory, and a retrieval-frequency signal that this job feeds would be
    measuring itself.
    """
    # The id becomes a path segment, so it is checked before it is one. It
    # comes off a log line the assistant cannot write, but a name built from
    # data is a name whichever way the data arrived, and a separator in it
    # would reach outside the store.
    if not memory_id or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", memory_id):
        return ""
    from tesseract.paths import home_dir

    store = home_dir() / "memory-store"
    # The store's own layout rather than a walk of it: `MemoryStore.find_file`
    # checks these buckets, and a whole-tree `rglob` per quoted row scaled the
    # cost with the operator's library instead of with the evidence.
    # The store's own list, not a second copy of it: a new memory type added
    # there would otherwise be a type this reader silently cannot quote.
    from tesseract.memory.store import MEMORY_SUBDIRS

    buckets = MEMORY_SUBDIRS
    found: Path | None = None
    for bucket in buckets:
        flat = store / bucket / f"{memory_id}.md"
        if flat.is_file():
            found = flat
            break
    if found is None:
        # The operator may curate sub-buckets (`reference/people/`), which
        # `find_file` supports, so a miss above is not an absence. Bounded to
        # the five buckets rather than the whole store.
        for bucket in buckets:
            found = next((store / bucket).rglob(f"{memory_id}.md"), None)
            if found is not None:
                break
    if found is None:
        return ""
    try:
        raw = found.read_text(encoding="utf-8")
    except OSError:
        return ""
    if _expired(raw):
        return ""
    body = raw.split("---", 2)[-1] if raw.startswith("---") else raw
    return " ".join(body.split())[:cap]


def _expired(raw: str) -> bool:
    """Whether a memory has passed its own `expiry_at`.

    Retrieval drops an expired record from the prefilter, and a quote carries
    the authority of something the operator still stands behind. Without this
    a correction they deliberately gave a shelf life could be read back into a
    rewrite prompt weeks after it lapsed.
    """
    match = re.search(r"^expiry_at:\s*(\S+)", raw, re.MULTILINE)
    if not match:
        return False
    try:
        when = datetime.fromisoformat(match.group(1).strip("'\"").replace("Z", "+00:00"))
    except ValueError:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when <= datetime.now(timezone.utc)


def _evidence_block(
    cand: dict[str, Any], window_days: int, max_rows: int, max_chars: int,
) -> str:
    """What was measured, in the words the card and the model both get.

    Wrapped in the untrusted envelope even though every figure in it was
    composed by the runtime from its own records, because the rule that
    survives contact is "anything not authored here goes in the envelope"
    and a block that is half trusted teaches a reader to skim the marker.
    The step numbers come off the usage rows, which the assistant's own
    behaviour produced.
    """
    from tesseract.kernel.tools.untrusted_envelope import wrap

    at = f" v{cand['version']}" if cand["version"] else ""
    lines = [
        f"{cand['skill']}{at}, over the last {window_days} days:",
        f"  {cand['neg']} of {cand['total']} loads were corrected afterwards.",
    ]
    if cand["errors"]:
        lines.append(
            f"  {cand['errors']} reads of the file failed. Not counted above: "
            "an unreadable file is not a procedure that misled."
        )
    if cand["unattributable"]:
        lines.append(
            f"  {cand['unattributable']} rows named no revision and are not "
            "counted above."
        )
    shown = [f for f in cand["failures"] if f.get("step") is not None][:max_rows]
    # Read once per row and carried: the count below used to re-read every
    # one of them, which is a second walk of the memory store per failure.
    quoted = [(f, _correction_text(f.get("memory_id", ""), max_chars)) for f in shown]
    if shown:
        lines.append("")
        lines.append("What went wrong, and where the turn had got to:")
        for f, said in quoted:
            lines.append(f"  step {f['step']} reached, {f['ts'][:19]}")
            if said:
                lines.append(f"    you said: {said}")
    held_back = len(cand["failures"]) - len(shown)
    if held_back > 0:
        lines.append(f"  and {held_back} more not shown.")
    lines.append("")
    unquoted = sum(1 for _, said in quoted if not said)
    ambiguous = sum(1 for f, said in quoted if not said and f.get("unattributed"))
    if unquoted:
        why = (
            " Of those, "
            f"{ambiguous} were recorded but could not be tied to this skill, "
            "because the session corrected more than one."
        ) if ambiguous else ""
        lines.append(
            f"{unquoted} of the corrections above carry no record of what was "
            f"said, so their cause is unknown.{why} Do not invent one."
        )
    lines.append(
        "A correction means the work was put right afterwards. It does not "
        "prove the procedure caused it."
    )
    return wrap(tool="skill_refinement", output="\n".join(lines), source="usage.jsonl")


def _sha256(text: str) -> str:
    """The bytes a proposal was written against, so a stale one can be told
    from a current one at the moment it is applied."""
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _live_revisions(skills_dir: Path) -> dict[str, str]:
    """Every skill FOLDER on disk, and the revision a usage row must carry.

    A playbook's is its whole-number `version`; a plain skill's is `""`,
    meaning its rows are counted by name because it has no revision to count
    against. A folder whose `SKILL.md` will not parse is also `""`, and that
    is the point of walking the folders rather than `load_skills`.

    **`load_skills` returns "every well-formed skill", and a skill that will
    not parse is exactly the one the unreadable card exists for.** Building
    this map from it dropped such a folder, so `_aggregate` skipped its rows
    before reaching the error counter and the card could never fire for a
    malformed file. That is the second time that path was closed: the first
    was the error branch sitting below the revision gates. A usage row is
    written from the folder path (`skill_usage.skill_name_for_path`) and does
    not care whether the file parses, so the reader must not either.
    """
    parsed = {e.name: e for e in load_skills(skills_dir)}
    out: dict[str, str] = {}
    try:
        folders = [p for p in skills_dir.iterdir() if p.is_dir()]
    except OSError:
        folders = []
    for folder in folders:
        # The quarantine trees hold skill folders, not skills. Excluded by
        # NAME, the way the loader excludes them, rather than inferred from
        # a SKILL.md happening not to sit at the top of one: a draft that is
        # inert until promoted must never be measured or offered a rewrite.
        if folder.name in SKIP_DIRNAMES:
            continue
        if not (folder / SKILL_FILENAME).exists():
            continue
        entry = parsed.get(folder.name)
        # A retired revision is a record, not an offer. `playbook_record`
        # leaves it out for the same reason, and refining something already
        # withdrawn puts a rewrite of it back in front of the operator.
        if entry is not None and entry.status == "retired":
            continue
        out[folder.name] = (
            entry.version if entry is not None and entry.is_playbook else ""
        )
    return out


def _aggregate(
    rows: list[dict[str, Any]], live: dict[str, str],
) -> dict[str, dict[str, Any]]:
    """Per-skill counts, against the revision that is live NOW.

    Three departures from counting every row under a skill's name, each
    measured on this machine rather than reasoned about.

    **Only the live revision counts.** A failure recorded against a revision
    that has since been replaced is not evidence about the text a rewrite
    would edit. `start-a-workshop-project` was proposed for rewrite at v3 on
    two corrections that both belonged to v1.

    **Corrections are the numerator, not corrections plus errors.** An
    `error` is a read of the SKILL.md that FAILED: the file is missing or
    unparseable, and no rewrite of its prose fixes that. It is counted and
    reported beside the ratio rather than inside it.

    **A correction never joins the denominator.** It is an EXTRA row that
    `attribute_session_corrections` appends beside the load it is about, so
    counting every row put one event on both sides and understated trouble:
    `ship-a-static-page-to-the-workshop` read 2/6 that way and is 2 in 4.

    `unattributable` is rows naming no revision, which is every `error` by
    construction (`skill_usage._version_at` reads the version off the file
    whose read just failed) and any correction that inherited from one. They
    are counted nowhere and REPORTED, because a negative dropped in silence
    is the failure this whole change exists to stop.
    """
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        skill = r.get("skill")
        if not skill or skill not in live:
            continue
        wanted = live[skill]
        bucket = out.setdefault(skill, {
            "version": wanted, "reads": 0, "loads": 0, "corrections": 0,
            "errors": 0, "unattributable": 0, "failures": [],
        })
        version = str(r.get("version") or "")
        # An error is a read of the file that FAILED, so it never carries a
        # version: `_version_at` reads that off the file whose read just
        # failed. Its missing version is expected rather than unattributable,
        # and it is a fact about the SKILL rather than about a revision, so it
        # is counted here before the revision gates. Putting it after them
        # made every error row unattributable, left `errors` at zero for every
        # playbook, and so made the unreadable card below unreachable on the
        # one kind of skill it exists for.
        if r.get("outcome") == "error":
            bucket["reads"] += 1
            bucket["errors"] += 1
            continue
        if wanted and not version:
            bucket["unattributable"] += 1
            continue
        if wanted and version != wanted:
            continue
        if r.get("outcome") == "correction":
            bucket["corrections"] += 1
            # The step is the only thing on a usage row that says WHERE the
            # procedure was when it went wrong, and until now it was written
            # and never read by anything. The correction's own words are not
            # here to be had: `attribute_session_corrections` is handed a
            # session id and records no memory reference, so the only join
            # back to what the operator said is session to session, which is
            # many to many. The card says so rather than guessing.
            bucket["failures"].append({
                "ts": r.get("ts") or "",
                "step": r.get("step"),
                "turn_id": r.get("turn_id") or "",
                "memory_id": r.get("memory_id") or "",
                "unattributed": bool(r.get("unattributed")),
            })
            continue
        bucket["reads"] += 1
        bucket["loads"] += 1
    return out


def _rank_candidates(
    stats: dict[str, dict[str, Any]], min_loads: int, ratio_threshold: float,
) -> list[dict[str, Any]]:
    cands: list[dict[str, Any]] = []
    for skill, b in stats.items():
        row = {
            "skill": skill, "version": b["version"], "total": b["loads"],
            "reads": b["reads"], "neg": b["corrections"], "errors": b["errors"],
            "unattributable": b["unattributable"], "failures": b["failures"],
        }
        # Two faults with two denominators, because they are not the same
        # question. "Was the procedure wrong" is asked of the loads that
        # SUCCEEDED on the live revision; a read that failed taught the
        # assistant nothing and must not dilute it. "Will the file read" is
        # asked of every attempt, which is the only denominator an error has.
        corrected = b["corrections"] / b["loads"] if b["loads"] else 0.0
        unreadable = b["errors"] / b["reads"] if b["reads"] else 0.0
        # Only one of the two is answered by rewriting prose. A skill whose
        # file keeps failing to read still has to reach the operator, so it
        # is a candidate too, but `_file_card` spends no model call on it.
        if b["loads"] >= min_loads and corrected >= ratio_threshold:
            cands.append({**row, "reason": "corrected", "ratio": corrected})
        elif b["reads"] >= min_loads and unreadable >= ratio_threshold:
            cands.append({**row, "reason": "unreadable", "ratio": unreadable})
    cands.sort(key=lambda c: (-c["ratio"], -c["neg"], c["skill"]))
    return cands


def _retire_worse_revisions(
    skills_dir: Path, window_days: int, min_loads: int, now: datetime,
) -> list[tuple[str, Reuse, Reuse]]:
    """Retire every active playbook revision that measured worse than its
    predecessor over the window. Returns what was retired, with both records,
    for the cards."""
    retired: list[tuple[str, Reuse, Reuse]] = []
    for entry in load_skills(skills_dir):
        if not entry.is_playbook or entry.status != "active":
            continue
        folder = skills_dir / entry.dirname
        live_number = version_number(entry.version)
        if live_number is None:
            continue
        # The revision this one replaced: the highest kept version below it.
        kept = [
            k for k in list_history(folder)
            if (version_number(k.version) or 0) < live_number
        ]
        if not kept:
            continue
        predecessor: SkillEntry = kept[-1]
        by_version = measure(entry.name, window_days=window_days, now=now)
        live = by_version.get(entry.version)
        before = by_version.get(predecessor.version)
        if live is None or before is None:
            continue
        if live.loads < min_loads or before.loads < min_loads:
            continue
        if worse_than(live, before) is not True:
            continue
        err = set_skill_status(folder, "retired")
        if err is not None:
            log.error("skill_refinement: could not retire %s v%s: %s", entry.name, entry.version, err)
            continue
        log.warning(
            "skill_refinement: retired %s v%s (trouble %.2f over %d loads) against v%s (%.2f over %d)",
            entry.name, live.version, live.trouble, live.loads,
            before.version, before.trouble, before.loads,
        )
        retired.append((entry.name, live, before))
    return retired


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
    # `home_logs_root` reads the environment itself on every call. Resolving
    # it again here was dead and read as though the override applied here.
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


def _refused_bases(store: Any) -> set[tuple[str, str]]:
    """(skill, base_sha256) pairs the operator has already said no to.

    A rejection is an answer about a proposal for a particular text. Without
    this, the next run reads the same log, reaches the same skill, and files
    the same card: the operator says no on Monday and is asked again on
    Tuesday, which teaches them to stop reading the cards. Keyed on the BYTES
    rather than on the skill, so editing the file by hand puts it back in
    scope, which is right: the thing they refused is no longer what is there.
    """
    try:
        return {
            (str((ev.payload or {}).get("name") or ""),
             str((ev.payload or {}).get("base_sha256") or ""))
            for ev in store.list_events(kinds=("skill_refinement",), status="rejected")
            if (ev.payload or {}).get("base_sha256")
        }
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

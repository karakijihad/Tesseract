"""WatchmanJob — read what the runtime actually did, and say so.

Its own row rather than a stage of the nightly pass: a runtime problem you
learn about at 23:00 is a runtime problem you lived with all day.

Every tick:

1. Reads the sources in `orchestrator/watchman/sources.py` over the window
   since its cursor — logs, breakers, worker records, the governor's pauses.
   Deterministic, no model, no network.
2. A window with no findings costs nothing: the summary says the runtime was
   quiet and no adapter is touched.
3. Otherwise a model turns the counted facts into one opening paragraph. It
   may not add a fact — a narration carrying a number the facts do not is
   dropped, and the run is `degraded` rather than quietly wrong.
4. Findings that are defects also get an evidence report the operator can hand
   upstream. Producing it is this job's business; sending it is theirs.
5. `home/autonomy/WHAT-RUNS.md` is re-derived, findings or none — what runs on
   this machine and whether it ran, in one file the assistant can read when
   the operator asks.

It inherited the heartbeat's role and budget when it succeeded that job; the
role is now named for this one, which is the only thing left on it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import timezone
from pathlib import Path
from typing import Any

from tesseract.kernel.adapters.base import AdapterOptions, ModelAdapter
from tesseract.orchestrator.outcome import RunOutcome
from tesseract.orchestrator.watchman import report, sources, tracker
from tesseract.orchestrator.watchman.findings import Sweep
from tesseract.scheduler.base_job import BaseJob
from tesseract.scheduler.role_chain import build_chain_for_job
from tesseract.scheduler.types import JobContext, JobResult

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 30.0


class WatchmanJob(BaseJob):
    uses_llm = True
    default_model_role = "watchman"

    async def run(self, ctx: JobContext) -> JobResult:
        t0 = time.monotonic()
        try:
            now = ctx.fired_at.astimezone(timezone.utc)
            start = report.read_cursor() or sources.default_window_start(now)
            # Reading a boot log's tail and every worker record is file IO in
            # the tens of milliseconds on a good day and seconds on a bad one.
            # The event loop carries health, WS heartbeats and inbound turns.
            swept = await asyncio.to_thread(
                sources.sweep, window_start=start, window_end=now
            )

            narration = ""
            model_called = False
            degraded_reason = ""
            if swept.findings:
                facts = report.fact_lines(swept)
                chain = build_chain_for_job(
                    ctx,
                    default_role=WatchmanJob.default_model_role,
                    log_label="watchman",
                )
                if chain:
                    model_called = True
                    raw = await _narrate(facts, chain)
                    if report.is_faithful(raw, facts):
                        narration = raw.strip()
                    else:
                        degraded_reason = (
                            "the model's summary cited figures the sources did "
                            "not, so only the counted facts are reported"
                            if raw.strip()
                            else "every model in the chain failed or answered "
                                 "empty, so the summary is the counted facts alone"
                        )
                else:
                    degraded_reason = (
                        "no model was reachable for this role, so the summary is "
                        "the counted facts alone"
                    )

            summary_path = await asyncio.to_thread(
                report.write_summary, swept, narration=narration
            )
            evidence = [
                await asyncio.to_thread(report.write_evidence, finding, observed_at=now)
                for finding in swept.defects
            ]
            # Every pass, findings or none: the tracker answers "what runs and
            # did it run", and a quiet window is an answer. It re-reads the run
            # log rather than being handed the collector's read — one file, an
            # hour apart, and keeping the collector free of anything that
            # writes is worth more than the read it saves.
            tracker_file = await asyncio.to_thread(
                tracker.refresh, now=now, window_start=start
            )
            notified = await _notify(ctx.app, swept, summary_path)
            # Only after the artifact is on disk. A cursor advanced before the
            # write turns a crash into a window nothing will ever look at again.
            report.write_cursor(now)

            payload = {
                "findings": len(swept.findings),
                "defects": len(swept.defects),
                "sources_read": sum(1 for r in swept.reads if r.present and not r.error),
                "sources_absent": sum(1 for r in swept.reads if not r.present),
                "model_called": model_called,
                "summary_path": str(summary_path),
                "evidence_paths": [str(p) for p in evidence],
                "tracker_path": str(tracker_file) if tracker_file else "",
                "notified": notified,
            }
            if degraded_reason:
                outcome, reason = RunOutcome.DEGRADED, degraded_reason
            else:
                outcome, reason = RunOutcome.SUCCEEDED, ""
            detail = (
                f"findings={len(swept.findings)} defects={len(swept.defects)} "
                f"model={'yes' if model_called else 'no'}"
            )
            return JobResult(
                job_name=ctx.job_name,
                run_id=ctx.run_id,
                ok=True,
                detail=detail,
                payload=payload,
                duration_ms=(time.monotonic() - t0) * 1000.0,
                outcome=outcome,
                outcome_reason=reason,
            )
        except Exception as exc:  # noqa: BLE001 — handler contract forbids raising
            log.exception("watchman crashed")
            return JobResult(
                job_name=ctx.job_name,
                run_id=ctx.run_id,
                ok=False,
                detail=f"unhandled: {exc!r}",
                duration_ms=(time.monotonic() - t0) * 1000.0,
                outcome=RunOutcome.FAILED,
                outcome_reason=f"the sweep crashed: {type(exc).__name__}",
            )


async def _notify(app: Any, swept: Sweep, summary_path: Path) -> int:
    """Ping the operator, and only about a defect.

    A quiet runtime sends nothing — an hourly all-clear is how a channel
    becomes one nobody reads. The rate cap in `channels.yaml` bounds a bad
    night; the notifier owns mutes and caps, so this decides only *whether
    there is something to say*.

    The path goes with it. The message is a summary of an artifact that is
    already on disk, and a summary that cannot be followed to the thing it
    summarises leaves the operator to go and find it.
    """
    if not swept.defects:
        return 0
    notifier = app.get("outbound_notifier") if hasattr(app, "get") else None
    if notifier is None:
        return 0
    try:
        result = await notifier.notify(
            "runtime_report",
            {
                "defects": len(swept.defects),
                "lines": [f.summary for f in swept.defects],
                "report_path": str(summary_path),
            },
        )
    except Exception:  # noqa: BLE001 — a report that cannot be sent is still written
        log.exception("watchman: outbound notify failed")
        return 0
    return int(getattr(result, "sent", 0) or 0)


def build_prompt(facts: list[str]) -> str:
    """The counted facts, and nothing else to work from.

    No log lines, no free text from the runtime — the model is given the same
    sentences the artifact prints, so anything it produces beyond rephrasing
    them is detectable by the faithfulness check rather than a matter of trust.
    """
    return "\n".join([
        "You are writing the opening line of a short operations report for the "
        "person who runs this machine.",
        "",
        "Below is EVERY fact that was observed. Write at most three sentences "
        "saying what they amount to. Do not add a number, a cause, a name or a "
        "recommendation that is not in the list. If the list is thin, say so "
        "plainly and stop.",
        "",
        "--- OBSERVED ---",
        *(f"- {line}" for line in facts),
        "",
        "Write the sentences now, with no preamble and no heading.",
    ])


async def _narrate(
    facts: list[str],
    chain: list[tuple[ModelAdapter, AdapterOptions]],
) -> str:
    prompt = build_prompt(facts)
    for adapter, options in chain:
        label = f"{options.provider or '?'}/{options.model or '?'}"
        try:
            out = await asyncio.wait_for(
                adapter.generate(prompt, options), timeout=DEFAULT_TIMEOUT_S
            )
        except asyncio.TimeoutError:
            log.warning("watchman: %s timed out after %.1fs", label, DEFAULT_TIMEOUT_S)
            continue
        except Exception as exc:  # noqa: BLE001
            log.warning("watchman: %s call failed (%s)", label, exc)
            continue
        if out and out.strip():
            return out
        log.warning("watchman: %s returned empty", label)
    return ""


__all__ = ["WatchmanJob", "build_prompt"]

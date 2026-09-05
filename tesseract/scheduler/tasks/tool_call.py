"""ToolCallJob — the recurring row that runs a tool.

The other generic primitive, `ScheduledTaskJob`, runs a prompt on a cadence.
This one runs a TOOL on a cadence, and it does not care whose tool it is: a
shipped tool, one of the operator's own out of the home workshop, or
`invoke_agent` pointed at an agent. One registry, one permission check, one
handler, so a home tool that wants polling needs no job module written for it
and no second route into the runtime.

That was the gap it closes. A tool like a mailbox poller lived under `tools/`
where a home tool belongs, scheduler handlers have to live under
`tesseract.scheduler.tasks`, and there was nothing in between. The only way to
poll was to write a job that reimplemented the tool, which is a second copy of
the work and a second copy of its permission story.

**What it may run is decided by posture, not by this file.** `tool_gate` holds
that rule and the reasoning; the short version is that a scheduled row fires
with nobody there to answer a prompt, so it may only name a tool that already
runs without asking. The gate is checked here again at fire time, because
`permissions.yaml` can change under a row that was armed months ago. A row
that fails it disables itself, says so in the Workspace and on whatever
channel the operator reads, and does not run. A row that merely could not be
CHECKED, which is what a fire during boot looks like, skips the tick instead:
the two refusals read alike and only one of them is settled.

The tool still goes through `execute_tool`, exactly as it would in a chat.
Nothing here is a way around the permission check; the whole point is that it
is the same one.

Config block:

    tool: the registered name
    args: what to call it with, validated against the tool's own schema
    title: what to call this row in the message it sends

Delivery is the row's `delivery`, or `routing.yaml` if it names none, the same
as every other job. A row routed nowhere on purpose is not a failed run.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from tesseract.lib.clock import to_local
from tesseract.orchestrator.outcome import RunOutcome
from tesseract.scheduler.base_job import BaseJob
from tesseract.scheduler.tasks._archive import archive_run
from tesseract.scheduler.tool_gate import (
    ToolNotSchedulable,
    check_call,
    read_call,
    runtime_from_app,
    scheduled_context,
    snapshot_registry,
)
from tesseract.scheduler.types import JobContext, JobResult

log = logging.getLogger(__name__)

#: What a tool's output is trimmed to before it becomes a message. The archive
#: on disk keeps the whole thing; a channel gets something a person can read.
_MAX_DELIVERED_CHARS = 4000


class ToolCallJob(BaseJob):
    uses_llm = False

    async def run(self, ctx: JobContext) -> JobResult:
        t0 = time.monotonic()
        try:
            live, policy = runtime_from_app(ctx.app)
            # One reading of the registry for the whole fire. The gate and the
            # dispatcher both resolve the tool by name, and a worker thread can
            # swap the registry's dict between the two, so what ran would not
            # be what was approved. `tool_gate._Snapshot` says the rest.
            registry = snapshot_registry(live)
            try:
                tool_name, args = read_call(ctx.config)
                check_call(
                    tool_name,
                    args,
                    registry=registry,
                    policy=policy,
                    context=scheduled_context(ctx.run_id, ctx.app),
                )
            except ToolNotSchedulable as refusal:
                return await _stand_down(ctx, t0, refusal)

            title = str((ctx.config or {}).get("title") or "").strip() or ctx.job_name
            result = await _invoke(registry, policy, ctx, tool_name, args)
            body = (result.output or "").strip()

            if result.is_error:
                return _result(
                    ctx, t0,
                    outcome=RunOutcome.FAILED,
                    detail=f"{tool_name} failed: {body[:400]}",
                    reason=body[:400] or f"{tool_name} failed without saying why.",
                    payload={"tool": tool_name},
                )
            if not body:
                return _result(
                    ctx, t0,
                    outcome=RunOutcome.SKIPPED_NO_WORK,
                    detail=f"{tool_name} had nothing to report",
                    reason=f"{tool_name} ran and found nothing to report.",
                    payload={"tool": tool_name},
                )

            # Archived before delivery so the run survives every channel being
            # down, which is what `ScheduledTaskJob` does and for the reason it
            # gives there.
            archived = archive_run(ctx.job_name, body, ctx.fired_at)
            delivered = await _deliver(ctx, title, _for_a_message(ctx, body, archived))
            reached = [
                name for name, why in getattr(delivered, "by_channel", ()) if why == "sent"
            ]
            payload = {
                "tool": tool_name,
                "archived": str(archived) if archived else None,
                "sent": getattr(delivered, "sent", 0),
                "channels": reached,
            }
            if delivered is None:
                return _result(
                    ctx, t0,
                    outcome=RunOutcome.DEGRADED,
                    detail=f"ran {tool_name}, nothing to deliver it with",
                    reason=(
                        f"{tool_name} ran and its result was saved, but there "
                        "was no way to send it anywhere."
                    ),
                    payload=payload,
                )
            if delivered.sent == 0 and delivered.reason != "no_destination":
                return _result(
                    ctx, t0,
                    outcome=RunOutcome.DEGRADED,
                    detail=f"ran {tool_name}, reached nobody: {delivered.reason or 'unknown'}",
                    reason=(
                        f"{tool_name} ran and its result was saved, but it "
                        f"reached nobody: {delivered.reason or 'no reason given'}."
                    ),
                    payload=payload,
                )
            where = ", ".join(reached) or "nowhere, by your routing"
            return _result(
                ctx, t0,
                outcome=RunOutcome.SUCCEEDED,
                detail=f"ran {tool_name}, delivered to {where}",
                reason="",
                payload=payload,
            )
        except Exception as exc:  # noqa: BLE001 — handler contract forbids raising
            log.exception("tool_call crashed")
            return _result(
                ctx, t0,
                outcome=RunOutcome.FAILED,
                detail=f"unhandled: {exc!r}",
                reason=f"the job stopped on an unexpected error: {exc}",
                payload={},
            )


def _for_a_message(ctx: JobContext, body: str, archived: Any) -> str:
    """What a person gets, which is not always all of it.

    A poll can return more than a message should carry, so the delivered copy
    is cut. What it must not do is simply stop: a reader has no way to tell a
    truncated message from a short one, and the rest of it is already on disk.

    It names the row's own archive and the day, not the path. The path is
    absolute and carries this machine's account name, the message goes wherever
    the routing sends it, and a local path is no use on the phone it is being
    read on.
    """
    if len(body) <= _MAX_DELIVERED_CHARS:
        return body
    dropped = len(body) - _MAX_DELIVERED_CHARS
    where = (
        " The whole thing is in this job's archive for "
        f"{to_local(ctx.fired_at).date().isoformat()}."
        if archived
        else ""
    )
    return (
        f"{body[:_MAX_DELIVERED_CHARS]}\n\n"
        f"[{dropped} more characters are not shown here.{where}]"
    )


async def _invoke(
    registry: Any, policy: Any, ctx: JobContext, tool_name: str, args: dict[str, Any]
) -> Any:
    """The tool call, through the runtime's one dispatch path.

    `ask_fn=None` is the truth about this context rather than a shortcut: no
    operator is attached to a scheduled fire, and a tool that wants one must
    be told so rather than handed a closure that answers on their behalf.
    """
    from tesseract.brain.tools import execute_tool

    context = scheduled_context(ctx.run_id, ctx.app)
    return await execute_tool(
        registry, tool_name, dict(args), context, ask_fn=None, policy=policy
    )


async def _stand_down(
    ctx: JobContext, t0: float, refusal: ToolNotSchedulable
) -> JobResult:
    """Refuse the run, and turn the row off if the refusal is a settled one.

    Leaving a permanently refused row enabled would mean refusing again on
    every tick and burying the one message that matters under a hundred
    identical ones. Off is also the honest state: it cannot do its job until
    somebody decides something, and the decision is the operator's.

    A refusal that only means "I cannot see the permission settings from here"
    gets none of that. It is a fact about the moment, not about the row, and a
    guard that turned a good row off over one would be the outage. That one
    skips the tick and tries again on the next.
    """
    if refusal.unknown:
        log.warning(
            "tool_call: %s could not be checked this tick (%s)",
            ctx.job_name,
            refusal.reason,
        )
        return _result(
            ctx, t0,
            outcome=RunOutcome.REFUSED,
            detail=f"skipped: {refusal.reason}",
            reason=refusal.reason,
            payload={"tool": refusal.tool_name, "disabled": False},
        )
    tool = refusal.tool_name or "the tool it names"
    headline = f"Scheduled job '{ctx.job_name}' is off"
    body = (
        f"{ctx.job_name} runs {tool} on a schedule, and it has just been "
        f"turned off. {refusal.reason}"
    )
    # Only the run that actually turns the row off says so. A disabled row can
    # still be fired by hand (`run_now` ignores `enabled`, which is what makes
    # the panel's Run now and `schedule_run` work), and a card plus a message
    # per press is the repetition turning the row off exists to prevent.
    if await _disable(ctx):
        await _file_nudge(ctx, headline, body, refusal)
        await _deliver(ctx, headline, body)
    return _result(
        ctx, t0,
        outcome=RunOutcome.REFUSED,
        detail=f"disabled: {refusal.reason}",
        reason=body,
        payload={"tool": refusal.tool_name, "disabled": True},
    )


async def _disable(ctx: JobContext) -> bool:
    """Turn the row off. True only when this call is what turned it off.

    The caller keys the card and the message off that, so the operator hears
    once about a row going off rather than once per attempt to run it.
    """
    app = ctx.app
    scheduler = app.get("scheduler") if hasattr(app, "get") else None
    if scheduler is None:
        log.warning(
            "tool_call: %s should be disabled and there is no scheduler to do it",
            ctx.job_name,
        )
        return False
    row = getattr(scheduler, "registry", {}).get(ctx.job_name)
    if row is not None and not getattr(row, "enabled", True):
        return False
    try:
        scheduler.set_enabled(ctx.job_name, False)
    except Exception:  # noqa: BLE001 — a run must not end on the tidy-up
        log.exception("tool_call: could not disable %s", ctx.job_name)
        return False
    return True


async def _file_nudge(
    ctx: JobContext, title: str, summary: str, refusal: ToolNotSchedulable
) -> None:
    """A Workspace card, because this needs a person and not a log line.

    It is a decision the operator has to make, which is what the Workspace is
    for. An ambient signal would go to the autonomy bus instead.
    """
    from tesseract.workspace_events import EventStore, WorkspaceEvent

    try:
        from tesseract.paths import home_logs_root

        store = EventStore(home_logs_root())
        event = WorkspaceEvent.new(
            kind="nudge",
            source="agent",
            title=title,
            summary=summary,
            payload={
                "origin": "tool_call",
                "job": ctx.job_name,
                "tool": refusal.tool_name,
                "reason": refusal.reason,
            },
        )
        store.append_event(event)
    except Exception:  # noqa: BLE001 — never lose the run over the card
        log.exception("tool_call: could not file the nudge for %s", ctx.job_name)
        return
    try:
        from tesseract.workspace_events.broadcast import broadcast_workspace_event

        if ctx.app is not None:
            await broadcast_workspace_event(ctx.app, event)
    except Exception:  # noqa: BLE001
        log.exception("tool_call: could not broadcast the nudge for %s", ctx.job_name)


async def _deliver(ctx: JobContext, title: str, body: str) -> Any:
    """The one notify path, which decides where. `None` when there is none."""
    app = ctx.app
    notifier = app.get("outbound_notifier") if hasattr(app, "get") else None
    if notifier is None:
        log.warning("tool_call: no notifier, %s reached nobody", ctx.job_name)
        return None
    try:
        return await notifier.notify(
            "scheduled_task_result",
            {"title": title, "body": body},
            destinations=ctx.delivery,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("tool_call: delivery failed (%s)", exc)
        return None


def _result(
    ctx: JobContext,
    t0: float,
    *,
    outcome: RunOutcome,
    detail: str,
    reason: str,
    payload: dict[str, Any],
) -> JobResult:
    return JobResult(
        job_name=ctx.job_name,
        run_id=ctx.run_id,
        ok=outcome is RunOutcome.SUCCEEDED,
        detail=detail,
        payload=payload,
        duration_ms=(time.monotonic() - t0) * 1000.0,
        outcome=outcome,
        outcome_reason=reason,
    )

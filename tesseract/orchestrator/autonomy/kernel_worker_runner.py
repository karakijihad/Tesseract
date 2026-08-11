"""KernelWorkerRunner — replaces ``_NoopRunner`` for live autonomy dispatch.

Translates a selected :class:`WorkerRecord` into a concrete tool call:

- ``CODER_SEAT`` → ``delegate_coder(task=record.prompt, background=False)``
- ``AUDITOR_SEAT`` → ``delegate_auditor(task=record.prompt, background=False)``
- ``MARKDOWN_AGENT`` → ``invoke_agent(name=record.role, task=record.prompt)``
- ``AGENT_SELF`` → ``invoke_agent(name=record.role|default_agent,
  task=record.prompt)``. Mission's ``AgentSelfWorker`` calls a SPECIFIC
  tool with operator-supplied inputs at plan time; autonomy doesn't
  know the tool, only the goal — so we route through a generic agent
  the same way ``MARKDOWN_AGENT`` does, falling back to
  ``DEFAULT_AGENT_SELF_AGENT`` when the agenda item didn't pin one.
- ``TERMINAL`` → graceful FAILED with ``unsupported_kind``. PTY lease
  + terminal substrate is an operator-attended pattern; lifting it
  into autonomy needs a UX decision (where does the operator watch
  The assistant in a terminal?). Deferred.

Per GOVERNANCE §6 the durable record landed BEFORE this runner was
called (in ``WorkerLane.admit`` / ``build_worker_record`` /
``write_record``). The runner mutates status in-place and writes the
record at every transition so recovery sees the latest state if the
process exits mid-run.

Concurrency model: the kernel wraps each ``run(record)`` call in a
background asyncio task. The runner itself awaits ``execute_tool`` to
completion — the wrapping task is the parallelism. ``background=False``
on delegate_* is intentional: we already ARE the background task, so
adding the spawn-registry hop would just defer the same result.

No ``ask_fn``. Autonomous dispatch is headless. Items that needed
operator approval transitioned through ``AWAITING_OPERATOR`` and only
reach the runner after the gate cleared — the approval IS the consent.
Tools that are ASK-posture at this layer will deny in headless and
surface as ``FAILED`` with the deny_reason in the summary; operator can
then either grant headless auto in ``permissions.yaml`` for that tool,
or rework the agenda item to use an AUTO-posture path.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from pathlib import Path
from typing import Any, Callable

from tesseract.brain.tools import ToolRegistry, execute_tool
from tesseract.config.cockpit import load_conductor_relay
from tesseract.kernel.tools.base import ToolContext
from tesseract.orchestrator.autonomy.summary_sanitize import clean_summary_tail
from tesseract.orchestrator.autonomy.worker_dispatch import WorkerRunner
from tesseract.orchestrator.workers.heartbeat import (
    HEARTBEAT_INTERVAL_SECONDS,
    touch_heartbeat,
)
from tesseract.orchestrator.workers.kinds import WorkerKind
from tesseract.orchestrator.workers.record import (
    Billing,
    WorkerRecord,
    WorkerStatus,
    write_record,
)


# Per-kind billing posture. CLI workers run under a flat-rate
# subscription — no per-call cost surfaces; the UI shows a "sub" badge
# instead of a misleading "$0.00". invoke_agent / agent_self hit the
# metered Anthropic API via chat_brain so usage IS reportable. Anything
# new must declare here or stay UNKNOWN.
_BILLING_BY_KIND: dict[WorkerKind, Billing] = {
    WorkerKind.CODER_SEAT: Billing.SUBSCRIPTION,
    WorkerKind.AUDITOR_SEAT: Billing.SUBSCRIPTION,
    WorkerKind.MARKDOWN_AGENT: Billing.API,
    WorkerKind.AGENT_SELF: Billing.API,
}

log = logging.getLogger(__name__)


_OUTPUT_SUMMARY_TAIL_CHARS = 500

# Default agent slug for ``AGENT_SELF`` when the agenda item didn't pin
# a real agent. Must match an actual entry under ``tesseract/agents/``.
DEFAULT_AGENT_SELF_AGENT = "agent-self"

# Default agent slug for ``MARKDOWN_AGENT`` when the agenda item didn't
# pin a real agent. ``research-brief`` is the generic operator-visible
# fallback (memory + vault + web search + brief output).
DEFAULT_MARKDOWN_AGENT = "research-brief"

# Known *model role* names from ``tesseract/config/roles.yaml``. Codex
# audit 2026-05-19 P0 #1: the kernel used to store ``self._rationale_role``
# (a model role) in ``WorkerRecord.role`` — which the runner then passed
# to ``invoke_agent`` as an agent slug, breaking every dispatch with
# ``Unknown agent: 'agents_default'``. Kernel no longer fills the field,
# but this set defends against (a) legacy records on disk from before
# the fix, (b) operator-pinned items that mis-use the field. Anything
# in here is rejected as a slug and the default is used instead.
_KNOWN_MODEL_ROLE_NAMES: frozenset[str] = frozenset({
    "chat_brain",
    "observer_agent",
    "mission_planner",
    "claude_cli",
    "codex_cli",
    "coder",
    "auditor",
    "agents_default",
    "subagents_default",
    "channel_vision",
    "feedback_consolidator",
    "autonomy_heartbeat",
    "autonomy_strategist",
    "autonomy_vetter",
    "autonomy_scout",
    "vision_agent",
    "image_generator",
    "audio_transcribe",
    "stt",
    "tts",
    "embeddings",
    "reranker",
})


def _resolve_agent_slug(record_role: str | None, default: str) -> str:
    """Return the agent slug for this record, falling back to ``default``
    when the field is empty or holds a model-role name. A real agent slug
    is hyphenated (e.g. ``agent-self``); the registry of model roles uses
    underscores. Provider-ref shapes (``<tier>.<provider>.<model>``) are
    also rejected for the same reason."""
    value = (record_role or "").strip()
    if not value:
        return default
    if value in _KNOWN_MODEL_ROLE_NAMES:
        return default
    if "." in value:  # provider-ref like ``api.openai.gpt54_mini``
        return default
    return value


class KernelWorkerRunner:
    """Concrete :class:`WorkerRunner` backed by ``execute_tool``.

    ``tool_registry`` MUST contain ``delegate_coder``, ``delegate_auditor``,
    and ``invoke_agent`` — wired by ``brain.boot._register_kernel_tool``.
    Missing tools surface as ``FAILED`` with ``tool_unavailable``.

    ``workspace_root`` is the directory tool calls see in their
    ``ToolContext`` (transcripts land under
    ``<workspace_root>/transcripts/<worker_id>.txt`` for delegate_*).
    Defaults to the repo root so artifacts stay under the project."""

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        workspace_root: str | Path = ".",
        policy: Any | None = None,
        worker_timeouts: dict[WorkerKind, float] | None = None,
        timeout_notifier: Any | None = None,
    ) -> None:
        self._registry = registry
        self._workspace_root = str(workspace_root)
        self._policy = policy
        # Operator-tunable wallclock budgets per kind, loaded from
        # agenda.yaml::worker_timeouts. Anything not declared uses the
        # underlying tool's own default (300s for delegate_coder). The
        # autonomy runner trusts the yaml — if the operator wants 1800s
        # for coder_seat that's what gets passed downstream.
        self._worker_timeouts: dict[WorkerKind, float] = dict(worker_timeouts or {})
        # Optional callback fired when a worker hits its wallclock limit.
        # Signature: ``await timeout_notifier(record)``. Wired by the
        # Mirror lifecycle to the OutboundNotifier so the operator gets
        # a Telegram ping ("worker X timed out, needs your input").
        # Never raises — handler swallows callback exceptions.
        self._timeout_notifier = timeout_notifier

    def _execution_root(self, record: WorkerRecord) -> str:
        """Where this worker's tools actually run.

        The kernel allocates an isolated worktree for code-editing workers and
        records it on the record. Running them in the shared base workspace
        instead would make that allocation a no-op and let concurrent workers
        edit the same tree.
        """
        return record.worktree_path or self._workspace_root

    async def run(self, record: WorkerRecord) -> None:
        # Surface that the dispatch reached the runner. Mission worker
        # would transition QUEUED → RUNNING here; the autonomy kernel
        # already set RUNNING in `_select_and_dispatch` per GOVERNANCE §6
        # so we don't double-transition. We only need to flip terminal
        # state when the tool returns. Records still in SPAWNING (e.g.
        # spawn-path dispatch) flip to RUNNING here so recovery never
        # sees a live worker stuck in a pre-run status.
        if record.status is WorkerStatus.SPAWNING:
            record.transition_to(WorkerStatus.RUNNING, reason="runner_start")
            write_record(record)
        # These workers run in-process, so nothing else touches their
        # heartbeat — `touch_heartbeat` had exactly one caller in the runtime
        # and it is the agent controller's PTY seats. Without this, a missing
        # heartbeat file made `stale_heartbeat` the guaranteed verdict for
        # every one of them at the next boot, healthy or not, so the signal
        # carried no information. Beat once up front so a worker that dies in
        # its first second still leaves a timestamp behind.
        # Guarded for the same reason the loop below is: a heartbeat describes
        # the work, it does not get to end it. Unguarded, a transient FS error
        # here would abort the dispatch before it started — the record would
        # land in FAILED via the kernel's outer handler, blaming the worker for
        # a file the runtime could not touch.
        try:
            touch_heartbeat(record.id)
        except OSError:
            log.warning("heartbeat write failed for %s", record.id, exc_info=True)
        beat = asyncio.create_task(
            self._beat_until_done(record.id), name=f"heartbeat-{record.id}"
        )
        try:
            await self._run_to_terminal(record)
        finally:
            beat.cancel()
            with suppress(asyncio.CancelledError):
                await beat

    async def _beat_until_done(self, worker_id: str) -> None:
        """Touch the heartbeat on the schema-locked interval until cancelled.

        Failures are logged and the loop continues: a heartbeat that cannot
        be written is worth knowing about, but it must not take down the
        worker whose liveness it was only describing.
        """
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
            try:
                touch_heartbeat(worker_id)
            except OSError:
                log.warning("heartbeat write failed for %s", worker_id, exc_info=True)

    async def _run_to_terminal(self, record: WorkerRecord) -> None:
        try:
            result = await self._dispatch(record)
        except Exception as exc:  # noqa: BLE001 — handler contract: never raise
            log.exception("kernel_worker_runner: dispatch raised for %s", record.id)
            record.error_class = type(exc).__name__
            record.error_message = str(exc)[:500]
            record.summary = f"runner crashed: {exc!r}"[:500]
            record.transition_to(WorkerStatus.FAILED, reason="runner_crash")
            write_record(record)
            return

        record.summary = result["summary"]
        if result.get("error_class"):
            record.error_class = result["error_class"]
        if result.get("error_message"):
            record.error_message = result["error_message"]
        record.billing = _BILLING_BY_KIND.get(record.kind, Billing.UNKNOWN)
        # Wallclock-exceeded: keep the item alive (BLOCKED) instead of
        # discarding the work as FAILED — operator can extend the budget
        # or comment to resume rather than re-queue from scratch.
        if result.get("timed_out"):
            record.transition_to(WorkerStatus.BLOCKED, reason="wallclock_exceeded")
            write_record(record)
            await self._fire_timeout_notification(record)
            return
        # Same "keep the work, park the worker" contract as the wallclock
        # case, for failures that are about this process rather than the
        # task (missing tool). No operator ping: nothing to extend.
        if result.get("parked"):
            record.transition_to(WorkerStatus.BLOCKED, reason=result["reason"])
            write_record(record)
            return
        terminal = WorkerStatus.DONE if result["ok"] else WorkerStatus.FAILED
        record.transition_to(terminal, reason=result["reason"])
        write_record(record)

    async def _fire_timeout_notification(self, record: WorkerRecord) -> None:
        if self._timeout_notifier is None:
            return
        try:
            await self._timeout_notifier(record)
        except Exception:  # noqa: BLE001 — notification failure is non-fatal
            log.exception(
                "kernel_worker_runner: timeout notifier raised for %s", record.id
            )

    async def _dispatch(self, record: WorkerRecord) -> dict[str, Any]:
        tool_name, tool_args = _route_for_kind(record)
        if tool_name is None:
            return {
                "ok": False,
                "summary": tool_args,  # rendered message
                "reason": "unsupported_kind",
                "error_class": "UnsupportedKindError",
                "error_message": tool_args,
            }

        # A tool the registry never got (chat_brain adapter unresolved at
        # boot → no invoke_agent) is an environment problem, not a bad
        # item: burning it as FAILED with "unknown tool: invoke_agent"
        # threw away 15 items on the live install. Park it instead, the
        # same way a wallclock overrun parks one.
        if self._registry.get(tool_name) is None:
            message = (
                f"{tool_name} is not registered in this process — "
                f"worker {record.id} cannot dispatch"
            )
            log.error("kernel_worker_runner: %s", message)
            return {
                "ok": False,
                "summary": message,
                "reason": "tool_unavailable",
                "error_class": "ToolUnavailableError",
                "error_message": message,
                "parked": True,
            }

        # Apply operator-configured timeout if the kind has one.
        # delegate_coder / delegate_auditor accept a ``timeout`` arg
        # (range 10–1800); invoke_agent ignores unknown kwargs.
        timeout = self._worker_timeouts.get(record.kind)
        if timeout is not None and tool_name in ("delegate_coder", "delegate_auditor"):
            tool_args = {**tool_args, "timeout": float(timeout)}

        from tesseract.orchestrator.agent_controller.lanes.ipc_proxy import (
            IpcLaneManager,
        )
        from tesseract.orchestrator.agent_controller.lanes.principals import (
            OPERATOR_PRINCIPAL,
        )

        ctx = ToolContext(
            workspace_root=self._execution_root(record),
            session_id="autonomy",
            current_call_id=record.id,
            # Autonomy dispatches delegate_coder / delegate_auditor, and a
            # delegation runs on a lane. The daemon is the only host of a real
            # LaneManager, so the proxy is what an out-of-Mirror caller uses.
            # Autonomy is the runtime acting on the operator's behalf; there
            # is no MCP client to attribute it to, and the daemon refuses a
            # lane message that names nobody.
            lane_manager_provider=lambda: IpcLaneManager(
                caller_principal=OPERATOR_PRINCIPAL
            ),
        )
        result = await execute_tool(
            self._registry,
            tool_name,
            tool_args,
            ctx,
            ask_fn=None,
            policy=self._policy,
        )
        output = (result.output or "")
        summary = clean_summary_tail(output, tail_chars=_OUTPUT_SUMMARY_TAIL_CHARS)
        if result.is_error:
            # Distinguish wallclock timeout from generic tool errors so
            # the caller can transition to BLOCKED instead of FAILED.
            # Primary signal is the structured ``ToolResult.timed_out``
            # flag (set by delegate_coder / delegate_auditor in their
            # timeout branches). The output-string sniff is a
            # belt-and-braces fallback for any tool that hasn't migrated
            # to the structured field yet.
            timed_out = bool(getattr(result, "timed_out", False)) or (
                "timed out" in output.lower()
            )
            reason = "tool_denied" if result.denied_hard else (
                "wallclock_exceeded" if timed_out else "tool_error"
            )
            return {
                "ok": False,
                "summary": summary or (result.deny_reason or reason),
                "reason": reason,
                "error_message": result.deny_reason or summary,
                "timed_out": timed_out,
            }
        return {"ok": True, "summary": summary, "reason": "ok"}


def _route_for_kind(record: WorkerRecord) -> tuple[str | None, Any]:
    """Pick the kernel-tool route for this worker kind.

    Returns ``(tool_name, args_dict)`` for supported kinds, or
    ``(None, "<error message>")`` for deferred kinds so the caller
    surfaces a clean FAILED row without raising.
    """
    prompt = (record.prompt or "").strip()
    if not prompt:
        return None, f"worker {record.id} has empty prompt"

    if record.kind is WorkerKind.CODER_SEAT:
        return "delegate_coder", {"task": prompt, "background": False}
    if record.kind is WorkerKind.AUDITOR_SEAT:
        return "delegate_auditor", {"task": prompt, "background": False}
    if record.kind is WorkerKind.MARKDOWN_AGENT:
        # ``record.role`` is an optional agent slug pin. When empty OR
        # holding a model-role name (legacy / mis-pinned), fall back to
        # the generic research agent — better than failing the dispatch.
        agent_name = _resolve_agent_slug(record.role, DEFAULT_MARKDOWN_AGENT)
        return "invoke_agent", {"name": agent_name, "task": prompt, "background": False}
    if record.kind is WorkerKind.AGENT_SELF:
        # Autonomy doesn't know which tool to call at plan time, so
        # route through invoke_agent. Slug resolution defends against
        # the prior model-role-as-slug bug (codex audit P0 #1).
        agent_name = _resolve_agent_slug(record.role, DEFAULT_AGENT_SELF_AGENT)
        return "invoke_agent", {"name": agent_name, "task": prompt, "background": False}
    if record.kind is WorkerKind.AGENT_CONTROLLER:
        # 2026-05-24 — accepted OPERATOR_GATE items now flow into a
        # fresh controller session whose chat brain orchestrates
        # claude / codex / agents. The dispatcher's
        # ``ensure_daemon_running`` is a safety net if the supervisor's
        # controller spawn failed.
        return "delegate_agent_controller", {
            "task": prompt,
            "title": (record.summary or "")[:80] or None,
            # Same rationale as delegate_*: the kernel's wrapping task IS
            # the background; the spawn-registry hop would defer nothing.
            "background": False,
        }
    return None, (
        f"worker kind {record.kind.value} is not yet wired in the autonomy runner "
        "(TERMINAL needs a UX decision before autonomy can lease a PTY pane)"
    )


__all__ = ["KernelWorkerRunner"]

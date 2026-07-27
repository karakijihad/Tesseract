"""KernelWorkerRunner — replaces ``_NoopRunner`` for live autonomy dispatch.

Translates a selected :class:`WorkerRecord` into a concrete tool call:

- ``CLAUDE_CLI`` → ``delegate_claude(task=record.prompt, background=False)``
- ``CODEX_CLI`` → ``delegate_codex(task=record.prompt, background=False)``
- ``MARKDOWN_AGENT`` → ``invoke_agent(name=record.role, task=record.prompt)``
- ``TARS_SELF`` → ``invoke_agent(name=record.role|default_agent,
  task=record.prompt)``. Mission's ``TarsSelfWorker`` calls a SPECIFIC
  tool with operator-supplied inputs at plan time; autonomy doesn't
  know the tool, only the goal — so we route through a generic agent
  the same way ``MARKDOWN_AGENT`` does, falling back to
  ``DEFAULT_TARS_SELF_AGENT`` when the agenda item didn't pin one.
- ``TERMINAL`` → graceful FAILED with ``unsupported_kind``. PTY lease
  + terminal substrate is an operator-attended pattern; lifting it
  into autonomy needs a UX decision (where does the operator watch
  TARS in a terminal?). Deferred.

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

import logging
from pathlib import Path
from typing import Any, Callable

from tesseract.brain.tools import ToolRegistry, execute_tool
from tesseract.kernel.tools.base import ToolContext
from tesseract.orchestrator.autonomy.summary_sanitize import clean_summary_tail
from tesseract.orchestrator.autonomy.worker_dispatch import WorkerRunner
from tesseract.orchestrator.workers.kinds import WorkerKind
from tesseract.orchestrator.workers.record import (
    Billing,
    WorkerRecord,
    WorkerStatus,
    write_record,
)


# Per-kind billing posture. CLI workers run under a flat-rate
# subscription — no per-call cost surfaces; the UI shows a "sub" badge
# instead of a misleading "$0.00". invoke_agent / tars_self hit the
# metered Anthropic API via chat_brain so usage IS reportable. Anything
# new must declare here or stay UNKNOWN.
_BILLING_BY_KIND: dict[WorkerKind, Billing] = {
    WorkerKind.CLAUDE_CLI: Billing.SUBSCRIPTION,
    WorkerKind.CODEX_CLI: Billing.SUBSCRIPTION,
    WorkerKind.MARKDOWN_AGENT: Billing.API,
    WorkerKind.TARS_SELF: Billing.API,
}

log = logging.getLogger(__name__)


_OUTPUT_SUMMARY_TAIL_CHARS = 500

# Default agent slug for ``TARS_SELF`` when the agenda item didn't pin
# a real agent. Must match an actual entry under ``tesseract/agents/``.
DEFAULT_TARS_SELF_AGENT = "tars-self"

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
})


def _resolve_agent_slug(record_role: str | None, default: str) -> str:
    """Return the agent slug for this record, falling back to ``default``
    when the field is empty or holds a model-role name. A real agent slug
    is hyphenated (e.g. ``tars-self``); the registry of model roles uses
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

    ``tool_registry`` MUST contain ``delegate_claude``, ``delegate_codex``,
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
        named_lane_manager_provider: Callable[[], Any] | None = None,
    ) -> None:
        self._registry = registry
        self._workspace_root = str(workspace_root)
        self._policy = policy
        self._named_lane_manager_provider = named_lane_manager_provider
        # Operator-tunable wallclock budgets per kind, loaded from
        # agenda.yaml::worker_timeouts. Anything not declared uses the
        # underlying tool's own default (300s for delegate_claude). The
        # autonomy runner trusts the yaml — if the operator wants 1800s
        # for claude_cli that's what gets passed downstream.
        self._worker_timeouts: dict[WorkerKind, float] = dict(worker_timeouts or {})
        # Optional callback fired when a worker hits its wallclock limit.
        # Signature: ``await timeout_notifier(record)``. Wired by the
        # Mirror lifecycle to the OutboundNotifier so the operator gets
        # a Telegram ping ("worker X timed out, needs your input").
        # Never raises — handler swallows callback exceptions.
        self._timeout_notifier = timeout_notifier

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
        terminal = WorkerStatus.DONE if result["ok"] else WorkerStatus.FAILED
        record.transition_to(terminal, reason=result["reason"])
        write_record(record)

    async def _dispatch_via_named_lane(
        self, record: WorkerRecord
    ) -> dict[str, Any]:
        """Route CLAUDE_CLI / CODEX_CLI through a NamedLane instead of
        delegate_claude / delegate_codex. Called only when
        self._named_lane_manager_provider is set.

        Routing matrix (phase-X-5-persistent-lanes.md §5):
          CLAUDE_CLI -> coder/claude (kind="claude")
          CODEX_CLI  -> auditor/codex (kind="codex")

        ensure is idempotent -- cold-start (no pre-opened lane) works
        fine; the lane is opened on demand under the same name. The model
        defaults are used only when opening a fresh lane; the binding
        record's kind check prevents accidental kind swaps on an existing
        lane."""
        from tesseract.config import cockpit
        from tesseract.orchestrator.tars_controller.lanes.named import NamedLaneError

        # Routing matrix. Lane names/kinds are the trio contract
        # (phase-X-5 §5); the model is resolved from cockpit.yaml ->
        # roles.yaml -> providers.yaml at dispatch time (roles are pillars —
        # a hardcoded id here previously pinned lanes to stale models).
        _LANE_ROUTE: dict[WorkerKind, tuple[str, str]] = {
            WorkerKind.CLAUDE_CLI: ("coder/claude", "claude"),
            WorkerKind.CODEX_CLI: ("auditor/codex", "codex"),
        }
        lane_name, kind = _LANE_ROUTE[record.kind]
        try:
            default_model = cockpit.trio_lane(lane_name)["model"]
        except Exception as exc:  # noqa: BLE001 — config error surfaces as worker failure
            return {
                "ok": False,
                "summary": f"trio lane model resolution failed: {exc}"[
                    :_OUTPUT_SUMMARY_TAIL_CHARS
                ],
                "reason": "tool_error",
                "error_class": type(exc).__name__,
                "error_message": str(exc)[:500],
            }
        prompt = (record.prompt or "").strip()
        if not prompt:
            return {
                "ok": False,
                "summary": f"worker {record.id} has empty prompt",
                "reason": "unsupported_kind",
                "error_class": "EmptyPromptError",
                "error_message": "empty prompt",
            }

        mgr = self._named_lane_manager_provider()  # type: ignore[misc]
        try:
            binding = await mgr.ensure(
                lane_name,
                kind=kind,
                model=default_model,
                working_dir=self._workspace_root,
            )
        except NamedLaneError as exc:
            return {
                "ok": False,
                "summary": str(exc)[:_OUTPUT_SUMMARY_TAIL_CHARS],
                "reason": "tool_error",
                "error_class": type(exc).__name__,
                "error_message": str(exc)[:500],
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "summary": f"named lane ensure failed: {exc}"[:_OUTPUT_SUMMARY_TAIL_CHARS],
                "reason": "tool_error",
                "error_class": type(exc).__name__,
                "error_message": str(exc)[:500],
            }

        try:
            send_result = await mgr.lane_manager.send(binding.lane_id, prompt)
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "summary": f"named lane send failed: {exc}"[:_OUTPUT_SUMMARY_TAIL_CHARS],
                "reason": "tool_error",
                "error_class": type(exc).__name__,
                "error_message": str(exc)[:500],
            }

        if not send_result.accepted:
            reason = getattr(send_result, "reason", "lane_rejected")
            return {
                "ok": False,
                "summary": f"lane {lane_name} rejected message: {reason}",
                "reason": "tool_error",
                "error_class": "LaneSendRejected",
                "error_message": str(reason)[:500],
            }
        return {
            "ok": True,
            "summary": f"dispatched to named lane {lane_name} ({binding.lane_id})",
            "reason": "ok",
        }

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
        # Named-lane fast path: when a NamedLaneManager is wired and the
        # worker kind maps to a named lane, route through the lane directly
        # (NamedLaneManager.ensure + LaneManager.send) instead of the
        # delegate_claude / delegate_codex execute_tool path.
        if self._named_lane_manager_provider is not None and record.kind in (
            WorkerKind.CLAUDE_CLI,
            WorkerKind.CODEX_CLI,
        ):
            return await self._dispatch_via_named_lane(record)

        tool_name, tool_args = _route_for_kind(record)
        if tool_name is None:
            return {
                "ok": False,
                "summary": tool_args,  # rendered message
                "reason": "unsupported_kind",
                "error_class": "UnsupportedKindError",
                "error_message": tool_args,
            }

        # Apply operator-configured timeout if the kind has one.
        # delegate_claude / delegate_codex accept a ``timeout`` arg
        # (range 10–1800); invoke_agent ignores unknown kwargs.
        timeout = self._worker_timeouts.get(record.kind)
        if timeout is not None and tool_name in ("delegate_claude", "delegate_codex"):
            tool_args = {**tool_args, "timeout": float(timeout)}

        ctx = ToolContext(
            workspace_root=self._workspace_root,
            session_id="autonomy",
            current_call_id=record.id,
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
            # flag (set by delegate_claude / delegate_codex in their
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

    if record.kind is WorkerKind.CLAUDE_CLI:
        return "delegate_claude", {"task": prompt, "background": False}
    if record.kind is WorkerKind.CODEX_CLI:
        return "delegate_codex", {"task": prompt, "background": False}
    if record.kind is WorkerKind.MARKDOWN_AGENT:
        # ``record.role`` is an optional agent slug pin. When empty OR
        # holding a model-role name (legacy / mis-pinned), fall back to
        # the generic research agent — better than failing the dispatch.
        agent_name = _resolve_agent_slug(record.role, DEFAULT_MARKDOWN_AGENT)
        return "invoke_agent", {"name": agent_name, "task": prompt, "background": False}
    if record.kind is WorkerKind.TARS_SELF:
        # Autonomy doesn't know which tool to call at plan time, so
        # route through invoke_agent. Slug resolution defends against
        # the prior model-role-as-slug bug (codex audit P0 #1).
        agent_name = _resolve_agent_slug(record.role, DEFAULT_TARS_SELF_AGENT)
        return "invoke_agent", {"name": agent_name, "task": prompt, "background": False}
    if record.kind is WorkerKind.TARS_CONTROLLER:
        # 2026-05-24 — accepted OPERATOR_GATE items now flow into a
        # fresh controller session whose chat brain orchestrates
        # claude / codex / agents. The dispatcher's
        # ``ensure_daemon_running`` is a safety net if the supervisor's
        # controller spawn failed.
        return "delegate_tars_controller", {
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

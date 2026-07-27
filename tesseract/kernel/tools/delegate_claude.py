"""DelegateClaudeTool — hands a task to the claude CLI subprocess.

Use for heavy lifts: large reads, multi-file refactors, careful audits.
One-shot, non-interactive. Returns final text output.

**Survival caveat.** This tool runs the claude subprocess in-process; a
backend crash mid-task kills the worker. For mission steps that must
survive a backend restart, use `delegate_tars_controller` instead — it
spawns a controller-managed session that outlives a backend bounce.

Headless-only (2026-05-24): always runs ``claude -p`` as a one-shot
subprocess. A chat brain that wants a controller-managed claude pane
must call ``delegate_tars_controller`` instead — that path spawns a
``tars_cli`` session whose chat brain drives claude/codex as
subprocesses. The PTY auto-route from MO-9-5 was removed because it
collapsed two distinct intents (one-shot headless vs.
controller-mediated session) into "raw claude TUI in a pane" with no
controller to survive a backend restart mid-edit.
"""

from __future__ import annotations

import logging
from typing import ClassVar

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

from tesseract.kernel.tools.base import PermissionResult, Tool, ToolContext, ToolResult
from tesseract.kernel.tools._delegate_runner import resolve_cli_model, run_delegate

# Output cleanup goes through the shared `_strip_control_sequences`
# (SGR colour + OSC title sequences — the latter would otherwise rename
# the operator's terminal to "claude").


def _cli_disabled_reason(provider: str) -> str | None:
    """Return a short reason string when the ``cli`` tier or this CLI provider
    is switched off in providers.yaml; otherwise None. Best-effort —
    config-load errors don't block the delegate (they surface elsewhere).
    Thin wrapper around `ConfigBundle.is_provider_enabled` so the tier+provider
    check has a single implementation."""
    try:
        from tesseract.config.loader import load_config
        ok, reason = load_config().is_provider_enabled("cli", provider)
        return None if ok else reason
    except Exception:
        return None


from tesseract.kernel.adapters.cli_utils import (
    claude_subscription_env as _subscription_env,
)


class DelegateClaudeInput(BaseModel):
    task: str = Field(description="Task prompt to send to the claude CLI")
    timeout: float = Field(default=300.0, ge=10, le=1800, description="Timeout in seconds (10–1800, default 300)")
    target_paths: list[str] = Field(
        default_factory=list,
        description=(
            "Repo-relative paths this task will edit. Declare them for edit "
            "tasks. If any path is under tesseract/mirror/**, this headless "
            "tool refuses and you must use "
            "start_controller_session(launch_terminal=True) instead."
        ),
    )
    background: bool = Field(
        default=True,
        description=(
            "True (DEFAULT, 2026-05-16): the claude subprocess runs as a "
            "background asyncio task; the tool returns a `spawn_handle` "
            "immediately so TARS can keep chatting with the operator "
            "and dispatch other work in parallel. Use spawn_check (poll) "
            "or spawn_await (block) to retrieve the result later. "
            "Set false ONLY when the next step in the same turn must "
            "consume the result immediately and there's nothing else to "
            "do meanwhile — the foreground path blocks the entire turn "
            "for the full delegate duration (up to `timeout` seconds). "
            "Foreground requests whose timeout exceeds "
            "runtime.yaml::max_foreground_delegate_timeout_s are "
            "auto-flipped to background."
        ),
    )


class DelegateClaudeTool(Tool):
    default_posture = "ask"

    risk_class: ClassVar[str] = "propose"

    @property
    def name(self) -> str:
        return "delegate_claude"

    @property
    def description(self) -> str:
        return (
            "Delegate a coding task to the claude CLI (heavy lifter). "
            "Use for large reads, multi-file refactors, careful audits. "
            "Returns final output text."
        )

    @property
    def input_schema(self) -> type[BaseModel]:
        return DelegateClaudeInput

    def is_concurrency_safe(self) -> bool:
        return False

    def is_read_only(self) -> bool:
        return False

    def check_permissions(self, tool_input: BaseModel, context: ToolContext) -> PermissionResult:
        return PermissionResult.PASSTHROUGH

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        from tesseract.kernel.tools.claude_stream_render import (
            ClaudeDelegateStreamParser,
        )

        inp = tool_input if isinstance(tool_input, DelegateClaudeInput) else DelegateClaudeInput(**tool_input.model_dump())
        # stream-json (fix-pass 2026-07-10): `--output-format text` printed
        # nothing until exit, so the DelegateCard showed "waiting for first
        # chunk" for the entire run and a timeout kill lost ALL output.
        # stream-json emits one NDJSON event per message; the parser renders
        # them live and extracts the final result text.
        return await run_delegate(
            tool_name="delegate_claude",
            cli_label="claude",
            provider="claude",
            build_argv=lambda: (
                "claude", "-p", inp.task,
                "--model", resolve_cli_model("claude_cli"),
                "--output-format", "stream-json",
                "--verbose",
                "--dangerously-skip-permissions",
            ),
            env=_subscription_env(),
            tool_input=inp,
            context=context,
            output_parser_factory=ClaudeDelegateStreamParser,
        )

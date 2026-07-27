"""DelegateCodexTool — hands a task to the codex CLI subprocess.

Use for code review, verification, and second opinions.
One-shot, non-interactive. Returns final text output.

Headless-only (2026-05-24): always runs ``codex exec`` as a one-shot
subprocess. A chat brain that wants a controller-managed codex pane
must call ``delegate_tars_controller`` instead — that path spawns a
``tars_cli`` session whose chat brain drives claude/codex as
subprocesses. The PTY auto-route from MO-9-6 was removed for the same
reason as MO-9-5: collapsing one-shot + controller-mediated into "raw
codex TUI in a pane" left no controller to survive a backend restart
mid-edit.
"""

from __future__ import annotations

import logging
from typing import ClassVar

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

from tesseract.kernel.adapters.cli_utils import (
    codex_subscription_env as _subscription_env,
    resolve_codex_executable as _resolve_codex,
)
from tesseract.kernel.tools.base import PermissionResult, Tool, ToolContext, ToolResult
from tesseract.kernel.tools._delegate_runner import resolve_cli_model, run_delegate
from tesseract.kernel.tools.delegate_claude import _cli_disabled_reason  # noqa: F401 — preserved import


class DelegateCodexInput(BaseModel):
    task: str = Field(description="Task prompt to send to the codex CLI")
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
            "True (DEFAULT, 2026-05-16): the codex subprocess runs as a "
            "background asyncio task; the tool returns a `spawn_handle` "
            "immediately so TARS can keep chatting and dispatch other "
            "work in parallel. Use spawn_check / spawn_await to retrieve "
            "the result. Set false ONLY when the next step in the same "
            "turn must consume the result immediately. Foreground requests "
            "whose timeout exceeds "
            "runtime.yaml::max_foreground_delegate_timeout_s are "
            "auto-flipped to background."
        ),
    )


class DelegateCodexTool(Tool):
    default_posture = "ask"

    risk_class: ClassVar[str] = "propose"

    @property
    def name(self) -> str:
        return "delegate_codex"

    @property
    def description(self) -> str:
        return (
            "Delegate an audit or review task to the codex CLI (auditor). "
            "Use for code review, verification, second opinions. "
            "Returns final output text."
        )

    @property
    def input_schema(self) -> type[BaseModel]:
        return DelegateCodexInput

    def is_concurrency_safe(self) -> bool:
        return False

    def is_read_only(self) -> bool:
        return False

    def check_permissions(self, tool_input: BaseModel, context: ToolContext) -> PermissionResult:
        return PermissionResult.PASSTHROUGH

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp = tool_input if isinstance(tool_input, DelegateCodexInput) else DelegateCodexInput(**tool_input.model_dump())
        # `_resolve_codex()` stays inside build_argv so it runs only after
        # run_delegate's terminal-handoff + cli-disabled guards pass — matches
        # the pre-refactor ordering (a missing codex binary must not pre-empt
        # the clean handoff-redirect / disabled-reason returns).
        return await run_delegate(
            tool_name="delegate_codex",
            cli_label="codex",
            provider="codex",
            build_argv=lambda: (
                _resolve_codex(), "exec",
                "--model", resolve_cli_model("codex_cli"),
                "--dangerously-bypass-approvals-and-sandbox",
                inp.task,
            ),
            env=_subscription_env(),
            tool_input=inp,
            context=context,
        )

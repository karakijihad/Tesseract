"""DelegateCodexExecTool — one-shot headless `codex exec` subprocess.

AUTO posture, read-only by construction. Per phase-SU-3-delegation-daemon.md §2.2 row 1.
Interactive delegations use delegate_auditor / delegate_coder.

Phase 5 (2026-05-22): when ``ToolContext.cli_sink`` is wired (chat-direct
call inside a Mirror session), the subprocess streams through
``run_subprocess_with_sink`` so the chat-side ``RunningSpawnsChip`` lights
up and the ``delegate-transcript`` canvas card shows live stdout (D-6: the
former right-side SpawnDrawer overlay retired in Y-3). Without a sink
(REPL, scheduler, mission), the bare-subprocess path is preserved
byte-for-byte so AUTO-posture audits and second opinions still work the
same way.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, Field

from tesseract.kernel.adapters.cli_utils import (
    codex_subscription_env,
    resolve_codex_executable,
)
from tesseract.kernel.tools.base import PermissionResult, Tool, ToolContext, ToolResult
from tesseract.kernel.tools.cli_stream import (
    _strip_control_sequences,
    run_subprocess_with_sink,
)

log = logging.getLogger(__name__)


class DelegateCodexExecInput(BaseModel):
    prompt: str = Field(
        ...,
        min_length=1,
        max_length=20000,
        description=(
            "Prompt to send to `codex exec`. Read-only by construction — "
            "codex exec returns parseable output without touching files."
        ),
    )
    timeout: float = Field(
        default=300.0,
        ge=5,
        le=1800,
        description="Subprocess timeout in seconds (5-1800, default 300).",
    )


class DelegateCodexExecTool(Tool):
    default_posture: ClassVar[str] = "auto"
    risk_class: ClassVar[str] = "autonomous"

    @property
    def name(self) -> str:
        return "delegate_codex_exec"

    @property
    def description(self) -> str:
        return (
            "Run `codex exec <prompt>` as a short-lived headless subprocess. "
            "Use for read-only audits, searches, second opinions. Returns "
            "stdout + exit code. No daemon, no PTY, no survival across "
            "backend restart — short call, retry on failure."
        )

    @property
    def input_schema(self) -> type[BaseModel]:
        return DelegateCodexExecInput

    def is_read_only(self) -> bool:
        return True

    def is_concurrency_safe(self) -> bool:
        return True

    def check_permissions(self, tool_input: BaseModel, context: ToolContext) -> PermissionResult:
        return PermissionResult.PASSTHROUGH

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp = (
            tool_input
            if isinstance(tool_input, DelegateCodexExecInput)
            else DelegateCodexExecInput.model_validate(tool_input.model_dump())
        )
        executable = resolve_codex_executable()
        env = codex_subscription_env()

        # Wire the spawn to the MCP hub (best-effort; a failed
        # provision must never block an audit/second-opinion run).
        from tesseract.kernel.tools._delegate_runner import provision_delegate_mcp
        from tesseract.orchestrator.seal_guard import safe_cwd

        # Second delegate spawn path: this tool does not route through
        # `run_delegate_foreground`, so it needs the seal guard of its own.
        spawn_cwd = str(safe_cwd(context.workspace_root or Path.cwd()))

        await provision_delegate_mcp("codex", spawn_cwd)

        # Sink route: chat-direct call with a Mirror cli_sink + call_id.
        # Output streams through cli_start / cli_output / cli_end envelopes
        # so RunningSpawnsChip + the delegate-transcript canvas card see it live.
        if context.cli_sink is not None and context.current_call_id:
            try:
                return await run_subprocess_with_sink(
                    tool_name=self.name,
                    argv=(executable, "exec", inp.prompt),
                    cwd=spawn_cwd,
                    timeout=inp.timeout,
                    sink=context.cli_sink,
                    call_id=context.current_call_id,
                    empty_message="codex exec returned empty output",
                    missing_message=f"codex executable not found",
                    env=env,
                )
            except asyncio.TimeoutError:
                return ToolResult(
                    output=f"codex exec timed out after {inp.timeout}s",
                    is_error=True,
                    timed_out=True,
                )

        # Headless fallback — REPL, scheduler, mission step. No drawer
        # visibility, but the existing AUTO-posture contract is preserved.
        try:
            proc = await asyncio.create_subprocess_exec(
                executable,
                "exec",
                inp.prompt,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=spawn_cwd,
            )
        except FileNotFoundError as exc:
            return ToolResult(
                output=f"codex executable not found: {exc}",
                is_error=True,
            )
        except OSError as exc:
            return ToolResult(
                output=f"failed to spawn codex exec: {exc}",
                is_error=True,
            )

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=inp.timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass
            return ToolResult(
                output=f"codex exec timed out after {inp.timeout}s",
                is_error=True,
                timed_out=True,
            )

        stdout_text = _strip_control_sequences(stdout_bytes.decode("utf-8", errors="replace"))
        stderr_text = _strip_control_sequences(stderr_bytes.decode("utf-8", errors="replace"))

        if proc.returncode != 0:
            tail = (
                stderr_text.strip()[-2000:]
                if stderr_text.strip()
                else stdout_text.strip()[-2000:]
            )
            return ToolResult(
                output=f"codex exec returned {proc.returncode}\n{tail}",
                is_error=True,
            )

        return ToolResult(output=stdout_text)


__all__ = ["DelegateCodexExecTool", "DelegateCodexExecInput"]

"""One read-only question to whichever command line tool holds the auditor seat.

Named for the job, the way `delegate_coder` and `delegate_auditor` are. The
tool this replaced was named for a vendor and called that vendor's helpers
directly, so the seat could not be re-pointed from `roles.yaml` and an install
without that particular CLI carried a tool that could never work.

Three things now come from config and none from here:

* WHICH tool answers is `roles.yaml::auditor`, read through the same
  `resolve_delegate_seat` the other two delegations use.
* WHAT command line runs is that provider's `read_only_command` in
  `providers.yaml`, with the prompt appended as the final argument. That is
  also where the read-only flags live, which matters because running without
  asking the operator first is the whole premise of this tool.
* WHICH binary is spawned is `resolve_cli_executable`, so a CLI whose
  installer has an awkward shape on this platform is handled once, for every
  caller.

Anything the seat cannot supply is reported to the caller in those terms. A
seat wired to a model rather than a command line tool, and a provider whose
catalog entry never said how to ask it one question, are two different
answers, and neither of them is "the delegation failed".

When `ToolContext.cli_sink` is wired (a chat-direct call inside a Mirror
session) the subprocess streams through `run_subprocess_with_sink`, so the
running-work chip lights up and the transcript card shows live output.
Without a sink (a scheduled job, a headless turn) it runs as a plain
subprocess and returns the whole answer at the end.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, Field

from tesseract.kernel.adapters.cli_utils import (
    resolve_cli_executable,
    subscription_env,
)
from tesseract.kernel.tools.base import PermissionResult, Tool, ToolContext, ToolResult
from tesseract.kernel.tools.cli_stream import (
    _strip_control_sequences,
    run_subprocess_with_sink,
)

log = logging.getLogger(__name__)

#: The delegation seat this tool asks. Reviews, verifies, second opinions,
#: never edits: the same seat `delegate_auditor` runs, because this is that
#: job with no conversation around it.
SEAT = "auditor"


class SeatUnavailable(RuntimeError):
    """The seat named nothing this tool can run, and why."""


class DelegateSecondOpinionInput(BaseModel):
    prompt: str = Field(
        ...,
        min_length=1,
        max_length=20000,
        description=(
            "The question to ask. It runs read only, so ask for findings "
            "rather than fixes."
        ),
    )
    timeout: float = Field(
        default=300.0,
        ge=5,
        le=1800,
        description="Seconds to wait before giving up (5 to 1800, default 300).",
    )
    provider: str | None = Field(
        default=None,
        description=(
            "Borrow a specific command line tool for this one call. Leave it "
            "unset to use whichever one holds the auditor seat in roles.yaml, "
            "which is the normal path. Any provider with a `<name>_cli` role "
            "is borrowable, and an unknown name is refused with the list of "
            "the ones that are."
        ),
    )


def _resolve(provider_override: str | None) -> tuple[str, tuple[str, ...]]:
    """The provider filling the seat and the argv to ask it one question.

    Raises `SeatUnavailable` with a sentence the caller can act on. Every
    branch says which file to open, because all four ways this fails are
    config saying something the runtime cannot carry out.
    """
    from tesseract.kernel.tools._delegate_runner import (
        _cli_disabled_reason,
        resolve_delegate_seat,
    )

    try:
        seat = resolve_delegate_seat(SEAT, provider_override)
    except Exception as exc:  # noqa: BLE001 — config resolution is authoritative
        raise SeatUnavailable(
            f"the {SEAT} seat resolves to nothing: {exc}. roles.yaml decides "
            f"which tool fills it."
        ) from exc

    if seat.tier != "cli":
        raise SeatUnavailable(
            f"the {SEAT} seat is wired to {seat.model!r}, which is a model "
            f"rather than a command line tool, and there is no command line to "
            f"run. Use delegate_auditor, which runs this seat on either kind."
        )

    reason = _cli_disabled_reason(seat.provider, tier=seat.tier)
    if reason:
        raise SeatUnavailable(reason)

    if not seat.read_only_command:
        raise SeatUnavailable(
            f"providers.yaml does not say how to ask {seat.provider} one "
            f"question without letting it write, so there is no safe command "
            f"line to run. Add a read_only_command line under "
            f"cli.{seat.provider}, or point the {SEAT} seat at a provider "
            f"that has one."
        )

    return seat.provider, seat.read_only_command


class DelegateSecondOpinionTool(Tool):
    default_posture: ClassVar[str] = "auto"
    risk_class: ClassVar[str] = "autonomous"

    group: ClassVar[str] = "handing-work-off"
    summary: ClassVar[str] = "Asks the auditor seat one read-only question and waits for the answer."
    use_when: ClassVar[str] = (
        "Use for a quick audit, search or second opinion with nothing to go "
        "back and forth about. It runs one command, reads only, and does not "
        "survive a backend restart, so retry it if it fails."
    )
    not_when: ClassVar[str] = (
        "Work that writes files, or that needs more than one exchange: use "
        "`delegate_auditor` for a review or `delegate_coder` for a build. A "
        "session that outlives the turn: use `delegate_agent_controller`."
    )
    depends_on: ClassVar[str] = ""

    @property
    def name(self) -> str:
        return "delegate_second_opinion"

    @property
    def input_schema(self) -> type[BaseModel]:
        return DelegateSecondOpinionInput

    def is_read_only(self) -> bool:
        return True

    def is_concurrency_safe(self) -> bool:
        return True

    def check_permissions(self, tool_input: BaseModel, context: ToolContext) -> PermissionResult:
        return PermissionResult.PASSTHROUGH

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp = (
            tool_input
            if isinstance(tool_input, DelegateSecondOpinionInput)
            else DelegateSecondOpinionInput.model_validate(tool_input.model_dump())
        )

        try:
            provider, base_argv = _resolve(inp.provider)
        except SeatUnavailable as exc:
            # Not `is_error` framed as a broken delegation: nothing ran, and
            # the caller's next move is a config edit rather than a retry.
            return ToolResult(
                output=f"{self.name} did not run: {exc}",
                is_error=True,
                metadata={"reason": "seat_unavailable", "seat": SEAT},
            )

        executable = resolve_cli_executable(base_argv[0])
        env = subscription_env(base_argv[0])
        argv = (executable, *base_argv[1:], inp.prompt)

        from tesseract.kernel.tools._delegate_runner import provision_delegate_mcp
        from tesseract.orchestrator.seal_guard import safe_cwd

        # This tool does not route through `run_delegate_foreground`, so it
        # carries the seal guard itself: `workspace_root` is the code tree,
        # which in a packaged install is the one an update overwrites.
        spawn_cwd = str(safe_cwd(context.workspace_root or Path.cwd()))

        await provision_delegate_mcp(provider, spawn_cwd)

        if context.cli_sink is not None and context.current_call_id:
            try:
                return await run_subprocess_with_sink(
                    tool_name=self.name,
                    argv=argv,
                    cwd=spawn_cwd,
                    timeout=inp.timeout,
                    sink=context.cli_sink,
                    call_id=context.current_call_id,
                    empty_message=f"{provider} answered with nothing",
                    missing_message=f"the {provider} command was not found on this machine",
                    env=env,
                )
            except asyncio.TimeoutError:
                return ToolResult(
                    output=f"{provider} did not answer within {inp.timeout:.0f}s",
                    is_error=True,
                    timed_out=True,
                    metadata={"provider": provider},
                )

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=spawn_cwd,
            )
        except FileNotFoundError as exc:
            return ToolResult(
                output=(
                    f"the {provider} command was not found on this machine "
                    f"({exc}). Install it, or point the {SEAT} seat in "
                    f"roles.yaml at a provider that is installed."
                ),
                is_error=True,
                metadata={"provider": provider},
            )
        except OSError as exc:
            return ToolResult(
                output=f"could not start {provider}: {exc}",
                is_error=True,
                metadata={"provider": provider},
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
                output=f"{provider} did not answer within {inp.timeout:.0f}s",
                is_error=True,
                timed_out=True,
                metadata={"provider": provider},
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
                output=f"{provider} exited with {proc.returncode}\n{tail}",
                is_error=True,
                metadata={"provider": provider},
            )

        return ToolResult(output=stdout_text, metadata={"provider": provider})


__all__ = [
    "SEAT",
    "DelegateSecondOpinionInput",
    "DelegateSecondOpinionTool",
    "SeatUnavailable",
]

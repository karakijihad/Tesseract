"""BashTool — executes shell commands with security layer.

Not concurrent-safe, not read-only. Requires Stage 2 security layer.
Commands pass through 26 numbered bash security checks before
execution. The 20 absolute-DENY checks block hard at
``check_permissions`` time and again at ``run`` time (defense in depth).
The 6 forced-ASK checks (8, 10, 15, 17, 18, 24) surface as
``PermissionResult.ASK`` and route through ``decide.evaluate``'s
``ask_fn`` for operator approval — once approved, ``run``'s
defense-in-depth gate lets the command proceed.
"""

from __future__ import annotations

import asyncio
import logging

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import PermissionResult, Tool, ToolContext, ToolResult
from tesseract.permissions.bash_security import check as security_check

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 30.0

# Mirrors `bash_security._LOCKED_POSTURE_YAMLS`, both spellings. Kept in sync
# by hand: this list only names the file in the operator-visible audit row, so
# a missing entry degrades the deny reason to "<unknown>" rather than letting
# the write through — silent, and precisely for the install-shaped paths the
# bare spelling exists to catch.
_LOCKED_POSTURE_YAMLS_FOR_EXTRACT: tuple[str, ...] = (
    "tesseract/config/permissions.yaml",
    "tesseract/config/roles.yaml",
    "tesseract/config/providers.yaml",
    "tesseract/config/mirror.yaml",
    "config/permissions.yaml",
    "config/roles.yaml",
    "config/providers.yaml",
    "config/mirror.yaml",
)


def _extract_locked_yaml(command: str) -> str:
    """Best-effort match of which locked yaml the command referenced.

    The bash_security check already confirmed one of the four paths is in
    the command; return the first match (lower-cased, posix form). Falls
    back to ``"<unknown>"`` only on a defensive miss — the security check
    would not have fired without one of the four being present.
    """
    lower = command.lower()
    for yaml_path in _LOCKED_POSTURE_YAMLS_FOR_EXTRACT:
        if yaml_path in lower or yaml_path.replace("/", "\\") in lower:
            return yaml_path
    return "<unknown>"


class BashInput(BaseModel):
    command: str = Field(description="The shell command to execute")
    timeout: float = Field(default=_DEFAULT_TIMEOUT, gt=0, le=600, description="Timeout in seconds (max 600)")


class BashTool(Tool):
    default_posture = "ask"

    risk_class: ClassVar[str] = "operator_gate"

    # Appended by decide.evaluate to security-layer denials. Steers the
    # model to a productive next move without naming the check or its
    # pattern (checks are numbered-not-named to avoid attack hints).
    security_deny_hint: ClassVar[str] = (
        "The command matched a security pattern. Prefer the dedicated file "
        "tools (file_read/file_write/file_copy/file_move) for file "
        "operations, use forward-slash relative paths, and avoid shell "
        "constructs that decode or substitute commands."
    )

    group: ClassVar[str] = "running-commands"
    summary: ClassVar[str] = (
        "Runs a raw shell command through the operating system."
    )
    use_when: ClassVar[str] = (
        "Use for an actual shell operation no dedicated tool covers — running "
        "a script, a build, a git command, a package manager."
    )
    not_when: ClassVar[str] = (
        "Reading a file's contents (`file_read`), searching file contents or "
        "names (`grep`, `glob`), or writing or editing a file (`file_write`) "
        "— those tools exist so this one doesn't have to, and reaching for "
        "bash to cat/grep/echo something costs an operator prompt for "
        "nothing. Most commands prompt the operator before running."
    )

    @property
    def name(self) -> str:
        return "bash"

    @property
    def input_schema(self) -> type[BaseModel]:
        return BashInput

    def is_concurrency_safe(self) -> bool:
        return False

    def is_read_only(self) -> bool:
        return False

    def check_permissions(self, tool_input: BaseModel, context: ToolContext) -> PermissionResult:
        inp = tool_input if isinstance(tool_input, BashInput) else BashInput(**tool_input.model_dump())
        result = security_check(inp.command)
        if result is not None:
            check_num, posture = result
            if posture == "ask":
                logger.info("Bash security check #%d forced ASK posture", check_num)
                return PermissionResult.ASK
            logger.warning("Bash security check #%d blocked command", check_num)
            if check_num == 25:
                # SU-5 acceptance — surface every posture-yaml write attempt
                # as an operator-visible audit row. Best-effort; never blocks
                # the DENY decision.
                try:
                    from tesseract.workspace_events.runtime_lock import emit_runtime_lock_deny

                    emit_runtime_lock_deny(
                        tool="bash",
                        locked_path=_extract_locked_yaml(inp.command),
                        reason=f"bash_security check #25 blocked write to a posture yaml",
                        command_excerpt=inp.command,
                        check_id="25",
                    )
                except Exception:  # noqa: BLE001
                    pass
            return PermissionResult.DENY
        # No security hit → defer to the permission policy (config-driven ASK/AUTO).
        return PermissionResult.PASSTHROUGH

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp = tool_input if isinstance(tool_input, BashInput) else BashInput(**tool_input.model_dump())

        # Security checks run again at execution time (defense in depth).
        # ASK-posture hits already cleared the operator-approval gate in
        # decide.evaluate; only "blocked" sentinels hard-fail here.
        sec_result = security_check(inp.command)
        if sec_result is not None:
            check_num, posture = sec_result
            if posture == "blocked":
                return ToolResult(
                    output=f"Command blocked by security check #{check_num}",
                    is_error=True,
                )

        try:
            process = await asyncio.create_subprocess_shell(
                inp.command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=context.workspace_root,
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=inp.timeout,
            )
        except asyncio.TimeoutError:
            return ToolResult(
                output=f"Command timed out after {inp.timeout}s",
                is_error=True,
            )
        except OSError as e:
            return ToolResult(output=f"Command failed: {e}", is_error=True)

        out = stdout.decode("utf-8", errors="replace")
        err = stderr.decode("utf-8", errors="replace")

        if process.returncode != 0:
            combined = f"Exit code: {process.returncode}\n"
            if out:
                combined += f"stdout:\n{out}\n"
            if err:
                combined += f"stderr:\n{err}"
            return ToolResult(output=combined.strip(), is_error=True)

        result = out
        if err:
            result += f"\nstderr:\n{err}"
        return ToolResult(output=result.strip() if result.strip() else "(no output)")

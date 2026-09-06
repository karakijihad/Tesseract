"""BashTool — executes shell commands with security layer.

**Where it runs.** The open project's root, which is what `git` already
resolves, or `cwd` when the caller names one. It used to be
`ToolContext.workspace_root` always: the code tree, fixed when the session was
built, which `project_open` does not move. A command aimed at the project the
operator had open therefore ran in this repository instead, silently and
successfully, and one of them rewrote the wrong repository's `origin`. The two
tools that both run git had to agree on where "here" is.


Not concurrent-safe, not read-only. Requires Stage 2 security layer.
Commands pass through 26 numbered bash security checks before
execution. The 20 absolute-DENY checks block hard at
``check_permissions`` time and again at ``run`` time (defense in depth).
The 6 forced-ASK checks (8, 10, 15, 17, 18, 24) surface as
``PermissionResult.ASK`` and route through ``decide.evaluate``'s
``ask_fn`` for operator approval — once approved, ``run``'s
defense-in-depth gate lets the command proceed. ``check_permissions``
also records which of the six fired on ``ToolContext.security_checks``,
because in the unattended mode ``decide.evaluate`` answers for them
itself rather than reaching ``ask_fn``: one runs, five are refused. That
classification is ``bash_security``'s, not this tool's.
"""

from __future__ import annotations

import asyncio
import logging
import pathlib

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import PermissionResult, Tool, ToolContext, ToolResult
from tesseract.permissions.bash_security import ask_reason as security_ask_reason
from tesseract.permissions.bash_security import asks as security_asks
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
    cwd: str | None = Field(
        default=None,
        description=(
            "Where to run it. Defaults to the open project's root, the same "
            "directory `git` acts on, so a command about the work you have "
            "open runs where that work is. Name one only to run somewhere "
            "else."
        ),
    )


def _working_dir(inp: "BashInput", context: ToolContext) -> str:
    """Where the command runs: what the caller named, else the open project,
    else the workspace root.

    **Both new answers are checked against the seal, and refused rather than
    relocated.** `bash_security`'s sealed-tree DENY reads the command TEXT, so
    it cannot see a directory handed to the subprocess beside it: `echo x >
    note.txt` names nothing sealed and lands wherever this says. A caller that
    named a directory on purpose is told no, which is the choice `git_tool`
    already made and for the reason its docstring gives — silently moving
    someone who typed a path is worse than telling them.

    `workspace_root` is the one that is MOVED rather than refused, because
    nobody chose it: it is what is left when the caller named nothing and no
    project is open, and in an installed app it IS the sealed code tree.
    Refusing it would take the shell out of the product; running in it would
    write into the tree an update wipes. `safe_cwd` is the answer the runtime
    already gives to this exact fact for CLI delegates, and it is a no-op
    outside an install, where the root is not sealed and comes back unchanged.

    Raises `SealViolation` for the two a caller DID choose, which `run` turns
    into an answer.
    """
    from tesseract.orchestrator.seal_guard import assert_cwd_outside_seal, safe_cwd

    if inp.cwd and inp.cwd.strip():
        named = inp.cwd.strip()
        assert_cwd_outside_seal(named)
        return named
    fallback = str(safe_cwd(context.workspace_root))
    try:
        from tesseract.orchestrator.projects.store import ProjectStore

        active = ProjectStore().active()
    except Exception:  # noqa: BLE001 — a broken registry is not this tool's error
        # Said out loud. The fallback is right, and running somewhere the
        # caller did not expect with no trace is the failure this whole fix
        # exists to close: `git` raises here, and a shell that quietly
        # disagrees with it is the same silence in a different tool.
        logger.warning(
            "bash: the project registry could not be read, so the command runs "
            "in %s rather than the open project", fallback,
            exc_info=True,
        )
        return fallback
    if active is None:
        return fallback
    root = pathlib.Path(active.root)
    if not root.is_dir():
        return fallback
    assert_cwd_outside_seal(root)
    return str(root)


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
        "Use for an actual shell operation no dedicated tool covers, such as running "
        "a script, a build, a git command, a package manager."
    )
    not_when: ClassVar[str] = (
        "Reading a file's contents (`file_read`), searching file contents or "
        "names (`grep`, `glob`), or writing or editing a file (`file_write`) "
        "Those tools exist so this one does not have to, and reaching for "
        "bash to cat/grep/echo something costs an operator prompt for "
        "nothing. Most commands prompt the operator before running."
    )
    depends_on: ClassVar[str] = ""

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

    def ask_reason(self, validated: BaseModel) -> str:
        """What the operator is being asked to approve, and why they are asked.

        Read by the channel gate, which is the surface with the least room:
        away from the desk the prompt was the tool name and nothing else, so
        an unexpected one sent the operator looking at a security mode that
        could not have caused it. `bash_security` composes the sentence,
        because the checks are its and a second wording here would drift from
        them.
        """
        inp = (
            validated
            if isinstance(validated, BashInput)
            else BashInput(**validated.model_dump())
        )
        return security_ask_reason(inp.command)

    def check_permissions(self, tool_input: BaseModel, context: ToolContext) -> PermissionResult:
        inp = tool_input if isinstance(tool_input, BashInput) else BashInput(**tool_input.model_dump())
        result = security_check(inp.command)
        if result is not None:
            check_num, posture = result
            if posture == "ask":
                logger.info("Bash security check #%d forced ASK posture", check_num)
                # Every check that fired, not just the reported one. What may
                # run unattended is decided against all of them
                # (`bash_security.asks`).
                context.security_checks = security_asks(inp.command)
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

        from tesseract.orchestrator.seal_guard import SealViolation

        try:
            where = _working_dir(inp, context)
        except SealViolation as exc:
            return ToolResult(output=f"bash: {exc}", is_error=True)

        try:
            process = await asyncio.create_subprocess_shell(
                inp.command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=where,
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

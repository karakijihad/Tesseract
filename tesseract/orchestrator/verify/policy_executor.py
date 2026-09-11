"""The gate's production executor — one verify command through the bash policy path.

Nothing here decides whether a command is allowed. `decide.evaluate` does, over
the same `BashTool` instance the assistant uses, which means `bash_security`'s numbered
checks run first, `permissions.yaml` is consulted second, and
`readonly_commands.is_readonly_allowed` is what promotes an allowlisted
invocation to AUTO. A verify command carries no special authority: a project
that registers `rm -rf /` as its "test" meets the same DENY everything else does.

**A verify command is a `bash` call, so it leaves the same trail one leaves
anywhere else.** This is the second place in the runtime that dispatches a
tool (`brain/tools.py::execute_tool` is the first), and a command that ran
outside that function used to leave nothing on the record: a crash during a
project's test suite left `checkpoints.latest_step` pointing at the last
call before the gate, which is worse than pointing at nothing, because a
resuming pass would believe it. `bash` declares `recovery_behaviour =
"unsafe"`, which is exactly the class this record exists for.

The gate is given the *tool's* answer rather than a raw subprocess result on
purpose. `BashTool.run` re-runs the security checks at execution time (defense
in depth) and bounds the command with its own timeout; reimplementing the spawn
here to get a cleaner exit code would step around both.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from tesseract.brain.tools import noting_the_park
from tesseract.kernel.tools.base import ToolContext, ToolResult
from tesseract.kernel.tools.bash_tool import BashInput, BashTool
from tesseract.kernel.tools.recovery import behaviour_of as recovery_behaviour_of
from tesseract.orchestrator import checkpoints
from tesseract.permissions import decide

from .gate import ExecOutcome

# `BashTool.run` formats a failure as "Exit code: N\nstdout:\n...\nstderr:...".
# The code is recovered for reporting only — pass/fail is taken from
# `ToolResult.is_error`, which is the tool's own answer and cannot drift from
# it. A format change costs the number in a rendered brief, never a verdict.
_EXIT_CODE_RE = re.compile(r"^Exit code: (-?\d+)", re.MULTILINE)


def _split_tool_output(text: str) -> tuple[str, str]:
    """Recover (stdout, stderr) from BashTool's combined failure rendering."""
    stdout, stderr = text, ""
    if "\nstderr:\n" in text:
        stdout, stderr = text.split("\nstderr:\n", 1)
    if stdout.startswith("Exit code: "):
        _, _, rest = stdout.partition("\n")
        stdout = rest
    if stdout.startswith("stdout:\n"):
        stdout = stdout[len("stdout:\n") :]
    return stdout, stderr


class PolicyExecutor:
    """Runs verify commands as `bash` tool calls under a real permission policy.

    `workspace_root` on the per-call `ToolContext` is what puts the command in
    the project's directory — `BashTool.run` spawns with `cwd=context.
    workspace_root` and takes no cwd argument of its own.
    """

    def __init__(
        self,
        *,
        policy: Any,
        ask_fn: Any = None,
        session_id: str = "",
        caller_principal: str = "",
        chat_id: str = "",
        run_id: str = "",
    ) -> None:
        self._tool = BashTool()
        self._policy = policy
        self._ask_fn = ask_fn
        self._session_id = session_id
        self._caller_principal = caller_principal
        # Which file the boundaries below are written to. Given by the
        # caller rather than read off its own context, because the context
        # this class builds per call is its own and carries neither.
        self._chat_id = chat_id
        self._run_id = run_id

    async def _note(self, boundary: str, behaviour: str, call_id: str) -> None:
        """One boundary for the command this executor is about to run."""
        await checkpoints.step(
            session_id=self._session_id,
            chat_id=self._chat_id,
            run_id=self._run_id,
            boundary=boundary,
            tool=self._tool.name,
            call_id=call_id,
            recovery=behaviour,
        )

    async def __call__(
        self, command: str, *, cwd: str, timeout_s: float
    ) -> ExecOutcome:
        validated = BashInput(command=command, timeout=timeout_s)
        raw_input = {"command": command, "timeout": timeout_s}
        context = ToolContext(
            workspace_root=cwd,
            session_id=self._session_id,
            caller_principal=self._caller_principal,
            ask_fn=self._ask_fn,
            policy=self._policy,
        )

        behaviour = recovery_behaviour_of(self._tool)
        # Minted, because there is no model here to have named the call, and
        # `open_calls` pairs a boundary to its close on this id: a row with
        # none can never be reported open and so can never be cleared.
        call_id = uuid.uuid4().hex
        await self._note("before_tool", behaviour, call_id)

        refusal: ToolResult | None = await decide.evaluate(
            self._tool,
            validated,
            raw_input,
            context,
            noting_the_park(self._ask_fn, context, self._tool.name, behaviour),
            self._policy,
        )
        if refusal is not None:
            # Covers hard DENY, path validation, policy DENY, an ASK with no
            # approval channel, and an operator decline. Every one of them means
            # the command did not run, which is never a pass.
            # Closed on the way out, so a refused command does not leave the
            # tail reading as one still in flight.
            await self._note("after_tool", behaviour, call_id)
            return ExecOutcome(
                blocked_reason=refusal.deny_reason or refusal.output,
            )

        result = await self._tool.run(validated, context)
        await self._note("after_tool", behaviour, call_id)
        if not result.is_error:
            return ExecOutcome(exit_code=0, stdout=result.output)

        match = _EXIT_CODE_RE.search(result.output)
        if match is None:
            # A timeout, an OSError, or a run-time security block — the command
            # produced no exit status, so there is nothing to call a failure of.
            return ExecOutcome(blocked_reason=result.output.strip())

        stdout, stderr = _split_tool_output(result.output)
        return ExecOutcome(exit_code=int(match.group(1)), stdout=stdout, stderr=stderr)


__all__ = ["PolicyExecutor"]

"""command_run — run your own code with one of your own accounts.

`api_request` is for a service that takes a single value in a single place,
and the runtime builds that request and checks the address it goes to. This is
for everything else: a sign-in that wants an id beside a secret, a long-lived
token exchanged for a short-lived one, a signature computed over both. You
write the code that knows the service; this hands your code the account's
fields and runs it.

Every field arrives as an environment variable named after the field,
uppercased. Those are the names you asked for when the account was set up, and
`credential_list` prints them, so nothing has to be guessed.

**No shell.** The program and its arguments are given as a list and handed
straight to the operating system. A shell would re-read the command text and
substitute the very variables this is carrying, which is how a value ends up
inside a command line, and a command line is written down in places a value
must not be.

**The environment is built for this one call, and dies with it.** The
runtime's own environment is not touched and nothing is written to disk. The
command runs inside a container the operating system tears down when the call
ends, so a process it started and walked away from goes too, rather than
outliving the tool that reported it finished.

**What you print comes back checked.** Everything the account holds is
screened out of the result before you see it, so printing a value for
debugging yields its name rather than the value. That is the guarantee this
tool spends: the runtime does not check where your code sends what it holds.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from tesseract.kernel.tools import _process_containment as containment
from tesseract.kernel.tools.base import (
    PermissionResult,
    Tool,
    ToolContext,
    ToolResult,
)
from tesseract.permissions.bash_security import check as security_check

logger = logging.getLogger(__name__)

#: How many arguments one command may carry. Nothing legitimate needs more,
#: and the whole list is echoed back to a person in the approval prompt.
_MAX_ARGS = 64

#: How long stopping a command may take before the attempt is given up on and
#: reported. Not a `runtime.yaml` number: this is not a policy an operator
#: tunes, it is the bound that keeps a `finally` from hanging a turn.
_REAP_TIMEOUT_S = 5.0


def _limits() -> tuple[float, int, int]:
    """How long it may run, how much is read, how much is reported.

    All three in `runtime.yaml`, read at call time. The loader refuses a
    report ceiling that is not below the read ceiling: the read ceiling is
    itself a cut, and a value straddling it stays out of the report only while
    the report is the shorter of the two.
    """
    from tesseract.config.runtime_limits import (
        default_runtime_config_path,
        load_command_run_max_output_bytes,
        load_command_run_report_chars,
        load_command_run_timeout_s,
    )

    path = default_runtime_config_path()
    return (
        load_command_run_timeout_s(path),
        load_command_run_max_output_bytes(path),
        load_command_run_report_chars(path),
    )


class CommandRunInput(BaseModel):
    credential_id: str = Field(
        description=(
            "Which of your accounts the command is given, from "
            "`credential_list`. It has to be one recorded as read from a "
            "command's environment."
        ),
        min_length=1,
        max_length=64,
    )
    command: list[str] = Field(
        description=(
            "The program and its arguments, as a list: "
            "['python', 'send_mail.py', '--to', 'someone@example.com']. Not a "
            "shell line, so quoting, pipes and redirection do nothing here."
        ),
        min_length=1,
    )
    cwd: str | None = Field(
        default=None,
        description=(
            "Where to run it. Defaults to the open project's root, the same "
            "directory `bash` and `git` act on."
        ),
    )


def _without_the_value(exc: Exception) -> str:
    """What a launch failure says, with no stored value left in it.

    `execute_tool` screens every tool result and would catch one, but this
    tool knowingly holds every field of an account, so it does not export the
    problem to a wall two layers up. `api_request` reasons the same way about
    its transport errors, and the tool holding more must not check less. If
    the store cannot be read the value cannot be looked for, and then the
    class of failure is all this may say.
    """
    from tesseract.credentials.redaction import RedactionUnavailable, redact

    try:
        return redact(str(exc))
    except RedactionUnavailable:
        return type(exc).__name__


def _reportable(text: str, ceiling: int) -> str:
    """What the command printed, checked for stored values, then cut.

    The order is the point and is not a preference. `redact` matches a stored
    value exactly, so cutting first would destroy a value that straddles the
    cut into two fragments nothing downstream could find, and the wall at
    `execute_tool` would see only what was left.

    A store that cannot be read means the output cannot be checked, and
    unchecked output from a command that was handed every field of an account
    is not shown at all.
    """
    from tesseract.credentials.redaction import RedactionUnavailable, redact

    try:
        checked = redact(text)
    except RedactionUnavailable as exc:
        return (
            f"[what it printed is not shown: the credential store could not "
            f"be read, so it could not be checked for stored values. {exc}]"
        )
    if len(checked) <= ceiling:
        return checked
    return checked[:ceiling] + f"\n[... {len(checked) - ceiling} more characters]"


def _working_dir(inp: CommandRunInput, context: ToolContext) -> str:
    """Where the command runs. The same answer `bash` gives, for one reason.

    Two tools that run a program and disagree about where "here" is have
    already cost this project a rewritten git remote. `bash_tool::_working_dir`
    is that answer, and calling it is how the two cannot drift apart.
    """
    from tesseract.kernel.tools.bash_tool import BashInput, _working_dir as bash_cwd

    return bash_cwd(BashInput(command="", cwd=inp.cwd), context)


class CommandRunTool(Tool):
    default_posture = "ask"

    risk_class: ClassVar[str] = "operator_gate"

    group: ClassVar[str] = "your-own-accounts"
    summary: ClassVar[str] = (
        "Run your own code with one of your accounts in its environment."
    )
    use_when: ClassVar[str] = (
        "Use when a service wants more than one value, or one value "
        "exchanged for another before it accepts anything: an id beside a "
        "secret, a refresh token traded for a short-lived one, a signed "
        "request. Write what the service documents, ask for the fields with "
        "`credential_request`, and run it here. They arrive as environment "
        "variables, in capitals."
    )
    not_when: ClassVar[str] = (
        "a service that takes one value in a header or a query, which is "
        "`api_request` and is safer because the runtime checks the address "
        "too. Running anything that does not need an account, which is "
        "`bash`. Pushing and pulling, which `git` does with its own account."
    )
    depends_on: ClassVar[str] = ""

    security_deny_hint: ClassVar[str] = (
        "The command matched a security pattern. This tool runs a program "
        "with one of your accounts in its environment; it is not a way around "
        "what the shell refuses."
    )

    @property
    def name(self) -> str:
        return "command_run"

    @property
    def input_schema(self) -> type[BaseModel]:
        return CommandRunInput

    def is_concurrency_safe(self) -> bool:
        return False

    def is_read_only(self) -> bool:
        return False

    def check_permissions(
        self, tool_input: BaseModel, context: ToolContext
    ) -> PermissionResult:
        """The same 26 checks `bash` runs, over the same text.

        This tool starts a process, which is the thing those checks are about.
        Twenty of them are absolute denials the repo describes as unrelaxable
        by any hook, plugin, skill or agent, and check 26 is what enforces the
        seal on `app/` and `runtime/` at runtime. A second tool that starts
        processes and skips them does not merely have a gap: it makes every
        one of those denials optional, because there is another door beside
        the locked one.

        The argv is joined with spaces to give the checks the string shape
        they read. That over-matches slightly, since an argument containing a
        shell character is not a shell construct here, and over-matching is
        the right direction: the outcome is an operator prompt or a refusal
        with a reason, not a value going somewhere unapproved.
        """
        inp = (
            tool_input
            if isinstance(tool_input, CommandRunInput)
            else CommandRunInput(**tool_input.model_dump())
        )
        result = security_check(" ".join(inp.command))
        if result is None:
            return PermissionResult.PASSTHROUGH
        check_num, posture = result
        if posture == "ask":
            logger.info("command_run: security check #%d forced ASK", check_num)
            return PermissionResult.ASK
        logger.warning("command_run: security check #%d blocked the command", check_num)
        return PermissionResult.DENY

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        from tesseract.credentials.reader import (
            NotReadableAsEnvironment,
            VariableAlreadySet,
            environment_for,
        )
        from tesseract.credentials.store import (
            CredentialNotSet,
            CredentialStoreError,
            UnknownCredentialError,
        )
        from tesseract.orchestrator.seal_guard import SealViolation

        assert isinstance(tool_input, CommandRunInput)
        # Again, at the moment it would run. `check_permissions` is what the
        # policy layer calls, and a caller reaching `run` another way (a
        # headless context, a tool dispatching to a tool) would otherwise
        # skip twenty denials the repo describes as unrelaxable. `bash` has
        # carried the same second check for the same reason.
        blocked = security_check(" ".join(tool_input.command))
        if blocked is not None and blocked[1] != "ask":
            return ToolResult(
                output=(
                    f"command_run: that command matched security check "
                    f"#{blocked[0]} and is refused. This tool runs a program "
                    f"with one of your accounts in its environment; it is not "
                    f"a way around what the shell refuses. Nothing ran."
                ),
                is_error=True,
                denied_hard=True,
            )
        timeout_s, max_output_bytes, report_chars = _limits()

        # Passed through as given. An earlier version filtered blank entries
        # out of the list itself, which silently ran a different command than
        # the caller asked for: an empty string is a legitimate argument to
        # plenty of programs. The filter belongs in the CHECK below and
        # nowhere else.
        argv = list(tool_input.command)
        if not argv[0].strip():
            # The FIRST entry, not "any entry". Passing the list through
            # unchanged took away the guarantee the old blanket filter gave by
            # accident, and `['', 'python']` would have been handed to the
            # operating system as a program named nothing, after every message
            # here had already been built around `argv[0]`.
            return ToolResult(
                output="command_run: name the program to run. Nothing ran.",
                is_error=True,
            )
        if len(argv) > _MAX_ARGS:
            return ToolResult(
                output=(
                    f"command_run: {len(argv)} arguments is more than the "
                    f"{_MAX_ARGS} this runs. Nothing ran."
                ),
                is_error=True,
            )

        try:
            where = _working_dir(tool_input, context)
        except SealViolation as exc:
            return ToolResult(output=f"command_run: {exc}", is_error=True)

        credential_id = tool_input.credential_id.strip()
        try:
            fields = await asyncio.to_thread(environment_for, credential_id)
        except (
            CredentialNotSet,
            CredentialStoreError,
            NotReadableAsEnvironment,
            UnknownCredentialError,
            VariableAlreadySet,
        ) as exc:
            return ToolResult(output=f"command_run: {exc}", is_error=True)

        # Built here and referenced nowhere else. `os.environ` is copied
        # rather than mutated: this process serves other tools on other
        # threads at the same time, and a value in the runtime's own
        # environment would be inherited by every one of their subprocesses.
        #
        # A field that would replace one of these is refused by the reader,
        # before anything is decrypted, because that rule is about the names
        # the reader chose rather than about launching a process.
        environment = {**os.environ, **fields}

        # Containment is decided at the start, not at the end. Killing on the
        # way out only reaches the one process this runtime holds a handle to,
        # and only on the paths where it is still alive: a script that starts
        # a worker and exits normally leaves that worker holding every field
        # of the account, and the leader's exit code is already set, so
        # anything that reads it first does nothing at all.
        job = containment.open_job()
        process: asyncio.subprocess.Process | None = None
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=where,
                env=environment,
            )
            containment.contain(job, process.pid)
            output, cut, code = await asyncio.wait_for(
                _read_and_wait(process, max_output_bytes), timeout=timeout_s
            )
        except asyncio.TimeoutError:
            return ToolResult(
                output=(
                    f"command_run: {argv[0]} was still running after "
                    f"{int(timeout_s)} seconds and was stopped. Whether it "
                    f"finished what it started is not known from here."
                ),
                is_error=True,
            )
        except (OSError, ValueError) as exc:
            return ToolResult(
                output=(
                    f"command_run: {argv[0]} could not be run: "
                    f"{_without_the_value(exc)}"
                ),
                is_error=True,
            )
        finally:
            # EVERY exit path: it finished, it failed, it ran past the
            # timeout, it printed past the ceiling, the turn was cancelled.
            #
            # The job closes FIRST and closes synchronously, which is what
            # makes this survive the last of those. A second cancellation
            # arriving while an awaited cleanup is running aborts it, and
            # nothing can be awaited uncancellably; a handle close cannot be
            # interrupted, and closing this one kills everything in it.
            #
            # `_reap` stays behind it for the two cases the job does not
            # cover: a machine where no job could be opened, and the
            # microseconds between the process being created and being
            # assigned to one.
            containment.close_job(job)
            if process is not None:
                await _reap(process)

        printed = _reportable(output.decode("utf-8", errors="replace"), report_chars)
        if cut:
            # Said instead of the exit code, not beside it. A command stopped
            # at the ceiling has no exit code of its own worth reporting, and
            # `returncode` can still be None here, which read as "exited None".
            return ToolResult(
                output=(
                    f"{argv[0]} printed more than the {max_output_bytes} bytes "
                    f"this reads and was stopped there. What it did before that "
                    f"is below." + "\n\n" + printed
                ),
                is_error=True,
                metadata={
                    "exit_code": None,
                    "command": argv[0],
                    "credential_id": credential_id,
                    "stopped_at_output_ceiling": True,
                },
            )
        metadata: dict[str, Any] = {
            "exit_code": code,
            "command": argv[0],
            "credential_id": credential_id,
        }
        if code != 0:
            return ToolResult(
                output=f"{argv[0]} exited {code}\n\n{printed}".rstrip(),
                is_error=True,
                metadata=metadata,
            )
        return ToolResult(
            output=printed.rstrip() or f"{argv[0]} finished and printed nothing.",
            metadata=metadata,
        )


async def _read_and_wait(
    process: asyncio.subprocess.Process, ceiling: int
) -> tuple[bytes, bool, int | None]:
    """What it printed up to ``ceiling``, whether it was cut, and its code.

    Read in chunks rather than with ``communicate``, so a command that never
    stops printing cannot take the process down before the timeout does. Cut
    what was KEPT and not only the loop: a chunk is whatever size it arrives
    in, so breaking without this holds output of any size the moment it comes
    in one piece.

    A command cut at the ceiling is killed rather than waited for. Its pipe is
    full and nobody is going to drain it, so waiting is waiting until the
    timeout for a process that can no longer make progress.
    """
    assert process.stdout is not None
    body = bytearray()
    cut = False
    while True:
        chunk = await process.stdout.read(65536)
        if not chunk:
            break
        body.extend(chunk)
        if len(body) >= ceiling:
            del body[ceiling:]
            cut = True
            break
    if cut:
        await _reap(process)
        return bytes(body), True, process.returncode
    return bytes(body), False, await process.wait()


async def _reap(process: asyncio.subprocess.Process) -> None:
    """Stop the command and everything it started, or say why it could not.

    **The tree, not the process.** The command is given every field of an
    account in its environment, and a child it starts inherits that
    environment. Killing only the one this runtime holds a handle to leaves
    those descendants running with the values in them, after the tool has
    reported that it stopped. `supervisor/reap.py` already answers this for
    orphaned daemons and its answer is reused here: `taskkill /F /T` on
    Windows, which walks the tree, and `SIGKILL` elsewhere, which does not,
    and is named as the weaker half rather than presented as equivalent.

    **Never raises, and never waits forever.** This runs in a `finally`, so an
    exception here would replace whatever was actually being reported,
    including a cancellation that has to propagate.
    """
    if process.returncode is not None:
        return
    try:
        if sys.platform == "win32":
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/F",
                "/T",
                "/PID",
                str(process.pid),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(killer.wait(), timeout=_REAP_TIMEOUT_S)
        else:
            process.kill()
    except (OSError, ProcessLookupError, asyncio.TimeoutError):
        # Best effort by construction: the process may already be gone, and
        # `taskkill` may be missing on a stripped image. Logged rather than
        # raised, because the caller is on its way out with something to say.
        logger.warning(
            "command_run: could not stop pid %s and its children",
            process.pid,
            exc_info=True,
        )
    try:
        await asyncio.wait_for(process.wait(), timeout=_REAP_TIMEOUT_S)
    except asyncio.TimeoutError:
        logger.warning("command_run: pid %s did not exit after being stopped", process.pid)


__all__ = ["CommandRunInput", "CommandRunTool"]

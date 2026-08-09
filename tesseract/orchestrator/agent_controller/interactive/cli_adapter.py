from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable

from tesseract.kernel.adapters.cli_utils import (
    claude_subscription_env,
    codex_subscription_env,
    resolve_codex_executable,
    scope_mcp_token,
)

from .stream_parser import ClaudeTurnAccumulator, CodexTurnAccumulator

log = logging.getLogger(__name__)

SpawnFn = Callable[[list[str], str], Awaitable[Any]]


async def _drain_and_wait(proc: Any) -> None:
    """Drain any buffered stdout so proc.wait() can't deadlock on a full
    pipe (Windows), then reap the process."""
    if proc.stdout is not None:
        try:
            while await proc.stdout.read(65536):
                pass
        except (asyncio.LimitOverrunError, ValueError):
            pass
    await proc.wait()


async def _default_spawn(argv: list[str], cwd: str, env: dict[str, str] | None = None) -> Any:
    return await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=cwd,
        env=env,
    )


# CLI lanes are subscription-auth by definition: a `claude`/`codex` lane must
# use the operator's Claude/ChatGPT plan, never an API key. The backend's env
# carries ANTHROPIC_API_KEY / OPENAI_API_KEY (loaded from .env for the SDK
# adapter path), which the CLI would silently prefer over its OAuth login and
# bill against API credit. Strip it — same discipline as delegate_coder /
# delegate_auditor (kernel/adapters/cli_utils.py).
# A lane also gets exactly one hub identity. The scrub belongs HERE and not in
# the shared subscription-env builders: those serve every CLI-backed role, a
# scheduled job and a non-lane delegate, all of which are the runtime acting as
# the operator — narrowing them would demote the assistant's own brain to a lane.
async def _claude_spawn(argv: list[str], cwd: str) -> Any:
    env = scope_mcp_token(claude_subscription_env(), "lane-claude")
    return await _default_spawn(argv, cwd, env=env)


async def _codex_spawn(argv: list[str], cwd: str) -> Any:
    env = scope_mcp_token(codex_subscription_env(), "lane-codex")
    return await _default_spawn(argv, cwd, env=env)


async def _run_turn_loop(
    proc: Any,
    accumulator: Any,
    on_event: Callable[[dict[str, Any]], None],
    cancel_event: asyncio.Event | None,
    turn_timeout: float | None = None,
) -> None:
    """Drive a stream-json subprocess: read newline-delimited JSON from
    proc.stdout, feed each dict event into `accumulator`, fan out to
    `on_event`, stop on accumulator.done / EOF / cancel / deadline.

    When `turn_timeout` is set, the entire turn is bounded: each readline
    is wrapped in asyncio.wait_for against a rolling deadline. On timeout
    the process is killed and the accumulator is marked as an error.
    Returns when the process is reaped. Used by both Claude and Codex adapters."""
    loop = asyncio.get_event_loop()
    deadline: float | None = (loop.time() + turn_timeout) if turn_timeout is not None else None

    def _mark_timeout() -> None:
        accumulator.done = True
        accumulator.is_error = True
        elapsed = turn_timeout or 0
        timeout_msg = f"turn timed out after {elapsed:.0f}s"
        # Both ClaudeTurnAccumulator and CodexTurnAccumulator expose
        # result_text as a read-only property; write to their backing
        # field so the message is visible without adding a setter.
        if hasattr(accumulator, "_result_field"):
            accumulator._result_field = timeout_msg
        elif hasattr(accumulator, "_error_text"):
            accumulator._error_text = timeout_msg

    # A single long-lived waiter on the cancel event, raced against each
    # readline (M2). Checking cancel_event only between lines would let a
    # mid-tool-call turn (silent stdout) ignore an interrupt until the CLI
    # next prints — so an operator "cancel now" could hang arbitrarily.
    cancel_task = (
        asyncio.ensure_future(cancel_event.wait()) if cancel_event is not None else None
    )
    try:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                proc.kill()
                break
            readline_task = asyncio.ensure_future(proc.stdout.readline())
            wait_set: set[Any] = {readline_task}
            if cancel_task is not None:
                wait_set.add(cancel_task)
            timeout: float | None = None
            if deadline is not None:
                timeout = deadline - loop.time()
                if timeout <= 0:
                    readline_task.cancel()
                    proc.kill()
                    _mark_timeout()
                    break
            done, _pending = await asyncio.wait(
                wait_set, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
            if cancel_task is not None and cancel_task in done:
                readline_task.cancel()
                proc.kill()
                break
            if readline_task not in done:
                # Deadline elapsed with no new line.
                readline_task.cancel()
                proc.kill()
                _mark_timeout()
                break
            line = readline_task.result()
            if not line:
                break
            try:
                event = json.loads(line.decode("utf-8", errors="replace"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(event, dict):
                accumulator.feed(event)
                try:
                    on_event(event)
                except Exception:  # noqa: BLE001 — render must not kill the turn
                    log.debug("on_event raised", exc_info=True)
                if accumulator.done:
                    break
        await _drain_and_wait(proc)
    finally:
        if cancel_task is not None and not cancel_task.done():
            cancel_task.cancel()
        if proc.returncode is None:
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass


class ClaudeStreamAdapter:
    """Claude CLI driver: argv builder + stream-json turn runner.

    Flags verified against Claude Code docs: `-p` print mode,
    `--output-format stream-json` (requires `--verbose`),
    `--resume <session_id>` to continue, `--dangerously-skip-permissions`
    for full access (operator-approved for claude/codex).

    No `read_only` counterpart to the codex adapter's, deliberately. The claude
    CLI offers a permission *mode* (`dontAsk` refuses writes) but no sandbox, so
    a read-only claude lane would be enforced a layer above the one codex's
    `--sandbox read-only` is enforced at. The auditor seat runs on codex for
    that reason, and `LaneManager` still refuses a read-only claude lane rather
    than issuing a weaker boundary under the same name.

    `output_schema` turns the turn into a schema-enforced one: the CLI forces a
    `StructuredOutput` tool call and the validated object rides the terminal
    `result` event. It composes with `--output-format stream-json` — verified
    against the installed CLI — so enabling it costs the streaming event feed
    nothing. The schema goes on the command line as an inline JSON string,
    where codex takes a file path instead."""

    binary = "claude"

    def __init__(
        self,
        spawn: SpawnFn | None = None,
        model: str | None = None,
        output_schema: dict[str, Any] | None = None,
    ) -> None:
        self._spawn = spawn or _claude_spawn
        # Without an explicit --model the CLI runs its own account default,
        # silently ignoring the model recorded on the lane binding.
        self.model = model
        self.output_schema = output_schema

    def build_argv(self, *, task: str, session_id: str | None) -> list[str]:
        argv = [
            self.binary, "-p", task,
            "--output-format", "stream-json",
            "--verbose",
            "--dangerously-skip-permissions",
        ]
        if self.output_schema is not None:
            argv += ["--json-schema", json.dumps(self.output_schema)]
        if self.model:
            argv += ["--model", self.model]
        if session_id:
            argv += ["--resume", session_id]
        return argv

    def new_accumulator(self) -> ClaudeTurnAccumulator:
        return ClaudeTurnAccumulator()

    async def run_turn(
        self,
        *,
        task: str,
        session_id: str | None,
        cwd: str,
        on_event: Callable[[dict[str, Any]], None],
        cancel_event: asyncio.Event | None = None,
        turn_timeout: float | None = None,
    ) -> ClaudeTurnAccumulator:
        argv = self.build_argv(task=task, session_id=session_id)
        acc = self.new_accumulator()
        proc = await self._spawn(argv, cwd)
        await _run_turn_loop(proc, acc, on_event, cancel_event, turn_timeout)
        return acc


class CodexStreamAdapter:
    """Codex CLI driver: argv builder + stream-json turn runner.

    `exec <task> --json` for open turns; `exec resume <session_id> <message>
    --json` for resume turns. Two access modes, mutually exclusive:
    `--dangerously-bypass-approvals-and-sandbox` for full access
    (operator-approved, mirrors claude's --dangerously-skip-permissions);
    `--sandbox read-only` for a lane that must never modify the tree.
    `codex exec` has no approval prompt at all, so a refused write fails the
    command rather than blocking the headless turn.

    `output_schema` is a path to a JSON Schema file — codex reads it itself,
    which is why this takes a `Path` where the claude adapter takes a dict it
    serialises inline. The validated object comes back as the final
    agent_message text, not as a field of its own."""

    binary = "codex"

    def __init__(
        self,
        spawn: SpawnFn | None = None,
        model: str | None = None,
        read_only: bool = False,
        output_schema: Path | None = None,
    ) -> None:
        self._spawn = spawn or _codex_spawn
        # Windows: npm installs `codex` as an extensionless script wrapper
        # that asyncio's CreateProcess can't exec — it needs `codex.cmd`.
        # Same resolution delegate_auditor uses (WinError 2 otherwise).
        self.binary = resolve_codex_executable()
        # Without an explicit --model the CLI runs its own config default,
        # silently ignoring the model recorded on the lane binding.
        self.model = model
        # Selects codex's own read-only sandbox instead of full access, so a
        # reviewer lane can inspect the tree but never modify it.
        self.read_only = read_only
        self.output_schema = output_schema

    def build_argv(self, *, task: str, session_id: str | None) -> list[str]:
        if session_id:
            argv = [self.binary, "exec", "resume", session_id, task]
        else:
            argv = [self.binary, "exec", task]
        argv += ["--json", "--skip-git-repo-check"]
        if self.output_schema is not None:
            argv += ["--output-schema", str(self.output_schema)]
        if self.read_only:
            argv += ["--sandbox", "read-only"]
        else:
            argv += ["--dangerously-bypass-approvals-and-sandbox"]
        if self.model:
            argv += ["--model", self.model]
        return argv

    def new_accumulator(self) -> CodexTurnAccumulator:
        return CodexTurnAccumulator()

    async def run_turn(
        self,
        *,
        task: str,
        session_id: str | None,
        cwd: str,
        on_event: Callable[[dict[str, Any]], None],
        cancel_event: asyncio.Event | None = None,
        turn_timeout: float | None = None,
    ) -> CodexTurnAccumulator:
        argv = self.build_argv(task=task, session_id=session_id)
        acc = self.new_accumulator()
        proc = await self._spawn(argv, cwd)
        await _run_turn_loop(proc, acc, on_event, cancel_event, turn_timeout)
        return acc

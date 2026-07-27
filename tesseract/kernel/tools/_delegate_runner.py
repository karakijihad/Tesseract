"""Shared execution kernel for delegate_claude and delegate_codex.

Neither tool class should be imported here — this module knows nothing
about the specific CLI wrappers, only the common orchestration logic.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Callable, Sequence

from tesseract.kernel.tools.base import (
    SpawnCapExceeded,
    ToolContext,
    ToolResult,
    spawn_cap_tool_result,
)
from tesseract.kernel.tools.cli_stream import (
    _strip_control_sequences,
    race_communicate,
    run_subprocess_with_sink,
)

log = logging.getLogger(__name__)

# Timeout-evidence bounds (delegate visibility fix-pass 2026-07-10). Safety
# caps on the best-effort target_paths walk, not tunables — a delegate that
# declares a giant tree still gets bounded snapshot cost.
_SNAPSHOT_MAX_FILES = 5_000
_EVIDENCE_MAX_LISTED = 20


def resolve_cli_model(role_name: str) -> str:
    """Concrete model id for a CLI role's primary (roles.yaml -> providers.yaml).

    The spawned CLI must run the configured model — without an explicit
    ``--model`` flag it silently falls back to its own account default.
    Raises (ConfigError / KeyError) on a missing role or primary: config is
    authoritative, no silent defaults.
    """
    from tesseract.config.loader import load_config

    role = load_config().role(role_name)
    if role.primary is None:
        raise KeyError(f"role {role_name!r} has no primary in roles.yaml")
    return role.primary.model.model


def snapshot_target_state(
    workspace_root: str, target_paths: Sequence[str] | None
) -> dict[str, tuple[int, int]] | None:
    """Best-effort ``{relative_path: (mtime_ns, size)}`` snapshot of the
    declared ``target_paths`` (files directly; directories recursively,
    bounded). Size rides along because filesystem mtime ticks are coarse
    enough (Windows) for a fast rewrite to keep the same timestamp.

    Returns None when there is nothing to snapshot or the walk fails — the
    evidence layer must never break the delegation itself.
    """
    if not target_paths:
        return None
    try:
        root = Path(workspace_root)
        state: dict[str, tuple[int, int]] = {}
        for raw in target_paths:
            target = root / raw
            if target.is_file():
                st = target.stat()
                state[raw] = (st.st_mtime_ns, st.st_size)
                continue
            if not target.is_dir():
                continue
            for child in target.rglob("*"):
                if len(state) >= _SNAPSHOT_MAX_FILES:
                    return state
                if child.is_file():
                    st = child.stat()
                    state[str(child.relative_to(root))] = (st.st_mtime_ns, st.st_size)
        return state
    except OSError:
        log.warning("target_paths snapshot failed for %r", workspace_root, exc_info=True)
        return None


def describe_target_changes(
    before: dict[str, tuple[int, int]] | None,
    workspace_root: str,
    target_paths: Sequence[str] | None,
) -> str:
    """Human-readable diff of the declared target_paths against ``before``.

    Appended to timeout error results so the model can distinguish
    productive-but-slow from dead (the 2026-07-10 incident: a killed
    delegate had written 9 files, TARS saw only "timed out" and redid the
    work). Returns "" when no snapshot was taken; an explicit "nothing
    changed" line when the snapshot exists but matches.
    """
    if before is None:
        return ""
    after = snapshot_target_state(workspace_root, target_paths)
    if after is None:
        return ""
    created = sorted(p for p in after if p not in before)
    modified = sorted(p for p in after if p in before and after[p] != before[p])
    if not created and not modified:
        return (
            "\n\nEvidence: no files under the declared target_paths changed "
            "during the run."
        )
    lines = [f"- {p} (new)" for p in created] + [f"- {p} (modified)" for p in modified]
    shown = lines[:_EVIDENCE_MAX_LISTED]
    if len(lines) > len(shown):
        shown.append(f"…and {len(lines) - len(shown)} more.")
    return (
        f"\n\nEvidence: {len(lines)} file(s) under the declared target_paths "
        "changed while the delegate ran — work WAS happening. Inspect what "
        "landed before re-doing or re-delegating this task:\n" + "\n".join(shown)
    )


async def provision_delegate_mcp(kind: str, workspace_root: str) -> None:
    """Best-effort hub provisioning for a delegate spawn (trio W1).

    Mirrors the lane adapter's call site (`lanes/manager.py`): wires the
    CLI's working dir (claude `.mcp.json`) / global codex config so the
    spawned CLI wakes up hub-connected. Failures are logged, never raised —
    a dead hub connection must not block the delegation itself (same
    contract as the pty_manager terminal call site)."""
    try:
        from tesseract.config.mcp import load_mcp_config
        from tesseract.orchestrator.tars_controller.lanes import mcp_provision

        await asyncio.to_thread(
            mcp_provision.provision,
            Path(workspace_root),
            kind,
            load_mcp_config(),
        )
    except Exception:  # noqa: BLE001 — best-effort, never fail the delegation
        log.warning(
            "delegate mcp_provision(%s) failed for %r", kind, workspace_root,
            exc_info=True,
        )


async def run_delegate_foreground(
    *,
    tool_name: str,
    cli_label: str,
    argv: Sequence[str],
    env: dict,
    inp,
    context: ToolContext,
    cancel_event: asyncio.Event | None,
    output_parser_factory: Callable[[], object] | None = None,
) -> ToolResult:
    """The foreground execution body shared by both delegate tools.

    Parameters
    ----------
    tool_name:
        The tool's ``self.name`` value (``"delegate_claude"`` / ``"delegate_codex"``).
    cli_label:
        Short CLI name used in user-facing messages (``"claude"`` / ``"codex"``).
    argv:
        Full subprocess argv tuple (already resolved for codex executable).
    env:
        Subscription environment dict from the tool's own env builder.
    inp:
        The tool's validated input object (must have ``.task`` and ``.timeout``).
    context:
        The ``ToolContext`` for this call.
    cancel_event:
        Cancel signal, or ``None``.
    output_parser_factory:
        Builds a stream parser (``ClaudeDelegateStreamParser``) per run, or
        ``None`` for CLIs whose stdout is already final text.
    """
    # Snapshot the declared edit targets so a timeout can report what the
    # subprocess actually accomplished before the kill (fix-pass 2026-07-10).
    target_paths = list(getattr(inp, "target_paths", None) or [])
    before = snapshot_target_state(context.workspace_root, target_paths)

    use_streaming = context.cli_sink is not None and context.current_call_id

    if use_streaming:
        result = await run_subprocess_with_sink(
            tool_name=tool_name,
            argv=argv,
            cwd=context.workspace_root,
            timeout=inp.timeout,
            sink=context.cli_sink,
            call_id=context.current_call_id or "",
            empty_message=f"{cli_label} returned empty output",
            missing_message=f"{cli_label} CLI not found — ensure it is installed and on PATH",
            env=env,
            cancel_event=cancel_event,
            output_parser=output_parser_factory() if output_parser_factory else None,
        )
        if result.timed_out:
            evidence = describe_target_changes(before, context.workspace_root, target_paths)
            if evidence:
                result = ToolResult(
                    output=result.output + evidence,
                    is_error=result.is_error,
                    metadata=result.metadata,
                    timed_out=True,
                )
        return result

    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=context.workspace_root,
            env=env,
        )
        result = await race_communicate(process, cancel_event, inp.timeout, tool_name)
    except asyncio.TimeoutError:
        return ToolResult(
            output=(
                f"{tool_name} timed out after {inp.timeout}s"
                + describe_target_changes(before, context.workspace_root, target_paths)
            ),
            is_error=True,
            timed_out=True,
        )
    except FileNotFoundError:
        return ToolResult(
            output=f"{cli_label} CLI not found — ensure it is installed and on PATH",
            is_error=True,
        )
    except OSError as e:
        return ToolResult(output=f"{tool_name} failed to start: {e}", is_error=True)

    if result is None:
        return ToolResult(output=f"{tool_name} cancelled", is_error=True)
    stdout, stderr = result

    out = _strip_control_sequences(stdout.decode("utf-8", errors="replace")).strip()
    turn_error = False
    if output_parser_factory is not None and out:
        # Machine-framed stdout (NDJSON): render + extract the result text.
        parser = output_parser_factory()
        rendered = parser.feed(out + "\n") + parser.flush()
        out = (parser.final_output() or rendered).strip()
        # Exit 0 with a failed turn (error_max_turns etc.) is still an error.
        turn_error = bool(getattr(parser, "is_error", False))
    err = stderr.decode("utf-8", errors="replace").strip()

    if process.returncode != 0:
        combined = f"Exit code: {process.returncode}"
        if out:
            combined += f"\nstdout:\n{out}"
        if err:
            combined += f"\nstderr:\n{err}"
        return ToolResult(output=combined, is_error=True)

    if not out:
        return ToolResult(output=f"{cli_label} returned empty output", is_error=True)

    if turn_error:
        return ToolResult(output=out, is_error=True)

    return ToolResult(output=out, metadata={"tool": tool_name, "exit_code": 0})


async def run_delegate(
    *,
    tool_name: str,
    cli_label: str,
    provider: str,
    build_argv: Callable[[], Sequence[str]],
    env: dict,
    tool_input,
    context: ToolContext,
    output_parser_factory: Callable[[], object] | None = None,
) -> ToolResult:
    """Full ``run()`` orchestration shared by both delegate tools.

    Parameters
    ----------
    tool_name:
        The tool's ``self.name`` value.
    cli_label:
        Short CLI name for user-facing messages.
    provider:
        Provider string for ``_cli_disabled_reason`` (``"claude"`` / ``"codex"``).
    build_argv:
        Zero-argument callable that returns the full argv for the subprocess.
        Called after all guard checks pass so that e.g. ``resolve_codex_executable``
        is only invoked when we actually intend to spawn.
    env:
        Subscription environment dict.
    tool_input:
        The validated input object (must have ``.task``, ``.timeout``,
        ``.target_paths``, ``.background``).
    context:
        The ``ToolContext`` for this call.
    """
    from tesseract.kernel.tools._terminal_handoff_guard import (
        HANDOFF_REDIRECT_MESSAGE,
        requires_terminal,
    )
    from tesseract.kernel.tools.delegate_claude import _cli_disabled_reason

    if requires_terminal(getattr(tool_input, "target_paths", None)):
        return ToolResult(
            output=HANDOFF_REDIRECT_MESSAGE,
            is_error=True,
            metadata={"reason": "requires_terminal_handoff"},
        )

    reason = _cli_disabled_reason(provider)
    if reason:
        return ToolResult(output=f"{tool_name} unavailable: {reason}", is_error=True)

    try:
        argv = build_argv()
    except Exception as exc:  # noqa: BLE001 — config resolution is authoritative
        return ToolResult(
            output=f"{tool_name} unavailable: argv/model resolution failed: {exc}",
            is_error=True,
        )
    await provision_delegate_mcp(provider, context.workspace_root)
    # mission-era gate removed 2026-07-13; delegates intentionally never bind
    # the turn cancel_event — background spawns outlive their launching turn
    # (behavior unchanged).
    cancel_event = None

    # parallel-tars P3: a context without a SpawnRegistry (headless /
    # REPL / autonomy) degrades to foreground instead of erroring so the
    # background default is safe everywhere.
    registry = getattr(context, "spawns", None)

    # Foreground hard cap (fix-pass 2026-07-10): a blocking delegate wedges
    # the whole chat turn — queued operator messages can't drain until it
    # returns. Long foreground requests are auto-flipped to background when a
    # registry exists rather than trusting the chat brain's judgment.
    background = tool_input.background
    flip_note = ""
    if not background and registry is not None:
        try:
            from tesseract.config.runtime_limits import (
                default_runtime_config_path,
                load_max_foreground_delegate_timeout_s,
            )

            max_foreground_s = load_max_foreground_delegate_timeout_s(
                default_runtime_config_path()
            )
        except Exception as e:  # noqa: BLE001 — raise-loudly, surfaced to the model
            return ToolResult(
                output=f"{tool_name} config error: {e}",
                is_error=True,
            )
        if tool_input.timeout > max_foreground_s:
            background = True
            flip_note = (
                f"NOTE: foreground blocking is capped at {max_foreground_s:.0f}s "
                f"(runtime.yaml::max_foreground_delegate_timeout_s); your "
                f"timeout of {tool_input.timeout:.0f}s exceeds it, so this "
                f"delegate was auto-flipped to a background spawn. "
            )

    if background and registry is not None:
        try:
            handle = registry.register(
                kind=tool_name,
                goal=getattr(tool_input, "task", None),
                coro=run_delegate_foreground(
                    tool_name=tool_name,
                    cli_label=cli_label,
                    argv=argv,
                    env=env,
                    inp=tool_input,
                    context=context,
                    cancel_event=cancel_event,
                    output_parser_factory=output_parser_factory,
                ),
            )
        except SpawnCapExceeded as exc:
            return spawn_cap_tool_result(exc)
        return ToolResult(
            output=(
                f"{flip_note}{tool_name} spawned in background: handle="
                f"{handle.handle_id}. Use spawn_check or spawn_await "
                f"to retrieve the result."
            ),
            metadata={
                "spawn_handle": handle.handle_id,
                "spawn_kind": tool_name,
                "started_at": handle.started_at,
                "status": "running",
            },
        )

    return await run_delegate_foreground(
        tool_name=tool_name,
        cli_label=cli_label,
        argv=argv,
        env=env,
        inp=tool_input,
        context=context,
        cancel_event=cancel_event,
        output_parser_factory=output_parser_factory,
    )

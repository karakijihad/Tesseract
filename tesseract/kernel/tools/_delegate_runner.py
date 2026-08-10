"""Shared execution kernel for delegate_coder and delegate_auditor.

Neither tool class should be imported here — this module knows nothing
about the specific CLI wrappers, only the common orchestration logic.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Sequence

from tesseract.kernel.tools.base import (
    SpawnCapExceeded,
    ToolContext,
    ToolResult,
    spawn_cap_tool_result,
)

log = logging.getLogger(__name__)


def _cli_disabled_reason(provider: str, tier: str = "cli") -> str | None:
    """Short reason string when the ``cli`` tier or this provider is switched
    off in providers.yaml; otherwise None. Best-effort — a config-load error
    doesn't block the delegate (it surfaces elsewhere). Thin wrapper around
    ``ConfigBundle.is_provider_enabled`` so the tier+provider check has one
    implementation."""
    try:
        from tesseract.config.loader import load_config

        ok, reason = load_config().is_provider_enabled(tier, provider)
        return None if ok else reason
    except Exception:  # noqa: BLE001
        return None

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
    return _seat_role_primary(role_name).model.model


# Which lane transport drives each tier. A seat may name any ref whose tier
# appears here; anything else has no way to run a lane turn.
_TIER_LANE_KIND = {"cli": None, "api": "api"}
"""``None`` means "the lane kind is the provider name" — the CLI adapters are
selected per-vendor (`claude` / `codex`), while every api-tier ref shares one
tool-less adapter and so shares one lane kind."""


def _seat_role_primary(role_name: str):
    """The ``ResolvedRef`` behind a delegation seat, or raise.

    A seat may be wired to a `cli.*` ref (a CLI subprocess lane) or an `api.*`
    ref (a tool-less model lane). Both have a lane adapter, so both can run a
    delegation, and moving a seat between them is a yaml edit.

    Any other tier is refused HERE rather than left to fail downstream: a
    delegation runs as a lane turn, and a lane's ``kind`` selects a transport.
    A ref whose tier has no adapter would surface as an unintelligible
    transport error instead of a config one."""
    from tesseract.config.loader import load_config

    role = load_config().role(role_name)
    if role.primary is None:
        raise KeyError(f"role {role_name!r} has no primary in roles.yaml")
    tier = role.primary.connection.tier
    if tier not in _TIER_LANE_KIND:
        raise ValueError(
            f"role {role_name!r} is wired to {role.primary.ref!r} (tier {tier!r}); "
            f"a delegation seat needs one of {sorted(_TIER_LANE_KIND)} — a "
            "delegation runs on a lane, and no other tier has a lane adapter"
        )
    return role.primary



SEAT_TOOLS: dict[str, str] = {
    "coder": "delegate_coder",
    "auditor": "delegate_auditor",
}
"""Delegation seat → the tool that runs it.

Listed rather than derived from the ``delegate_`` prefix because that prefix
also covers tools that are not seats (`delegate_codex_exec`,
`delegate_agent_controller`), and a prefix match would silently constrain them
too. A third seat is one entry plus its tool; nothing here names a vendor —
who fills a seat is `roles.yaml`.
"""


def delegation_seats() -> list[str]:
    """Every seat a session-level constraint may name."""
    return sorted(SEAT_TOOLS)


def borrowable_providers() -> list[str] | None:
    """Every provider a ``provider`` override may name — the ``<name>_cli``
    roles present in roles.yaml. ``None`` when roles.yaml could not be read.

    Derived rather than listed so wiring a third CLI makes it borrowable
    without a code change. The seat indirection put the choice of worker in
    config; a frozen pair on the tool input would have kept one copy of it in
    kernel source. Read at call time, like every other consumer of roles.yaml:
    the config watcher rebuilds without a restart, and a cached list would
    refuse a CLI the operator had just added.

    ``None`` rather than ``[]`` because the two are different answers to the
    operator: "nothing is borrowable" is a routing fact, "the roster could not
    be read" is a config failure, and reporting the second as the first sends
    them looking in the wrong file.
    """
    from tesseract.config.loader import load_config

    try:
        names = load_config().roles.keys()
    except Exception:  # noqa: BLE001 — reported as unknown, not as empty
        return None
    suffix = "_cli"
    return sorted(n[: -len(suffix)] for n in names if n.endswith(suffix))


@dataclass(frozen=True)
class DelegateSeat:
    """What a seat resolved to.

    ``kind`` is the lane transport and ``provider`` is who is billed/probed —
    they coincide for the CLIs (a `claude` lane runs the claude CLI) and
    diverge for api refs, where every provider shares the one tool-less
    adapter. Keeping them separate is what lets `api.openai.*` and
    `api.xai.*` be different providers on the same transport."""

    kind: str
    model: str
    provider: str
    tier: str


def resolve_delegate_seat(
    seat: str, provider_override: str | None = None
) -> DelegateSeat:
    """The transport, model and provider for a delegation seat.

    The seat's role in roles.yaml names the CLI that fills it by default, and
    the provider is READ OFF that ref rather than passed in — which is what
    makes re-seating a provider a yaml edit instead of a code change. Point
    ``auditor`` at a claude ref and claude reviews, with nothing here to
    change.

    An override borrows the other CLI for one call. Its model comes from that
    CLI's own ``<provider>_cli`` role, because a seat's ref cannot speak for a
    provider it does not name — reading the model off the seat while swapping
    the transport would run codex against a claude model id.
    """
    seat_ref = _seat_role_primary(seat)
    if provider_override is None or provider_override == seat_ref.connection.name:
        return _seat_from_ref(seat_ref)

    # A borrow names a CLI, so it resolves through that CLI's own role. There
    # is no `<provider>_api` convention to borrow with — an api seat is chosen
    # by wiring the seat, not per call.
    from tesseract.config.loader import ConfigError

    # Only the ABSENT-role case is rephrased. `_seat_role_primary` also raises
    # ValueError for a role wired to a tier with no lane adapter, and that
    # error already names the real problem — swallowing it into "no such role"
    # would send the operator looking for a role that is right there.
    try:
        override_ref = _seat_role_primary(f"{provider_override}_cli")
    except (ConfigError, KeyError) as exc:
        borrowable = borrowable_providers()
        if borrowable is None:
            known = "unknown (roles.yaml could not be read)"
        else:
            known = ", ".join(borrowable) if borrowable else "none"
        raise ValueError(
            f"cannot borrow provider {provider_override!r}: no "
            f"{provider_override}_cli role in roles.yaml. Borrowable: {known}"
        ) from exc
    if override_ref.connection.name != provider_override:
        raise ValueError(
            f"role {provider_override}_cli is wired to provider "
            f"{override_ref.connection.name!r}, not {provider_override!r}"
        )
    return _seat_from_ref(override_ref)


def _seat_from_ref(ref) -> DelegateSeat:
    tier = ref.connection.tier
    provider = ref.connection.name
    lane_kind = _TIER_LANE_KIND[tier] or provider
    # An api lane resolves its own connection from the ref, so it carries the
    # whole ref where a CLI lane carries a bare model id — the CLI gets the id
    # as a `--model` flag, and a ref there would be meaningless.
    model = ref.ref if tier == "api" else ref.model.model
    return DelegateSeat(kind=lane_kind, model=model, provider=provider, tier=tier)


def snapshot_target_state(
    anchor: str, target_paths: Sequence[str] | None
) -> dict[str, tuple[int, int]] | None:
    """Best-effort ``{relative_path: (mtime_ns, size)}`` snapshot of the
    declared ``target_paths`` (files directly; directories recursively,
    bounded). Size rides along because filesystem mtime ticks are coarse
    enough (Windows) for a fast rewrite to keep the same timestamp.

    ``anchor`` is the directory relative declarations resolve against, which
    must be the one the delegate is actually running in — not necessarily
    ``context.workspace_root``, since ``safe_cwd`` relocates a sealed run.

    Returns None when there is nothing to snapshot or the walk fails — the
    evidence layer must never break the delegation itself.
    """
    if not target_paths:
        return None
    try:
        root = Path(anchor)
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
                    state[_label(child, root)] = (st.st_mtime_ns, st.st_size)
        return state
    except OSError:
        log.warning("target_paths snapshot failed for %r", anchor, exc_info=True)
        return None


def _label(child: Path, root: Path) -> str:
    """`child` named relative to `root`, falling back to its full path.

    An absolute `target_paths` entry replaces `root` entirely on join, so its
    children need not sit under `root` at all — and `relative_to` raises
    `ValueError` rather than `OSError` when they don't, which would escape the
    walk's handler and take the whole delegation down with it. The evidence
    layer is documented never to do that.
    """
    try:
        return str(child.relative_to(root))
    except ValueError:
        return str(child)


def describe_target_changes(
    before: dict[str, tuple[int, int]] | None,
    anchor: str,
    target_paths: Sequence[str] | None,
) -> str:
    """Human-readable diff of the declared target_paths against ``before``.

    ``anchor`` must be the same one ``before`` was taken with — the keys are
    paths relative to it, and comparing two differently-anchored snapshots
    would report every file as new.

    Appended to timeout error results so the model can distinguish
    productive-but-slow from dead (the 2026-07-10 incident: a killed
    delegate had written 9 files, the assistant saw only "timed out" and redid the
    work). Returns "" when no snapshot was taken; an explicit "nothing
    changed" line when the snapshot exists but matches.
    """
    if before is None:
        return ""
    after = snapshot_target_state(anchor, target_paths)
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
    """Best-effort hub provisioning for a delegate spawn.

    Mirrors the lane adapter's call site (`lanes/manager.py`): wires the
    operator's global claude/codex config so the spawned CLI wakes up
    hub-connected. Failures are logged, never raised —
    a dead hub connection must not block the delegation itself (same
    contract as the pty_manager terminal call site)."""
    try:
        from tesseract.config.mcp import load_mcp_config
        from tesseract.orchestrator.agent_controller.lanes import mcp_provision

        await asyncio.to_thread(
            lambda: mcp_provision.provision(
                kind,
                load_mcp_config(),
                # Where the project-scope scheme used to write, so where a
                # stale entry can still shadow the user-scope one.
                cleanup_dirs=[Path(workspace_root)],
            )
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
    provider: str,
    model: str,
    working_dir: str,
    inp,
    context: ToolContext,
    lane_ref: dict | None = None,
) -> ToolResult:
    """The execution body shared by both delegate tools: one turn on one
    ephemeral lane, plus the edit-evidence layer around it.

    Parameters
    ----------
    tool_name:
        The tool's ``self.name`` value (``"delegate_coder"`` / ``"delegate_auditor"``).
    cli_label:
        Short CLI name used in user-facing messages (``"claude"`` / ``"codex"``).
    provider:
        Lane kind — ``"claude"`` / ``"codex"``.
    model:
        Concrete model id resolved from the CLI's role.
    working_dir:
        Already relocated out of the sealed tree by ``safe_cwd``.
    inp:
        The tool's validated input object (must have ``.task`` and ``.timeout``).
    context:
        The ``ToolContext`` for this call.
    """
    from tesseract.kernel.tools._lane_delegate import (
        LaneDelegationUnavailable,
        run_delegation_on_lane,
    )

    # Snapshot the declared edit targets so a stall can report what the
    # delegate actually accomplished before it went quiet (fix-pass
    # 2026-07-10: a killed delegate had written 9 files and the assistant saw only
    # "timed out", so it redid the work).
    #
    # Anchored on `working_dir`, not `context.workspace_root`: the two differ
    # exactly when `safe_cwd` relocated the run out of the sealed tree, which
    # on a packaged install is every time. Anchoring on the workspace root
    # then looked for `app/<declared path>` while the delegate was writing
    # under `home/workshop/` — so the evidence came back empty for precisely
    # the declarations that are normal there.
    target_paths = list(getattr(inp, "target_paths", None) or [])
    before = snapshot_target_state(working_dir, target_paths)

    try:
        result = await run_delegation_on_lane(
            tool_name=tool_name,
            cli_label=cli_label,
            kind=provider,
            model=model,
            task=inp.task,
            timeout_s=inp.timeout,
            working_dir=working_dir,
            context=context,
            lane_ref=lane_ref,
        )
    except LaneDelegationUnavailable as exc:
        # Provider rides even the failure paths: an auth-shaped message here
        # is exactly the case cli-auth invalidation exists for, and it reads
        # the provider off the result.
        return ToolResult(
            output=f"{tool_name} unavailable: {exc}",
            is_error=True,
            metadata={"provider": provider},
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — surfaced as a tool error
        return ToolResult(
            output=f"{tool_name} failed: {exc}",
            is_error=True,
            metadata={"provider": provider},
        )

    if result.timed_out:
        evidence = describe_target_changes(before, working_dir, target_paths)
        if evidence:
            result = ToolResult(
                output=result.output + evidence,
                is_error=result.is_error,
                metadata=result.metadata,
                timed_out=True,
            )
    # Which CLI actually ran, so an auth-shaped failure invalidates the right
    # provider's cached subscription state. A seat no longer implies one — the
    # caller may have borrowed the other CLI for this call.
    return replace(result, metadata={**(result.metadata or {}), "provider": provider})


async def run_delegate(
    *,
    tool_name: str,
    seat: str,
    tool_input,
    context: ToolContext,
) -> ToolResult:
    """Full ``run()`` orchestration shared by both delegate tools.

    The work itself runs on an ephemeral lane (``_lane_delegate``), so a
    delegation has the same identity, event stream, interrupt and wait
    primitive as any other lane turn. What stays here is everything that is
    about the *decision* to delegate rather than its transport: the terminal
    handoff guard, the provider-disabled check, the background/foreground
    call, and the edit-evidence snapshot that lets a stalled delegate report
    what it had already accomplished.

    Parameters
    ----------
    tool_name:
        The tool's ``self.name`` value.
    seat:
        The delegation seat this tool fills (``"coder"`` / ``"auditor"``). Its
        roles.yaml entry decides both the provider and the model; a
        ``provider`` field on ``tool_input`` borrows the other CLI for this
        one call.
    tool_input:
        The validated input object (must have ``.task``, ``.timeout``,
        ``.target_paths``, ``.background``; ``.provider`` optional).
    context:
        The ``ToolContext`` for this call.
    """
    from tesseract.kernel.tools._terminal_handoff_guard import (
        HANDOFF_REDIRECT_MESSAGE,
        requires_terminal,
    )

    if requires_terminal(getattr(tool_input, "target_paths", None)):
        return ToolResult(
            output=HANDOFF_REDIRECT_MESSAGE,
            is_error=True,
            metadata={"reason": "requires_terminal_handoff"},
        )

    # Resolution comes first now: the provider is read off the seat rather
    # than passed in, so there is nothing to check availability for until the
    # seat has named one.
    try:
        target = resolve_delegate_seat(seat, getattr(tool_input, "provider", None))
    except Exception as exc:  # noqa: BLE001 — config resolution is authoritative
        return ToolResult(
            output=f"{tool_name} unavailable: seat resolution failed: {exc}",
            is_error=True,
        )
    provider, model = target.provider, target.model
    cli_label = provider

    reason = _cli_disabled_reason(provider, tier=target.tier)
    if reason:
        return ToolResult(output=f"{tool_name} unavailable: {reason}", is_error=True)
    # `workspace_root` is the CODE tree, which in a packaged install IS the
    # sealed `app/`. Left alone, every delegation would start a CLI inside the
    # tree the next update overwrites. See `seal_guard.safe_cwd` for why this
    # relocates rather than refuses.
    from tesseract.orchestrator.seal_guard import safe_cwd

    working_dir = str(safe_cwd(context.workspace_root))
    # MCP provisioning moved with the transport: the lane adapter wires the
    # hub before its own first turn (`lanes/manager.py`), so doing it here
    # would be the same call twice.

    # a context without a SpawnRegistry (headless /
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
        from tesseract.kernel.tools._lane_delegate import make_delegate_cancel

        # The delegation's lane is opened inside the spawned coroutine, so the
        # cancel_fn is handed a box the runner fills in. Without one, cancelling
        # a background delegate stopped only the waiting task and left the CLI
        # running on a lane nobody was watching.
        lane_ref: dict[str, object] = {}
        try:
            handle = registry.register(
                kind=tool_name,
                goal=getattr(tool_input, "task", None),
                coro=run_delegate_foreground(
                    tool_name=tool_name,
                    cli_label=cli_label,
                    provider=target.kind,
                    model=model,
                    working_dir=working_dir,
                    inp=tool_input,
                    context=context,
                    lane_ref=lane_ref,
                ),
                cancel_fn=make_delegate_cancel(lane_ref),
                # The seat is named for the job, so the activity registry
                # cannot infer the worker from the kind — tell it.
                provider=provider,
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
                "provider": provider,
            },
        )

    return await run_delegate_foreground(
        tool_name=tool_name,
        cli_label=cli_label,
        provider=target.kind,
        model=model,
        working_dir=working_dir,
        inp=tool_input,
        context=context,
    )

"""Single permission decision pipeline.

The live decision flow consulted on every tool call. Three layers, in
order:
  1. Tool's own `check_permissions` — the security layer. Returns DENY
     for hardcoded blocks (injection, destructive verbs). Otherwise
     PASSTHROUGH or ASK.
  2. Path validation for file tools, under two boundaries: writes
     (`_WRITE_PATH_TOOLS`) are bounded at `home_dir()`, reads
     (`_READ_PATH_TOOLS`) at `install_root()`. The 12-vector
     `validate_path()` blocks null bytes, UNC, tilde, double-encoded
     traversal, and boundary escapes before policy is consulted. The seal
     on `app/` is the absence of write authority, not a special case.
  3. Operator policy (`permissions.yaml`) — the AUTO/ASK/DENY decision
     surface. Path overrides normalize absolute paths against
     `workspace_root` so kernel lockdown rules can't be bypassed by
     passing an absolute path.

ASK posture is honoured by `ask_fn` when wired (REPL/Mirror operator
prompt). When no `ask_fn` is wired, write/destructive tools are denied
by default — silent auto-allow would defeat the ASK contract — while
read-only tools fall through to allow because they are inert from the
operator's perspective. One narrow exception (Stage 10): a tool whose
class declares `headless_quarantine_write = True` proceeds unattended,
because its only write target is a quarantine the runtime never
executes from; the operator gate for such tools sits at activation
(`agent_promote` / Workspace proposal card), not at the write.

Returns either a `ToolResult` denial/declination (do NOT run the tool)
or `None` (proceed to `tool.run`). Single source of truth for tool
permission decisions; the previous parallel `PermissionEngine` is
deleted.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Awaitable, Callable

from tesseract.kernel.tools.base import (
    PermissionResult,
    Tool,
    ToolContext,
    ToolResult,
)
from tesseract.paths import home_dir, install_root, secret_path_component
from tesseract.permissions import approval_log
from tesseract.permissions.path_validator import validate_path
from tesseract.permissions.policy import WRITE_PATH_FIELDS, PermissionPolicy

AskFn = Callable[[Tool, Any, ToolContext], Awaitable[bool]]

# Tools whose path arguments are sent through `validate_path` before the tool
# runs, mapped to the raw-input keys to validate. file_copy validates only its
# destination here (its source is a read, and appears in `_READ_PATH_TOOLS`
# below); file_move validates both ends, because removing the source is a
# write.
#
# The map itself lives in `policy.py`, because the posture resolver needs the
# same answer and knew only `file_path`/`path` while this list sat here. An
# override written for the transfer verbs therefore matched nothing and still
# read as closed, which is how a copy onto the soul resolved AUTO.
_WRITE_PATH_TOOLS = WRITE_PATH_FIELDS

# Read-side path tools. Until this existed, `validate_path` ran only against
# the write tools, so `file_read` with an absolute path reached any file on
# the machine. Reads are bounded at the install root, not at `home/`: a sealed
# `app/` should still be legible to the agent running inside it.
#
# `file_copy`'s source belongs here rather than nowhere — leaving it unbounded
# would let a copy pull any file on the machine into `home/`, where reading it
# is allowed, which defeats the read boundary in one hop. `memory_get` is
# deliberately absent: its input is memory-store-relative and it enforces a
# stricter root of its own.
#
# The `channel_send_*` media tools belong here for a sharper reason than the
# rest: each reads a local path off disk and forwards the bytes to an external
# chat, at `auto` posture. Unbounded, that is a read of any file on the machine
# followed by an unattended exfiltration of it. The path field is not spelled
# the same across them (`audio_path`, `sticker_path`, `source_path`) — every
# media tool that takes a local path belongs in this map, whatever it calls it.
_READ_PATH_TOOLS: dict[str, tuple[str, ...]] = {
    "file_read": ("file_path", "path"),
    "pdf_read": ("file_path",),
    "glob": ("path",),
    "grep": ("path",),
    "log_triage": ("path",),
    "file_copy": ("source_path",),
    "vault_ingest": ("source_path",),
    "channel_send_photo": ("source_path",),
    "channel_send_document": ("source_path",),
    "channel_send_video": ("source_path",),
    "channel_send_animation": ("source_path",),
    "channel_send_video_note": ("source_path",),
    "channel_send_voice": ("audio_path",),
    "channel_send_sticker": ("sticker_path",),
}

logger = logging.getLogger(__name__)

#: The two shapes a gated call comes back to the model in. Named here because
#: `workspace/OPERATING.md` teaches the model to tell them apart, and that
#: paragraph is generated from these two constants rather than transcribed —
#: a reworded refusal would otherwise leave the document describing a
#: sentence the runtime no longer sends.
DENIED_PREFIX = "permission denied"
#: NOT "declined". `ask_fn` returns one bool for two different events and the
#: ledger keeps them apart (`result: deny` vs `result: timeout, actor:
#: timeout`), so asserting a decline contradicts the runtime's own record
#: whenever the prompt simply expired. Live case, 2026-08-09T15:13:58: the
#: operator approved a heredoc into `python -`, the ask had already timed out,
#: and this line told them they had declined it. They then reported an approval
#: that failed to round-trip; the approval path was fine, this sentence was
#: not. Name both possibilities rather than guess wrong.
#: Why the call did not run, as one clause. Named separately because
#: `OPERATING.md`'s generated region quotes THIS and nothing else — reading it
#: out of the full sentence meant splitting on an em-dash and a full stop, so
#: adding either to the wording below would have silently truncated the
#: document the model reads every turn.
NOT_APPROVED_CAUSE = (
    "the operator declined it, or the approval prompt expired before it was "
    "answered"
)
NOT_APPROVED_TEMPLATE = (
    "{tool} was not approved — " + NOT_APPROVED_CAUSE + ". Say what you "
    "intended, and offer to retry it or take a different approach."
)



def _gated_path_anchors() -> tuple[str, ...]:
    """Every root a gated tool resolves a relative path against.

    Not a guess and not a mode: the actual, enumerated set, because the tools
    disagree with each other and each disagreement has cost a finding.

    - ``home_dir()``   — `file_copy`/`file_move` via
      `file_write.py::_resolve_for_check`, and `file_write` itself.
    - ``install_root()`` — the read boundary `validate_path` joins a relative
      read path onto.
    - ``Path.cwd()``   — `vault_ingest` opens a bare ``Path(source_path)``
      (`kernel/tools/vault_ingest.py`), and the `channel_send_*` media tools do
      the same, so for those the process CWD IS the anchor.

    Resolved at call time. The CWD can move, and a set captured at import would
    be the stale-list problem this function exists to replace.

    Every root is resolved defensively and a root that cannot be resolved is
    DROPPED rather than raised. `Path.cwd()` raises when the directory the
    process was launched from has been deleted underneath it, which is a real
    state, and this is called inside the permission gate: an exception here is
    a tool call that got neither an allow nor a deny and left no row in the
    approval log. Losing one anchor narrows the check to the rest; losing the
    gate refuses nothing at all.
    """
    anchors: list[str] = []
    for root in (home_dir, install_root, Path.cwd):
        try:
            anchors.append(str(root()))
        except (OSError, RuntimeError, ValueError):
            logger.warning("secret-path anchor unavailable: %s", root.__name__)
    return tuple(anchors)


def _secret_component(raw_path: str, anchors: tuple[str, ...]) -> str | None:
    """The credential-bearing component of `raw_path`, under EVERY root a tool
    might resolve it against.

    This mechanism has been wrong four times, and every one was the same
    mistake: the check looked at a different file from the one the tool would
    open, because it committed to ONE anchor.

    1. It resolved against the process CWD alone. `file_copy`/`file_move`
       anchor at `home_dir()`, so an ordinary-named link in the state root was
       invisible here and followed there.
    2. It was anchored per MODE. `file_copy` carries its source in the READ
       map, whose root is `install_root()`, while the transfer resolves at
       `home_dir()` — parent and child, still disagreeing.
    3. It took `home_dir()` and `install_root()` and dropped the CWD, which is
       exactly what `vault_ingest` and the media tools use.
    4. A per-tool anchor map would be a third list beside the two path maps,
       and both of those have gone stale inside one phase.

    So it takes them ALL, from `_gated_path_anchors`. A false positive costs a
    refusal on a path that is credential-named under one of the runtime's own
    roots, which should be refused anyway; a false negative hands out the
    store. The trade is not close, and this is deliberately over-inclusive.

    Every call into `pathlib` is guarded, construction included. This runs
    INSIDE the permission gate, and an exception escaping it is a tool call
    that got neither an allow nor a deny and left no row in the approval log.
    """
    builders = [lambda: Path(raw_path)]
    for anchor in anchors:
        builders.append(lambda a=anchor: Path(a) / raw_path)
        builders.append(lambda a=anchor: (Path(a) / raw_path).resolve())
    for build in builders:
        try:
            offender = secret_path_component(build())
        except (OSError, RuntimeError, ValueError):
            continue
        if offender is not None:
            return offender
    return None


async def evaluate(
    tool: Tool,
    validated: Any,
    raw_input: dict[str, Any],
    context: ToolContext,
    ask_fn: AskFn | None,
    policy: PermissionPolicy | None,
) -> ToolResult | None:
    """Run the live permission pipeline for a single tool call.

    Returns a `ToolResult` denial when the call must NOT proceed (security
    DENY, path-validation failure, policy DENY, ASK without an approval
    channel for non-read-only tools, or operator decline). Returns `None`
    when the caller should run `await tool.run(validated, context)`.
    """
    # Redacted BEFORE it is summarised, because summarising only truncates.
    # `approvals.jsonl` records every ASK call whether the operator allows it
    # or refuses it, and it is rolled to an archive rather than deleted, so a
    # password typed into a page lived there for the life of the install. The
    # tool says which of its own fields must not be kept
    # (`Tool.redacted_input_fields`); a tool that declares none is unchanged.
    summary = approval_log.redacted_summary(tool, raw_input)

    decision = tool.check_permissions(validated, context)
    if decision == PermissionResult.DENY:
        await approval_log.record_ask(
            session_id=context.session_id,
            call_id=context.current_call_id,
            tool_name=tool.name,
            input_summary=summary,
            posture_source="security",
            result="deny",
            actor="system",
        )
        hint = getattr(tool, "security_deny_hint", "")
        return ToolResult(
            output=f"{DENIED_PREFIX}: {tool.name}" + (f" — {hint}" if hint else ""),
            is_error=True,
            denied_hard=True,
            deny_reason="security layer (tool.check_permissions DENY)",
        )

    write_root = str(home_dir())
    read_root = str(install_root())
    checks = (
        (_WRITE_PATH_TOOLS.get(tool.name, ()), "write"),
        (_READ_PATH_TOOLS.get(tool.name, ()), "read"),
    )
    for path_keys, mode in checks:
        for path_key in path_keys:
            raw_path = raw_input.get(path_key) or ""
            if not raw_path:
                continue
            # Credential-bearing names, refused before anything else looks
            # at the path. Five properties, and they are written down together
            # because this mechanism has now been found wanting twice and each
            # time the fix satisfied the property in front of it:
            #
            #   1. EVERY path field of EVERY gated tool is checked, not the
            #      read-mode ones. `file_move` carries its source in the WRITE
            #      map (it moves a file, so containment is judged against the
            #      write root), and a mode-conditional check read straight past
            #      it while refusing `file_copy` beside it.
            #   2. Seen the way the TOOL will see it, under EVERY root any
            #      gated tool resolves against (`_gated_path_anchors`). Four
            #      findings landed here, each because the check committed to
            #      one anchor and some tool used another: the CWD alone, then
            #      per-mode, then home and install without the CWD that
            #      `vault_ingest` and the media tools actually use. Over-
            #      inclusive on purpose: a false positive refuses a
            #      credential-named path under one of the runtime's own roots.
            #   3. Component-wise, never leaf-only, because a directory is as
            #      good a hiding place as a name.
            #   4. One implementation, shared with the read tools
            #      (`paths.py::secret_path_component`), so the gate and
            #      `_path_anchor.py` cannot come to refuse different sets.
            #   5. Fails closed and never raises out of the gate. BOTH calls
            #      are guarded, and ValueError is in the guard: a path with an
            #      embedded null raises ValueError, not OSError, and a guard
            #      that named only OSError and RuntimeError let it escape
            #      `evaluate` as an unhandled exception with no posture
            #      decision recorded. The property was written here before it
            #      was true, which is its own lesson.
            #
            # Checking WRITE fields too is deliberate, not a side effect of
            # covering `file_move`. A file the assistant creates under a
            # credential-bearing name is a file the read tools can never open
            # again, which is the write-then-cannot-read asymmetry
            # `READABLE_STATE_PREFIXES` exists to prevent; refusing the write
            # is the coherent half of refusing the read.
            #
            # What it is NOT is a boundary against code running as the same OS
            # user, and no comment here may say otherwise.
            offender = _secret_component(str(raw_path), _gated_path_anchors())
            if offender is not None:
                logger.warning(
                    "secret path refused for %s: %s", tool.name, offender
                )
                await approval_log.record_ask(
                    session_id=context.session_id,
                    call_id=context.current_call_id,
                    tool_name=tool.name,
                    input_summary=summary,
                    posture_source="secret_path",
                    result="deny",
                    actor="system",
                )
                return ToolResult(
                    output=(
                        f"{DENIED_PREFIX}: {tool.name} may not "
                        + (
                            f"create or overwrite {offender!r}, which is a "
                            f"credential name. A file written under it could "
                            f"never be read back."
                            if mode == "write"
                            else f"read {offender!r}, which holds credentials. "
                            f"Key PRESENCE is reported by Settings; key VALUES "
                            f"are not readable by design, and copying, moving, "
                            f"ingesting or attaching the file is the same read."
                        )
                    ),
                    is_error=True,
                    denied_hard=True,
                    deny_reason=f"secret_path: {offender}",
                )
            valid, reason = validate_path(
                str(raw_path), write_root=write_root, read_root=read_root, mode=mode
            )
            if not valid:
                logger.warning("path validation rejected %s: %s", tool.name, reason)
                await approval_log.record_ask(
                    session_id=context.session_id,
                    call_id=context.current_call_id,
                    tool_name=tool.name,
                    input_summary=summary,
                    posture_source="path_validator",
                    result="deny",
                    actor="system",
                )
                return ToolResult(
                    output=f"{DENIED_PREFIX}: path validation failed for {tool.name}: {reason}",
                    is_error=True,
                    denied_hard=True,
                    deny_reason=f"path_validator: {reason}",
                )

    posture_source = "tool"
    if decision == PermissionResult.ASK:
        # tool.check_permissions returned ASK directly — rare today.
        posture_source = "tool"
    if policy is not None and decision in (PermissionResult.PASSTHROUGH, PermissionResult.ALLOW):
        posture_source = _resolve_posture_source(policy, tool.name, raw_input)
        decision = policy.get_posture(tool.name, validated)
        if decision == PermissionResult.DENY:
            await approval_log.record_ask(
                session_id=context.session_id,
                call_id=context.current_call_id,
                tool_name=tool.name,
                input_summary=summary,
                posture_source=posture_source,
                result="deny",
                actor="system",
            )
            return ToolResult(
                output=f"{DENIED_PREFIX} by policy: {tool.name}",
                is_error=True,
                denied_hard=True,
                deny_reason="policy default deny",
            )

    if decision == PermissionResult.ASK:
        # Plumb posture_source onto context so the ask_fn implementation
        # (Mirror or REPL) writes the right ledger row alongside its UI.
        context.posture_source = posture_source
        if ask_fn is None:
            if tool.is_read_only():
                logger.info(
                    "tool %s asked approval; no ask_fn wired, read-only — auto-allowing",
                    tool.name,
                )
                await approval_log.record_ask(
                    session_id=context.session_id,
                    call_id=context.current_call_id,
                    tool_name=tool.name,
                    input_summary=summary,
                    posture_source=posture_source,
                    result="allow_once",
                    actor="system",
                )
            elif getattr(type(tool), "headless_quarantine_write", False):
                # Stage 10 — quarantine-write carve-out. A tool whose CLASS
                # declares `headless_quarantine_write = True` (kernel-owned
                # source; deliberately NOT readable from permissions.yaml,
                # and `type(tool)` lookup ignores instance attributes) may
                # proceed unattended because its only write target is a
                # quarantine the runtime never executes from (agents/pending/
                # — W7-A). The operator gate moves to activation
                # (agent_promote / Workspace card), not the write.
                logger.info(
                    "tool %s asked approval; no ask_fn wired — quarantine-write "
                    "carve-out allows",
                    tool.name,
                )
                await approval_log.record_ask(
                    session_id=context.session_id,
                    call_id=context.current_call_id,
                    tool_name=tool.name,
                    input_summary=summary,
                    posture_source=posture_source,
                    result="allow_quarantine_write",
                    actor="system",
                )
            else:
                logger.warning(
                    "tool %s requires approval but no ask_fn is wired — denying by default",
                    tool.name,
                )
                await approval_log.record_ask(
                    session_id=context.session_id,
                    call_id=context.current_call_id,
                    tool_name=tool.name,
                    input_summary=summary,
                    posture_source=posture_source,
                    result="deny",
                    actor="system",
                )
                return ToolResult(
                    output=(
                        f"{DENIED_PREFIX}: {tool.name} requires operator approval, "
                        "but no approval channel is wired in this context. "
                        "An operator-attended session is required for this tool."
                    ),
                    is_error=True,
                    denied_hard=True,
                    deny_reason="ASK posture with no approval channel",
                )
        else:
            # ask_fn implementations write the operator/timeout row themselves
            # (they own the timeout vs decline distinction). decide.evaluate
            # only handles the hard-DENY paths above.
            #
            # Cleared first, and this is the one place that can: the context
            # arrives here already built, and `chat.py` builds it with
            # `dataclasses.replace` off a session-lifetime object — so a value
            # written to that object would be copied into every later call and
            # read as a claim about the call in hand. Empty means the asker
            # made no claim, and that has to be true at the start of each ask
            # rather than merely true so far.
            context.ask_outcome = ""
            approved = await ask_fn(tool, validated, context)
            if not approved:
                return ToolResult(
                    output=NOT_APPROVED_TEMPLATE.format(tool=tool.name),
                    is_error=True,
                )
        # An ASK that was approved is recorded by the `ask_fn` implementation,
        # which owns the decline-vs-timeout distinction. Falling through here
        # must not write a second row for the same call.
        return None

    # Everything that reaches this line ran without anyone being asked.
    #
    # Writing nothing here, on the reasoning that nothing was approved so
    # there is no approval to log, holds right up against
    # `permissions.yaml`, which puts `memory_save`, `web_search` and
    # `delegate_coder` on AUTO, and against autonomy, which runs unattended:
    # the combination is mutating, outbound action with no durable record
    # anywhere, which is the exact class a ledger exists to catch.
    #
    # `result="auto"` rather than `allow_once` keeps "a person said yes" and
    # "nobody was asked" apart — see the `Result` literal for why that matters
    # more than it looks.
    await approval_log.record_ask(
        session_id=context.session_id,
        call_id=context.current_call_id,
        tool_name=tool.name,
        input_summary=summary,
        posture_source=posture_source,
        result="auto",
        actor="system",
    )
    return None


def _resolve_posture_source(
    policy: PermissionPolicy,
    tool_name: str,
    raw_input: dict[str, Any],
) -> str:
    """Identify which policy layer determined the posture for this call.

    Mirrors `PermissionPolicy.resolve_posture` resolution order — path
    overrides, then mode override, then defaults. Returns the layer name
    so the approval ledger can record provenance per row.

    Defensive against minimal duck-typed policy stubs (some tests pass a
    cut-down ``PermissionPolicy`` subclass that only implements
    ``get_posture``); any missing attribute on the introspection path
    falls back to ``"default"``.
    """
    try:
        if policy.has_path_overrides(tool_name):
            path = str(raw_input.get("file_path") or raw_input.get("path") or "")
            if path:
                path_norm = policy._normalize_for_prefix_match(path)
                for rule in policy.path_overrides.get(tool_name) or []:
                    prefix = str(rule.get("path_prefix", ""))
                    if prefix and path_norm.startswith(prefix):
                        return "path"
        if policy.has_mode_override(tool_name):
            return "mode"
        # A mode baseline decided this call too, unless something under it
        # spoke first. The ledger should say "mode" there rather than
        # "default": nothing in `tools:` chose it.
        if policy.mode_baseline() is not None and policy.default_posture(
            tool_name
        ) == policy.mode_baseline():
            return "mode"
    except AttributeError:
        return "default"
    return "default"

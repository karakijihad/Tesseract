"""Tool registry and executor for the assistant chat layer.

Registry maps tool names to `Tool` instances (defined in
`tesseract.kernel.tools.base`). `execute_tool()` validates input via the
tool's pydantic schema, delegates the permission decision to
`tesseract.permissions.decide.evaluate` (single source of truth for tool
permission decisions), and runs the tool when the decision says proceed.

the adapter-facing tool schema shape
and the assistant/tool message roundtrip are OpenAI-native. Gemini's
function-calling message shape is different; the GeminiAdapter's system
message split will handle tool descriptions, but tool-result turns over
Gemini are not yet wired and will error if attempted via the fallback
path. Address in a later session.
"""

from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tesseract.kernel.tokenjuice import (
    BUILTIN_RULES_DIR,
    TokenJuiceConfig,
    load_config as _tj_load_config,
    load_rules as _tj_load_rules,
    process as _tj_process,
    project_rules_dir as _tj_project_rules_dir,
    user_rules_dir as _tj_user_rules_dir,
)
from tesseract.brain import tool_availability
from tesseract.brain.tool_usage import record_tool_call
from tesseract.kernel.adapters.cli import _HARD_ERROR_NEEDLES
from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult
from tesseract.kernel.tools.tool_search import TOOL_SEARCH_NAME
from tesseract.permissions import approval_log
from tesseract.permissions.decide import AskFn, evaluate as evaluate_permission
from tesseract.permissions.policy import PermissionPolicy

logger = logging.getLogger(__name__)

# cli-auth DESIGN.md §3 — use-time cache invalidation. The two headless
# delegate tools, whose failed calls can drop a `providers.yaml cli.<name>`
# provider's stale "ready" auth cache entry. Which provider that is comes from
# the result, not from this set: a seat is filled by config and borrowable per
# call. Reuses `CLIAdapter`'s own hard-error needles
# (`tesseract/kernel/adapters/cli.py`) narrowed to the auth-shaped subset —
# read-only import, kernel stays unedited (kernel lockdown). `lane_turn` /
# `lane_send` are NOT covered here: they resolve their provider through a lane
# binding, and there's no clean generic attachment point for this hook.
_CLI_DELEGATE_TOOLS = frozenset({"delegate_coder", "delegate_auditor"})
_AUTH_SHAPED_NEEDLES = tuple(
    n for n in _HARD_ERROR_NEEDLES if n in ("unauthorized", "authentication", "auth required")
)

# Distributable-app source-edit gate. `delegate_coder`/`delegate_auditor` are
# the sanctioned path for the assistant to edit source (the kernel is
# write-locked against it editing its own) — correct on a dev checkout, where the running tree IS the repo
# being worked on, but pointless (the next update overwrites it) and risky
# (someone else's machine) on an installed copy. Neither tool distinguishes
# "edit" from "analyse" via a dedicated mode/cwd argument; both DO carry a
# `target_paths: list[str]` field ("paths this task will edit... declare
# them for edit tasks"). An empty `target_paths` is the existing signal for
# "not an edit task" (analysis/review/questions), so only a non-empty
# declaration is examined here — non-editing delegate use is unaffected.
#
# What makes a declaration refusable is WHERE it lands, not that it exists.
# The sealed `app/` and `runtime/` trees are the ones an update replaces
# wholesale; everything else an installed copy can reach — `home/workshop/`
# above all, the delegate's own working directory — is durable, and building
# there is ordinary work. Gating on "declared any path at all" refused those
# too, so declaring targets honestly was the thing that got a build denied.
_SOURCE_EDIT_DELEGATE_TOOLS = frozenset({"delegate_coder", "delegate_auditor"})

_SEALED_TARGET_REFUSAL = (
    "Refusing: the declared target_paths land inside the sealed application "
    "tree (app/ or runtime/), which every update replaces wholesale — edits "
    "made there are destroyed silently and are never reviewed. Work under "
    "the home tree instead (workshop/ is writable and survives updates), or "
    "run TESSERACT from a development checkout to change the application "
    "itself."
)


def _declares_sealed_target(target_paths: Any, workspace_root: str) -> bool:
    """True when any declared target path lands inside the sealed `app/` or
    `runtime/` tree.

    Relative paths resolve against `safe_cwd(workspace_root)` — the exact
    directory `_delegate_runner.py` will start the CLI in, not the raw
    `workspace_root` (which IS the sealed `app/` on a packaged install, so
    anchoring there would read every relative path as sealed). Sharing the
    anchor with the run is the point: a gate that resolves paths differently
    from the process it guards judges a location nobody will write to.
    """
    from tesseract.orchestrator.seal_guard import (
        SealViolation,
        assert_cwd_outside_seal,
        safe_cwd,
    )

    try:
        base = safe_cwd(workspace_root or ".")
    except SealViolation:
        # No directory outside the seal exists to anchor against, so nothing
        # can be shown to land outside it either. This gate answers "does
        # this declare a sealed target", and the honest answer when the
        # machine has nowhere safe is yes.
        return True
    for raw in target_paths:
        candidate = Path(str(raw))
        resolved = candidate if candidate.is_absolute() else base / candidate
        try:
            assert_cwd_outside_seal(resolved)
        except SealViolation:
            return True
    return False


async def _installed_tree_source_edit_refusal(
    tool_name: str, validated: Any, raw_input: dict[str, Any], context: ToolContext
) -> ToolResult | None:
    """`ToolResult` refusal when `tool_name` is a source-editing delegate
    call whose declared `target_paths` reach into a sealed tree AND this
    process is running from an installed tree; `None` otherwise (proceed as
    normal). Logged AND recorded in the durable `approvals.jsonl` ledger —
    same forensics trail every other permission denial gets
    (`permissions/decide.py::evaluate`) — so the refusal is visible for ops
    review, not just a transient log line."""
    if tool_name not in _SOURCE_EDIT_DELEGATE_TOOLS:
        return None
    declared = getattr(validated, "target_paths", None)
    if not declared:
        return None

    from tesseract.paths import is_installed_tree

    if not is_installed_tree():
        return None
    if not _declares_sealed_target(declared, context.workspace_root):
        return None

    logger.warning(
        "%s refused on installed tree: declared target_paths=%r reach the sealed tree",
        tool_name,
        declared,
    )
    await approval_log.record_ask(
        session_id=context.session_id,
        call_id=context.current_call_id,
        tool_name=tool_name,
        input_summary=approval_log.summarize_input(raw_input),
        posture_source="installed_tree",
        result="deny",
        actor="system",
    )
    return ToolResult(
        output=_SEALED_TARGET_REFUSAL,
        is_error=True,
        metadata={"reason": "installed_tree_source_edit_refused"},
    )


def _looks_auth_shaped(text: str) -> bool:
    lowered = (text or "").lower()
    return any(needle in lowered for needle in _AUTH_SHAPED_NEEDLES)


def _invalidate_cli_auth_on_failure(tool_name: str, result: ToolResult) -> None:
    """Drop a delegate tool's cached cli-auth state after an auth-shaped
    failure so the next capabilities read/reverify re-probes instead of
    trusting a subscription that just lapsed. Best-effort — never raises.

    The provider is read off the result, not off the tool name: a seat names
    the CLI that fills it by default, but a call may borrow the other one, and
    invalidating by seat would clear the wrong subscription's cache."""
    if tool_name not in _CLI_DELEGATE_TOOLS:
        return
    provider = (result.metadata or {}).get("provider")
    if not provider or not result.is_error or not _looks_auth_shaped(result.output):
        return
    try:
        from tesseract.brain import cli_auth

        cli_auth.invalidate(provider)
    except Exception:
        logger.warning("cli_auth invalidate on %s failure failed", tool_name, exc_info=True)


@dataclass
class ToolRegistry:
    tools: dict[str, Tool] = field(default_factory=dict)
    #: Whether the empty-working-set fallback has already said so. See
    #: `schemas_for_adapter`; it clears when the set comes back.
    #:
    #: Advisory and unsynchronised on purpose, unlike `tools`, which the
    #: snapshot discipline in `schemas_for_adapter` exists to protect because
    #: a worker thread swaps it. Nothing branches on this flag: the worst a
    #: race costs is one warning printed twice or skipped once. It is written
    #: down because the next piece of state added to this class should not
    #: read this one as a precedent for skipping that discipline.
    _warned_empty_working_set: bool = False

    def register(self, tool: Tool) -> None:
        self.tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self.tools.get(name)

    def names(self) -> list[str]:
        return list(self.tools.keys())

    def schemas_for_adapter(
        self,
        enabled_extended: set[str] | None = None,
        *,
        defer_outside_working_set: bool = False,
    ) -> list[dict[str, Any]]:
        """Tool schemas in the shape adapters expect.

        Matches the shape already used by the OpenAI + Gemini adapters'
        `stream(tools=...)` parameter: name, description, input_schema
        as JSON Schema.

        Lean-agent-os P1 Task 2 — tool-schema tiering. `enabled_extended`
        is `None` by default: returns every registered tool (the full
        registry surface), used by callers that need the complete
        picture (capability-matrix generator, introspection tests).
        When a `set` is passed (even empty), tiering is enforced: only
        `tier == "core"` tools plus any tool named in `enabled_extended`
        are returned — this is the live chat-session path, threaded from
        `ChatSession._tool_schemas` via its `_enabled_extended_tools`.
        Visibility only — `execute_tool` resolves any registered tool by
        name regardless of tier.

        ``defer_outside_working_set`` is the same decision made the other way,
        for an adapter that declares `defers_tool_loading`. The whole registry
        goes down the wire with everything outside the working set flagged
        `defer_loading`, and the provider matches those server-side and appends
        the schemas it needs INSIDE the request — so a demoted tool costs no
        round trip there. `tool_search` is left out of that payload: the
        provider's own search does the job, and shipping both is a model
        choosing between two doors into the same room.

        The working set is identical under both paths. What changes is whether
        the tools outside it travel as deferred entries or do not travel at
        all; nothing moves between tiers, and nothing about permission changes.
        """
        # One snapshot, read once. `home_tools.sync_home_tools` swaps this
        # dict from a worker thread, so re-reading `self.tools` between the
        # passes below could build a payload whose core and deferred halves
        # disagree about which tools exist.
        every = list(self.tools.values())
        # The working set first, in registry order, then anything `tool_search`
        # unlocked this session — appended, never interleaved.
        #
        # Tool schemas are serialised ahead of the system prompt, so the first
        # byte that moves in this list invalidates the cached prefix behind it:
        # the prompt, the memory capsule and the whole conversation. Selecting
        # unlocked tools by re-filtering the registry put each one at its
        # REGISTRATION position, which for a tool registered early is near the
        # front of the block. Measured 2026-08-30: every tool-count change in a
        # live session reported `cached=0` rather than the core block's own
        # size, three times in one conversation, re-reading 223,470 tokens at
        # full price to advertise three tools the model had already been handed
        # in `tool_search`'s result.
        #
        # Appending bounds it: the core block holds still, so what survives is
        # everything before the first unlock. It does not remove the cost, and
        # nothing here can — the fix that does is not advertising an unlocked
        # tool at all, which needs the provider to accept a call for a name it
        # was not shown.
        core = [t for t in every if getattr(t, "tier", "extended") == "core"]
        if enabled_extended:
            core += [
                t
                for t in every
                if getattr(t, "tier", "extended") != "core"
                and t.name in enabled_extended
            ]
        deferred: list[Any] = []
        if enabled_extended is None:
            selected = list(every)
        elif defer_outside_working_set:
            core = [t for t in core if t.name != TOOL_SEARCH_NAME]
            loaded = {t.name for t in core}
            deferred = [
                t
                for t in every
                if t.name not in loaded and t.name != TOOL_SEARCH_NAME
            ]
            # Never defer everything: the provider rejects a payload where no
            # tool is loaded, and a session whose working set is somehow empty
            # would reach nothing at all rather than reaching it one turn late.
            selected = [*core, *deferred] if core else list(every)
            if not core:
                deferred = []
                # Said out loud, because the fallback's cost is the whole
                # registry at full price on every turn and the only other
                # signal is a payload panel somebody has to be looking at.
                #
                # Once, not once per request. This is reached from
                # `_tool_schemas` inside the tool loop, so a condition that
                # cannot clear on its own would print on every model call and
                # teach a reader to filter the log. Latched on the registry
                # and released when the set comes back, the way a breaker
                # reports a trip rather than every call after it.
                if not self._warned_empty_working_set:
                    self._warned_empty_working_set = True
                    logger.warning(
                        "working set resolved empty: sending all %d tool "
                        "schemas undeferred. Check working_set.yaml::core.",
                        len(every),
                    )
            else:
                self._warned_empty_working_set = False
        else:
            selected = core
        deferred_names = {t.name for t in deferred}
        schemas: list[dict[str, Any]] = []
        for t in selected:
            # `to_schema`, not the same three keys written again. `tool_search`
            # already builds a tool's schema through it, so a second copy here
            # meant the shape a demoted tool arrives in and the shape it is
            # unlocked in were two definitions that had to be edited together.
            schema = t.to_schema()
            if t.name in deferred_names:
                schema["defer_loading"] = True
            schemas.append(schema)
        return schemas


async def execute_tool(
    registry: ToolRegistry,
    tool_name: str,
    tool_input: dict[str, Any],
    context: ToolContext,
    ask_fn: AskFn | None = None,
    policy: PermissionPolicy | None = None,
) -> ToolResult:
    """Validate, permission-check, and run a single tool call.

    Permission decision lives in `permissions/decide.py::evaluate` — the
    single source of truth for tool decisions. `_dispatch_tool` is the thin
    wrapper that does input-schema validation up front and `tool.run()`
    when `decide.evaluate` returns `None` (proceed). When `decide.evaluate`
    returns a `ToolResult`, that result is the final outcome (denial or
    operator decline).

    **This is where a stored credential is stopped.** Every tool result in the
    runtime comes out of this function — the chat loop, the brief route,
    `surface_open`, the autonomy worker, the open verb and the CLI script all
    call it — so it is the one place a value arriving from outside can be
    caught before it is part of the conversation. The leak the threat model
    names is not the model choosing to disclose a password: it is a service
    returning the authenticated URL in a 401 body, or an exception whose
    frames carry the header, and both arrive here as a `ToolResult`.

    Stopping it on the way IN rather than filtering on the way out is what
    makes the four sinks clean by construction: a value that never enters
    history is never written to the session file, never sent to the provider,
    never folded into a compaction summary and never saved as a memory. The
    filters at those four boundaries stay as backstops for a value that
    arrived by some route this one does not cover.

    The screen wraps the dispatch rather than sitting on each `return`, so a
    branch added later cannot be the one that forgets. That includes the two
    error returns, which interpolate an exception's own text and are the most
    likely carriers of the header case above.
    """
    return _screen_result(
        await _dispatch_tool(
            registry=registry,
            tool_name=tool_name,
            tool_input=tool_input,
            context=context,
            ask_fn=ask_fn,
            policy=policy,
        ),
        tool_name,
    )


def _screen_result(result: ToolResult, tool_name: str) -> ToolResult:
    """Replace any stored credential in a tool's result with a named marker.

    Degrades rather than fails closed, and that is the deliberate half. A
    credential store that cannot be read must not turn every tool call in the
    runtime into an error — the operator would lose the machine over a
    corrupt file. What it does instead is refuse to pass the result through,
    which loses one tool's output and says why. The path that fails CLOSED is
    the provider request, where the cost of guessing wrong is the value
    leaving the machine.
    """
    from tesseract.credentials.redaction import RedactionUnavailable, redact_payload

    try:
        output = redact_payload(result.output)
        metadata = redact_payload(result.metadata) if result.metadata else result.metadata
        deny_reason = redact_payload(result.deny_reason)
    except RedactionUnavailable as exc:
        logger.error("tool %s: result withheld, %s", tool_name, exc)
        # `deny_reason` is REPLACED, not preserved and not blanked, and it is
        # the field this branch originally forgot. `dataclasses.replace` keeps
        # what it is not given, and `chat.py::_result_chunk` tests
        # `denied_hard` FIRST and copies `deny_reason` into the chunk verbatim
        # — so a branch that withheld the output was still handing that string
        # to the transcript.
        #
        # Replaced rather than blanked because `chunk_handler.py` reads it as
        # `raw.get("deny_reason", "denied")`, and an empty string is a present
        # key: the surface would show a hard denial with no reason on it. This
        # text is the runtime's own, so it carries nothing from the tool.
        withheld = (
            f"{tool_name} ran, and its result was withheld: the credential "
            f"store could not be read, so the result could not be checked "
            f"for credentials. {exc}"
        )
        return dataclasses.replace(
            result,
            output=withheld,
            is_error=True,
            metadata=None,
            deny_reason=withheld if result.denied_hard else "",
        )
    if (
        output == result.output
        and metadata is result.metadata
        and deny_reason == result.deny_reason
    ):
        return result
    return dataclasses.replace(
        result, output=output, metadata=metadata, deny_reason=deny_reason
    )


async def _dispatch_tool(
    *,
    registry: ToolRegistry,
    tool_name: str,
    tool_input: dict[str, Any],
    context: ToolContext,
    ask_fn: AskFn | None,
    policy: PermissionPolicy | None,
) -> ToolResult:
    tool = registry.get(tool_name)
    if tool is None:
        return ToolResult(output=f"unknown tool: {tool_name}", is_error=True)

    try:
        validated = tool.input_schema(**tool_input)
    except Exception as e:
        logger.warning("tool %s: input validation failed: %s", tool_name, e)
        return ToolResult(output=f"invalid input for {tool_name}: {e}", is_error=True)

    # Keep the context's approval channel in sync with the effective one so
    # tools can detect attendedness in `run()` (`context.ask_fn is None` =
    # unattended). Chat sessions wire both already; this covers callers that
    # only pass the parameter (Stage 10: agent_create's headless cap).
    if context.ask_fn is None and ask_fn is not None:
        context.ask_fn = ask_fn

    # Same reasoning for the policy: a tool that dispatches to another tool
    # (`open` → os_launch/os_open_url/surface_create) must be able to forward
    # it. Without it `decide.evaluate` never reaches the operator-policy layer
    # and the nested call proceeds at PASSTHROUGH, skipping its configured
    # ASK/DENY entirely. Syncing here fixes every caller at once rather than
    # asking each one to remember.
    if context.policy is None and policy is not None:
        context.policy = policy

    # Before the permission gate, on purpose: asking the operator to approve a
    # call the runtime is about to refuse anyway is the confusing half. After
    # validation, because a call that never had a valid shape says nothing
    # about whether the thing behind it is answering.
    unavailable = tool_availability.gate(tool, tool_name)
    if unavailable is not None:
        return unavailable

    refusal = await _installed_tree_source_edit_refusal(tool_name, validated, tool_input, context)
    if refusal is not None:
        return refusal

    denial = await evaluate_permission(
        tool=tool,
        validated=validated,
        raw_input=tool_input,
        context=context,
        ask_fn=ask_fn,
        policy=policy,
    )
    if denial is not None:
        return denial

    # Recorded once the call is going to happen — after the permission gate,
    # so a denial is not counted as usage, and before `run()`, so a tool that
    # raises still counts. The question this answers is "did she reach for it",
    # not "did it work".
    await record_tool_call(tool_name, context.session_id)

    try:
        result = await tool.run(validated, context)
    except Exception as e:
        logger.exception("tool %s execution failed", tool_name)
        tool_availability.record(tool, tool_name, exc=e)
        return ToolResult(output=f"tool {tool_name} error: {e}", is_error=True)
    tool_availability.record(tool, tool_name, result=result)

    _invalidate_cli_auth_on_failure(tool_name, result)
    return _apply_tokenjuice(result, tool_name, tool_input)


# ── TokenJuice — tool-output compression ────────────────────────────
# Loaded once on first call; reset_tokenjuice_cache() exists for test fixtures
# that need to reload after a config swap or TESSERACT_HOME monkeypatch.
_TJ_CACHE: dict[str, Any] = {"config": None, "rules": None, "init_failed": False}


def _tj_state() -> tuple[TokenJuiceConfig, list] | tuple[None, None]:
    if _TJ_CACHE["init_failed"]:
        return None, None
    if _TJ_CACHE["config"] is None:
        try:
            cfg = _tj_load_config()
            rules = _tj_load_rules(
                BUILTIN_RULES_DIR, _tj_user_rules_dir(), _tj_project_rules_dir()
            )
            _TJ_CACHE["config"] = cfg
            _TJ_CACHE["rules"] = rules
        except Exception:
            logger.exception("tokenjuice init failed; passthrough until reset")
            _TJ_CACHE["init_failed"] = True
            return None, None
    return _TJ_CACHE["config"], _TJ_CACHE["rules"]


def reset_tokenjuice_cache() -> None:
    """Drop the cached TokenJuice config + rules. Tests call this when
    swapping TESSERACT_HOME or installing a custom config path."""
    _TJ_CACHE["config"] = None
    _TJ_CACHE["rules"] = None
    _TJ_CACHE["init_failed"] = False


def compress_for_delivery(text: str, tool_name: str) -> tuple[str, bool]:
    """Run the TokenJuice chain over arbitrary text, outside a tool call.

    For output that reaches the model somewhere other than a `tool_result`
    envelope — a background spawn's completion, delivered into the
    conversation rather than pointed at. Same rules, so a lane transcript is
    compressed by the same head+tail that already preserves an auditor's
    verdict at the tail.

    Returns `(text, was_compressed)`; the flag is what lets a caller say so
    rather than silently hand over a trimmed result. Best-effort: any failure
    returns the text untouched."""
    if not text:
        return text, False
    cfg, rules = _tj_state()
    if cfg is None or not cfg.enabled or not rules:
        return text, False
    try:
        pr = _tj_process(
            text,
            tool_name,
            {},
            rules=rules,
            enabled=cfg.enabled,
            dry_run=cfg.dry_run,
            audit_log=cfg.audit_log,
            disabled_rules=cfg.disabled_rules,
        )
    except Exception:
        logger.exception("tokenjuice process raised; returning raw text")
        return text, False
    return pr.text, pr.text != text


def _apply_tokenjuice(
    result: ToolResult, tool_name: str, tool_input: dict[str, Any]
) -> ToolResult:
    """Run the TokenJuice reducer chain over `result.output`. Best-effort —
    any failure logs and returns the original result so a broken rule cannot
    sink a successful tool call."""
    if not result.output:
        return result
    cfg, rules = _tj_state()
    if cfg is None or not cfg.enabled or not rules:
        return result
    try:
        pr = _tj_process(
            result.output,
            tool_name,
            tool_input,
            rules=rules,
            enabled=cfg.enabled,
            dry_run=cfg.dry_run,
            audit_log=cfg.audit_log,
            disabled_rules=cfg.disabled_rules,
        )
    except Exception:
        logger.exception("tokenjuice process raised; returning raw output")
        return result
    if pr.text == result.output:
        return result
    # `timed_out` carried too. Rebuilding field by field dropped it, and it is
    # the one field with a consumer that branches on it rather than displaying
    # it: `kernel_worker_runner` reads it to tell "ran out of time, park it"
    # from "the tool failed, mark it FAILED". Compression only rewrites a
    # result whose text it actually changed, which a timed-out `lane_turn`
    # returning a large partial answer is exactly the shape of.
    return ToolResult(
        output=pr.text,
        is_error=result.is_error,
        metadata=result.metadata,
        denied_hard=result.denied_hard,
        deny_reason=result.deny_reason,
        timed_out=result.timed_out,
    )

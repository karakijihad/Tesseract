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
from tesseract.kernel.adapters.cli import _HARD_ERROR_NEEDLES
from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult
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

    base = safe_cwd(workspace_root or ".")
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

    def register(self, tool: Tool) -> None:
        self.tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self.tools.get(name)

    def names(self) -> list[str]:
        return list(self.tools.keys())

    def schemas_for_adapter(
        self, enabled_extended: set[str] | None = None
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
        """
        if enabled_extended is None:
            selected = self.tools.values()
        else:
            selected = [
                t
                for t in self.tools.values()
                if getattr(t, "tier", "extended") == "core"
                or t.name in enabled_extended
            ]
        return [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.input_schema.model_json_schema(),
            }
            for t in selected
        ]


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
    single source of truth for tool decisions. This function is the thin
    wrapper that does input-schema validation up front and `tool.run()`
    when `decide.evaluate` returns `None` (proceed). When `decide.evaluate`
    returns a `ToolResult`, that result is the final outcome (denial or
    operator decline).
    """
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

    try:
        result = await tool.run(validated, context)
    except Exception as e:
        logger.exception("tool %s execution failed", tool_name)
        return ToolResult(output=f"tool {tool_name} error: {e}", is_error=True)

    _invalidate_cli_auth_on_failure(tool_name, result)
    return _apply_tokenjuice(result, tool_name, tool_input)


# ── TokenJuice (AU-15) — tool-output compression ────────────────────────────
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
    return ToolResult(
        output=pr.text,
        is_error=result.is_error,
        metadata=result.metadata,
        denied_hard=result.denied_hard,
        deny_reason=result.deny_reason,
    )

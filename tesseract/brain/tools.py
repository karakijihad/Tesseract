"""Tool registry and executor for the TARS chat layer.

Registry maps tool names to `Tool` instances (defined in
`tesseract.kernel.tools.base`). `execute_tool()` validates input via the
tool's pydantic schema, delegates the permission decision to
`tesseract.permissions.decide.evaluate` (single source of truth for tool
permission decisions), and runs the tool when the decision says proceed.

Scope note (tars-reboot Session 2): the adapter-facing tool schema shape
and the assistant/tool message roundtrip are OpenAI-native. Gemini's
function-calling message shape is different; the GeminiAdapter's system
message split will handle tool descriptions, but tool-result turns over
Gemini are not yet wired and will error if attempted via the fallback
path. Address in a later session.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
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
from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult
from tesseract.permissions.decide import AskFn, evaluate as evaluate_permission
from tesseract.permissions.policy import PermissionPolicy

logger = logging.getLogger(__name__)


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

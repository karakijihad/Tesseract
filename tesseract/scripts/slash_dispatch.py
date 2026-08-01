"""Universal slash dispatcher — operator-side surface to every kernel tool.

Operator types ``/<tool_name> key=value [...]`` (or ``/<tool_name> "single arg"``
for tools with exactly one required field). The dispatcher:

  1. parses the line via :mod:`shlex`,
  2. coerces tokens onto the tool's Pydantic ``input_schema``,
  3. runs the security + path-validation layers from
     :mod:`tesseract.permissions.decide` (security DENY, kernel-lockdown
     path checks for write tools),
  4. runs the tool with ``posture_source="operator-slash"`` audit hint,
  5. pretty-prints the :class:`ToolResult`.

Operator slash intentionally bypasses policy posture (AUTO/ASK/DENY in
``permissions.yaml``) — the operator initiated the call, so there is
nothing to confirm. DENY from ``tool.check_permissions`` still hard-blocks
(security layer is absolute). Path validation for write tools also still
runs — the kernel-lockdown boundary protects against typos and slip-ups
even when the operator is the caller. ASK returned directly from
``check_permissions`` (rare; no existing tool does this) is treated as
allow under operator slash.

**Security: TARS must not reach this dispatcher.** Because ``run_slash``
skips policy posture, any Tool that wrapped it would let the LLM bypass
``permissions.yaml``. Two structural defenses guard this:

  1. ``run_slash`` requires the module-private ``_OPERATOR_TOKEN`` sentinel
     as ``caller_token``. In production code only
     the Mirror commands registry imports it; test modules also
     import it to drive the dispatcher in unit tests. Any *other* import
     of ``_OPERATOR_TOKEN`` (especially from a Tool subclass) is a hard
     review block.
  2. Kernel lockdown (CLAUDE.md) prevents TARS from writing source, so it
     cannot author a Tool that calls this module in the first place.

``print_slash_help(registry)`` renders the dynamic ``/help`` output.
``/exit`` and ``/resume`` are control flow, not tools, and stay native
inside the retired REPL (deleted 2026-07-13).
"""

from __future__ import annotations

import difflib
import json
import shlex
from typing import Any

from pydantic import BaseModel, ValidationError

from tesseract.brain.tools import ToolRegistry
from tesseract.kernel.tools.base import (
    PermissionResult,
    Tool,
    ToolContext,
    ToolResult,
)
from tesseract.paths import home_dir, install_root
from tesseract.permissions.decide import _WRITE_PATH_TOOLS
from tesseract.permissions.path_validator import validate_path

_RESULT_CLIP = 4096

_BOOL_TRUE = {"true", "yes", "y", "1", "on"}
_BOOL_FALSE = {"false", "no", "n", "0", "off"}

# Ergonomic shortcuts mapping ``/<alias>`` to the canonical tool name.
# Kept tiny — anything bigger than name-equivalence belongs in its own
# command handler. Aliases are resolved before registry lookup so the
# universal `key=value` arg shape applies identically.
_COMMAND_ALIASES: dict[str, str] = {
    "brief": "brief_render",
    "read_brief": "brief_read",
}

# Operator-only sentinels. Only operator-driven frontends and the
# Mirror WS slash dispatch (`tesseract.mirror.server.commands_registry`)
# may import these — both are operator-only surfaces. A Tool importing
# either is a security review blocker — see the module docstring.
# Identity-checked against `caller_token` in `run_slash`.
_OPERATOR_TOKEN: object = object()
_MIRROR_OPERATOR_TOKEN: object = object()
_VALID_OPERATOR_TOKENS = (_OPERATOR_TOKEN, _MIRROR_OPERATOR_TOKEN)


def parse_slash(line: str) -> tuple[str, dict[str, str], list[str]] | None:
    """Parse ``/tool key=value "a b" positional`` into ``(name, kv, positional)``.

    Returns ``None`` if the line doesn't start with ``/`` or contains no name.
    Raises :class:`ValueError` on shlex parse failure (mismatched quoting).
    """
    if not line.startswith("/"):
        return None
    raw = line[1:].strip()
    if not raw:
        return None
    try:
        tokens = shlex.split(raw)
    except ValueError as exc:
        raise ValueError(f"could not parse command (mismatched quotes?): {exc}") from exc
    if not tokens:
        return None
    name, *rest = tokens
    kv: dict[str, str] = {}
    positional: list[str] = []
    for tok in rest:
        if "=" in tok:
            k, _, v = tok.partition("=")
            k = k.strip()
            if k:
                kv[k] = v
                continue
        positional.append(tok)
    return name, kv, positional


def _coerce_value(annotation: Any, raw: str) -> Any:
    """String → typed-ish for Pydantic. Booleans need help; the rest passes through."""
    stripped = raw.strip()
    if annotation is bool:
        low = stripped.lower()
        if low in _BOOL_TRUE:
            return True
        if low in _BOOL_FALSE:
            return False
    if stripped.startswith(("[", "{")):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            pass
    return raw


def coerce_args(
    schema: type[BaseModel],
    kv: dict[str, str],
    positional: list[str],
) -> BaseModel:
    """Validate parsed args against ``schema``.

    Single-positional shortcut: if no kv pairs and the schema has exactly one
    required field, bind the joined positional tokens to it. Mixing positional
    and ``key=value`` is rejected.
    """
    fields = schema.model_fields
    if positional and not kv:
        required = [n for n, info in fields.items() if info.is_required()]
        if len(required) != 1:
            raise ValueError(
                "positional arg only works when the tool has exactly one required field; "
                f"required: {', '.join(required) or '(none)'}. Use key=value pairs."
            )
        kv = {required[0]: " ".join(positional)}
    elif positional and kv:
        raise ValueError("mixing positional and key=value args is unsupported")

    unknown = set(kv) - set(fields)
    if unknown:
        raise ValueError(f"unknown args for {schema.__name__}: {', '.join(sorted(unknown))}")

    coerced: dict[str, Any] = {
        k: _coerce_value(fields[k].annotation, v) for k, v in kv.items()
    }
    try:
        return schema(**coerced)
    except ValidationError as exc:
        raise ValueError(_format_validation_error(exc)) from exc


def _format_validation_error(exc: ValidationError) -> str:
    parts = [
        f"{'.'.join(str(x) for x in err['loc']) or '?'}: {err['msg']}"
        for err in exc.errors()
    ]
    return "; ".join(parts)


def _format_result(result: ToolResult) -> str:
    text = result.output or ""
    if result.metadata:
        try:
            payload = json.dumps(result.metadata, indent=2, default=str)
        except (TypeError, ValueError):
            payload = repr(result.metadata)
        if len(payload) > _RESULT_CLIP:
            payload = payload[:_RESULT_CLIP] + "\n…(truncated)"
        text = f"{text}\n{payload}" if text else payload
    if result.denied_hard:
        return f"[denied] {result.deny_reason or text}"
    if result.is_error:
        return f"[error] {text}"
    return text


def _slash_context(parent: ToolContext, tool_name: str) -> ToolContext:
    """Fork a child context for a slash invocation: fresh cancel_event, audit hint."""
    return ToolContext(
        workspace_root=parent.workspace_root,
        session_id=parent.session_id,
        current_call_id=f"slash-{tool_name}",
        posture_source="operator-slash",
        cli_sink=parent.cli_sink,
        pty_dispatcher=parent.pty_dispatcher,
        scheduler_provider=parent.scheduler_provider,
        ask_fn=parent.ask_fn,
    )


async def run_slash(
    registry: ToolRegistry,
    name: str,
    kv: dict[str, str],
    positional: list[str],
    tool_context: ToolContext,
    *,
    caller_token: object | None = None,
) -> str:
    """Resolve, validate, run. Returns the printable output.

    ``caller_token`` MUST be one of ``_OPERATOR_TOKEN`` (REPL) or
    ``_MIRROR_OPERATOR_TOKEN`` (Mirror chat WS). The check is identity
    (``is``), not equality — a Tool cannot forge it by passing
    ``object()``. Any other caller raises :class:`RuntimeError`. See the
    module docstring for the threat model.
    """
    if caller_token not in _VALID_OPERATOR_TOKENS:
        raise RuntimeError(
            "run_slash() is operator-only and bypasses permissions.yaml; "
            "caller_token must be _OPERATOR_TOKEN (REPL) or "
            "_MIRROR_OPERATOR_TOKEN (Mirror chat). If you are wiring a "
            "Tool to call this, STOP — that would let the LLM escape "
            "policy posture."
        )
    resolved_name = _COMMAND_ALIASES.get(name, name)
    tool = registry.get(resolved_name)
    if tool is None:
        suggestions = difflib.get_close_matches(name, registry.names(), n=3, cutoff=0.5)
        hint = f"  did you mean: {', '.join(suggestions)}" if suggestions else ""
        return f"[unknown command: /{name}]{hint}\n[type /help for the list]"
    try:
        validated = coerce_args(tool.input_schema, kv, positional)
    except ValueError as exc:
        return f"[invalid args: {exc}]\n  {_usage_for(tool)}"
    denial = _security_gate(tool, validated, tool_context)
    if denial is not None:
        return denial
    try:
        result = await tool.run(validated, _slash_context(tool_context, tool.name))
    except Exception as exc:
        return f"[tool error] {type(exc).__name__}: {exc}"
    return _format_result(result)


def _security_gate(tool: Tool, validated: BaseModel, ctx: ToolContext) -> str | None:
    """Run the absolute layers of the permission pipeline.

    Layer 1: ``tool.check_permissions`` — security DENY (e.g. ``bash``
    pattern blocks). Operator slash cannot bypass this.

    Layer 2: ``validate_path`` for write tools — kernel-lockdown
    boundary checks (write-boundary escape, UNC, tilde, double-encoded
    traversal). Apply to operator slash too: protects against typos
    and accidental writes outside the state root.

    Layer 3 (policy posture) is *intentionally* skipped — the operator
    initiated the call, so there is nothing to confirm. ASK returned
    directly from ``check_permissions`` is also treated as allow.
    """
    if tool.check_permissions(validated, ctx) == PermissionResult.DENY:
        return f"[denied: {tool.name} blocked by tool's own security check]"
    raw = validated.model_dump()
    for path_key in _WRITE_PATH_TOOLS.get(tool.name, ()):
        path = raw.get(path_key) or ""
        if path:
            valid, reason = validate_path(
                str(path),
                write_root=str(home_dir()),
                read_root=str(install_root()),
                mode="write",
            )
            if not valid:
                return f"[denied: path validation rejected {tool.name}: {reason}]"
    return None


def _usage_for(tool: Tool) -> str:
    parts = [
        f"<{n}>" if info.is_required() else f"[{n}]"
        for n, info in tool.input_schema.model_fields.items()
    ]
    return f"/{tool.name} " + " ".join(parts) if parts else f"/{tool.name}"


def _category_of(name: str) -> str:
    head, _, _ = name.partition("_")
    return head or name


def print_slash_help(registry: ToolRegistry) -> None:
    names = sorted(registry.names())
    by_cat: dict[str, list[str]] = {}
    for n in names:
        by_cat.setdefault(_category_of(n), []).append(n)
    print()
    print("  operator slash commands — every registered kernel tool is callable")
    print()
    for cat in sorted(by_cat):
        print(f"  [{cat}]")
        for n in by_cat[cat]:
            tool = registry.get(n)
            usage = _usage_for(tool) if tool is not None else f"/{n}"
            print(f"    {usage}")
        print()
    print("  native (not tools):  /help  /exit  /resume")
    if _COMMAND_ALIASES:
        print("  aliases:")
        for alias, target in sorted(_COMMAND_ALIASES.items()):
            print(f"    /{alias}  →  /{target}")
    print("  arg forms:")
    print("    /tool key=value other=\"two words\"")
    print("    /tool \"single positional\"   (only when the tool has one required field)")
    print()

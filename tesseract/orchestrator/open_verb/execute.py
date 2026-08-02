"""Carry out a `Resolution`.

`open` holds no authority of its own. It resolves a target, then invokes one of
three primitives through the same gateway every other tool call uses —
`execute_tool` → `decide.evaluate` → `tool.run`. Calling a primitive's `run()`
directly would work and would skip the posture on it, which is the whole reason
`os_launch` is ASK. So it is never done.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tesseract.kernel.tools.base import ToolContext, ToolResult
from tesseract.orchestrator.open_verb.resolve import Resolution
from tesseract.paths import home_dir, install_root
from tesseract.permissions.path_validator import validate_path

# A text surface renders content, not a link, so the file has to be read. That
# is a read against the boundary and belongs here rather than in the resolver.
_MAX_TEXT_BYTES = 400_000


@dataclass(frozen=True)
class OpenOutcome:
    destination: str
    resolved_kind: str
    canonical_target: str
    handler: str
    reason: str
    surface_id: str | None = None
    is_error: bool = False


class ExecutionUnavailable(RuntimeError):
    """No tool registry on the context — `open` cannot reach its primitives."""


def _read_text(path_str: str) -> str:
    ok, why = validate_path(
        path_str,
        write_root=str(home_dir()),
        read_root=str(install_root()),
        mode="read",
        resolve_symlinks=True,
    )
    if not ok:
        raise PermissionError(why)

    path = Path(path_str)
    raw = path.read_bytes()[: _MAX_TEXT_BYTES + 1]
    text = raw.decode("utf-8", errors="replace")
    if len(raw) > _MAX_TEXT_BYTES:
        # Say so rather than ending mid-line as if that were the whole file.
        return text[:_MAX_TEXT_BYTES] + "\n\n… truncated — the file is larger than this card shows."
    return text


async def execute(
    resolution: Resolution,
    context: ToolContext,
    *,
    view: str = "tars",
) -> OpenOutcome:
    if context.tool_registry_provider is None:
        raise ExecutionUnavailable("no tool registry on this context")
    registry = context.tool_registry_provider()

    tool_name, tool_input = _dispatch_for(resolution, view)

    from tesseract.brain.tools import execute_tool

    result: ToolResult = await execute_tool(
        registry,
        tool_name,
        tool_input,
        context,
        ask_fn=context.ask_fn,
    )

    if result.is_error:
        # A denial or a refusal is an outcome, not an exception — the caller
        # gets the reason the primitive gave, not a traceback.
        return OpenOutcome(
            destination=resolution.destination,
            resolved_kind=resolution.resolved_kind,
            canonical_target=resolution.canonical_target,
            handler=resolution.handler,
            reason=result.output,
            is_error=True,
        )

    surface_id = None
    if isinstance(result.metadata, dict):
        raw_id = result.metadata.get("surface_id")
        surface_id = str(raw_id) if raw_id else None

    return OpenOutcome(
        destination=resolution.destination,
        resolved_kind=resolution.resolved_kind,
        canonical_target=resolution.canonical_target,
        handler=resolution.handler,
        reason=resolution.reason,
        surface_id=surface_id,
    )


def _dispatch_for(resolution: Resolution, view: str) -> tuple[str, dict[str, Any]]:
    if resolution.handler == "surface":
        props = dict(resolution.props)
        if resolution.text_from:
            props["text"] = _read_text(resolution.text_from)
        return "surface_create", {
            "type": resolution.surface_type,
            "view": view,
            "props": props,
            "title": Path(resolution.canonical_target).name or resolution.canonical_target,
        }

    if resolution.handler == "url":
        return "os_open_url", {"url": resolution.canonical_target}

    if resolution.handler == "launch":
        return "os_launch", {"path": resolution.canonical_target}

    raise ValueError(f"unroutable resolution handler: {resolution.handler!r}")

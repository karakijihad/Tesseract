"""Shared helpers for the seven `lane_*` kernel tools.

Mirrors `mission/tool_support.py::resolve_mission_manager` shape so the
tool surface is uniform: each lane tool calls `resolve_lane_manager`
on its `ToolContext`, returns a clean error `ToolResult` when the
provider is unwired (Mirror, REPL, boot-failure paths), and proceeds
otherwise."""

from __future__ import annotations

import logging
from inspect import isawaitable
from typing import TYPE_CHECKING, Any

from tesseract.kernel.tools.base import ToolContext

if TYPE_CHECKING:
    from .manager import LaneManager
    from .named import NamedLaneManager

logger = logging.getLogger(__name__)


async def maybe_await(value: Any) -> Any:
    """Await `value` iff it is awaitable. Lets a tool call a manager method
    that is sync on the real (in-process) LaneManager/NamedLaneManager but
    async on the Mirror IPC proxy (IpcLaneManager/IpcNamedLaneManager)."""
    return await value if isawaitable(value) else value


def resolve_lane_manager(context: ToolContext) -> "LaneManager | None":
    """Return the `LaneManager` from context, or `None` if unavailable.

    Degrades gracefully when wiring is incomplete — provider exceptions
    are logged but not re-raised so the calling tool can return a clean
    error `ToolResult` rather than an unhandled traceback. Mirror does
    not host a `LaneManager` (only the controller daemon does), so any
    `lane_*` invocation from Mirror's brain currently returns the
    'lane manager not wired' error — Session C lands the IPC bridge."""
    provider = getattr(context, "lane_manager_provider", None)
    if provider is None:
        return None
    try:
        return provider()
    except Exception:  # noqa: BLE001 — degrade gracefully when wiring is incomplete
        logger.exception("lane_manager_provider raised")
        return None


def resolve_named_lane_manager(context: ToolContext) -> "NamedLaneManager | None":
    """X-5 — sibling of `resolve_lane_manager` for the name→lane_id
    binding layer. Same degrade-gracefully contract."""
    provider = getattr(context, "named_lane_manager_provider", None)
    if provider is None:
        return None
    try:
        return provider()
    except Exception:  # noqa: BLE001
        logger.exception("named_lane_manager_provider raised")
        return None


def catalog_lane_models(kind: str) -> frozenset[str]:
    """Concrete model ids declared under ``cli.<kind>.models`` in
    providers.yaml. Empty when the kind has no catalog entry."""
    from tesseract.config.loader import load_config

    block = (load_config().providers_raw.get("cli") or {}).get(kind) or {}
    return frozenset(
        str(entry["model"])
        for entry in (block.get("models") or {}).values()
        if isinstance(entry, dict) and entry.get("model")
    )


def validate_lane_model(kind: str, model: str) -> str | None:
    """Return an error string when ``model`` is not a catalog model for
    ``kind``; ``None`` when valid. The model field on lane_open /
    lane_named_ensure is free text and gets passed verbatim as ``--model``
    when the CLI spawns — an invented id (a binding recorded
    ``codex-mini``, in no catalog, 2026-07-12) fails every send with a
    provider 400, so reject it at the tool boundary instead."""
    # An api lane names a provider REF (`api.<provider>.<model>`) because its
    # adapter resolves one (`manager.py::_default_adapter_factory`), not a bare
    # `--model` argv. The cli catalog is keyed `cli.<kind>`, so looking an api
    # lane up there searches a `cli.api` block that does not exist and rejects
    # every api lane — which made the api tier unreachable through this path.
    if kind == "api":
        from tesseract.config.loader import load_config

        try:
            load_config().resolve(model)
        except Exception as exc:  # noqa: BLE001 — surfaces as a tool error
            return (
                f"model {model!r} does not resolve as an api provider ref "
                f"(expected `api.<provider>.<model>` from providers.yaml): {exc}"
            )
        return None

    try:
        valid = catalog_lane_models(kind)
    except Exception as exc:  # noqa: BLE001 — config errors surface as tool errors
        return f"model validation failed reading providers.yaml: {exc}"
    if model in valid:
        return None
    options = ", ".join(sorted(valid)) or "<no cli models for this kind>"
    return (
        f"model {model!r} is not in the providers.yaml catalog for "
        f"cli.{kind}; valid: {options}"
    )


__all__ = [
    "catalog_lane_models",
    "maybe_await",
    "resolve_lane_manager",
    "resolve_named_lane_manager",
    "validate_lane_model",
]

"""``MCPRemoteTool`` — bridges one remote MCP tool into the local registry.

Each tool a curated server exposes becomes one ``MCPRemoteTool`` instance the
registry dispatches exactly like a native tool: ``execute_tool`` validates its
input, ``decide.evaluate`` gates it, ``chat.py`` wraps its output in the
untrusted envelope, and ``_apply_tokenjuice`` compresses it — all for free
because it IS a ``Tool``.

Security posture (mirrors ``web_search`` — the closest native analog: external,
untrusted content, operator-gated):
  * ``default_posture = "ask"``   — external capability floor (contract #2).
  * ``untrusted_source = True``   — output envelope-wrapped (contract #3).
  * ``tier = "extended"``         — surfaces only via ``tool_search``.
  * ``is_read_only() -> False``   — so a headless ASK (no ask_fn) DENIES rather
    than auto-allowing a possibly-mutating remote call (contract #6).
  * name namespaced by ``tool_prefix`` — cannot shadow a core tool (contract #8).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, ClassVar

from pydantic import BaseModel, ConfigDict

from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult
from tesseract.mcp_client.audit import append_mcp_client_audit_row, hash_params

log = logging.getLogger(__name__)

# Callable the manager wires so the tool always reaches the CURRENT live
# session for its server (or None when disconnected) without holding a stale
# reference across a reconnect.
SessionProvider = Callable[[], Any]

_MAX_SUMMARY = 200


def build_input_model(model_name: str, remote_schema: dict[str, Any] | None) -> type[BaseModel]:
    """Build a passthrough Pydantic model from a remote ``inputSchema``.

    The model accepts arbitrary fields (``extra="allow"``) so any argument the
    remote declares round-trips through ``execute_tool``'s validation step, and
    its ``model_json_schema()`` returns the remote's own JSON Schema verbatim so
    the chat model sees the real parameter shape. The remote server remains the
    authority on argument validity (contract: we gate + wrap, we don't re-spec).
    """
    schema: dict[str, Any] = (
        dict(remote_schema)
        if isinstance(remote_schema, dict) and remote_schema
        else {"type": "object", "properties": {}}
    )

    class _RemoteArgs(BaseModel):
        model_config = ConfigDict(extra="allow")

        @classmethod
        def model_json_schema(cls, *args: Any, **kwargs: Any) -> dict[str, Any]:  # type: ignore[override]
            return schema

    _RemoteArgs.__name__ = model_name
    _RemoteArgs.__qualname__ = model_name
    return _RemoteArgs


def _render_content(result: Any) -> str:
    """Flatten a ``CallToolResult``'s content blocks to text."""
    parts: list[str] = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text is not None:
            parts.append(str(text))
        else:
            parts.append(f"[non-text content: {getattr(block, 'type', 'unknown')}]")
    return "\n".join(parts)


class MCPRemoteTool(Tool):
    default_posture: ClassVar[str] = "ask"
    risk_class: ClassVar[str] = "propose"
    untrusted_source: ClassVar[bool] = True
    tier: ClassVar[str] = "extended"

    def __init__(
        self,
        *,
        server_name: str,
        tool_prefix: str,
        remote_name: str,
        description: str,
        input_schema: dict[str, Any] | None,
        session_provider: SessionProvider,
        tool_call_timeout_s: int,
    ) -> None:
        self._server_name = server_name
        self._remote_name = remote_name
        self._local_name = f"{tool_prefix}{remote_name}"
        self._description = description or f"{remote_name} (external MCP tool on {server_name})"
        self._session_provider = session_provider
        self._timeout_s = tool_call_timeout_s
        self._input_model = build_input_model(f"{self._local_name}_Args", input_schema)

    @property
    def name(self) -> str:
        return self._local_name

    @property
    def description(self) -> str:
        return self._description

    @property
    def input_schema(self) -> type[BaseModel]:
        return self._input_model

    def is_read_only(self) -> bool:
        # Unknown remote side effects → treat as write so a headless ASK denies.
        return False

    def is_concurrency_safe(self) -> bool:
        return False

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        args = tool_input.model_dump()
        phash = hash_params(args)
        session = self._session_provider()
        if session is None:
            await self._audit("error", phash, "server not connected")
            return ToolResult(
                output=(
                    f"MCP server '{self._server_name}' is not connected; "
                    f"tool '{self._local_name}' is unavailable."
                ),
                is_error=True,
            )

        try:
            result = await asyncio.wait_for(
                session.call_tool(self._remote_name, args), timeout=self._timeout_s
            )
        except asyncio.TimeoutError:
            await self._audit("timeout", phash, f"exceeded {self._timeout_s}s")
            return ToolResult(
                output=f"MCP tool '{self._local_name}' timed out after {self._timeout_s}s.",
                is_error=True,
                timed_out=True,
            )
        except Exception as exc:  # noqa: BLE001 - surface any transport/RPC error as a tool error
            log.warning("mcp_client: %s call failed: %s", self._local_name, exc)
            await self._audit("error", phash, str(exc)[:_MAX_SUMMARY])
            return ToolResult(
                output=f"MCP tool '{self._local_name}' error: {exc}", is_error=True
            )

        output = _render_content(result)
        is_error = bool(getattr(result, "isError", False))
        # Audit records METADATA, not content — external tool output is
        # untrusted and must not be persisted raw into the audit sink (the
        # envelope guards model history; this guards the log). Size only.
        await self._audit(
            "error" if is_error else "ok", phash, f"{len(output)} chars"
        )
        return ToolResult(
            output=output,
            is_error=is_error,
            metadata={"mcp_server": self._server_name, "remote_tool": self._remote_name},
        )

    async def _audit(self, outcome: str, params_hash: str, summary: str) -> None:
        await append_mcp_client_audit_row(
            server=self._server_name,
            tool=self._local_name,
            remote_tool=self._remote_name,
            outcome=outcome,
            params_hash=params_hash,
            result_summary=summary,
        )


__all__ = ["MCPRemoteTool", "build_input_model"]

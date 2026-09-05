"""brief_render — operator-facing tool that renders today's daily brief.

Wraps :class:`tesseract.orchestrator.brief.renderer.BriefRenderer`. The
``/brief`` REPL alias resolves to this tool. Synchronous overwrite is
the contract — a re-run on the same day replaces today's file (matches
the `/brief` slash semantics in `_shared/brief-renderer-spec.md`). The
the nightly `brief_render` stage calls the renderer directly with
``overwrite=False`` so a missed slot does not double-write.

The class floor is ASK, and ``permissions.yaml`` currently overrides it to
``auto``: the tool writes one file into the operator's own store and calls a
chain whose ceiling is a `roles.yaml` block named for the entry, and it is
operator-initiated in both its call sites. It used to fire Tavily searches
under a separate spend cap, which was the original reason for the override;
that cap and those searches are gone, so the override now rests on the file
write alone. ``permissions.yaml`` is the authority either way.
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, Field

from tesseract.agents.loader import load_agent
from tesseract.kernel.adapters.base import AdapterOptions, ModelAdapter
from tesseract.kernel.tools.base import PermissionResult, Tool, ToolContext, ToolResult
from tesseract.memory.store import MemoryStore
from tesseract.orchestrator.brief.renderer import BriefRenderer
from tesseract.paths import TESSERACT_HOME
from tesseract.lib import clock

logger = logging.getLogger(__name__)

DEFAULT_DIGESTER_TIMEOUT_S = 60.0


class BriefRenderInput(BaseModel):
    date: str = Field(
        default="",
        description=(
            "Target ISO date (YYYY-MM-DD). Empty = today UTC. The renderer "
            "writes to ``memory-store/daily/briefs/<iso-date>.md``."
        ),
    )
    overwrite: bool = Field(
        default=True,
        description=(
            "When true (the /brief default), replace today's file. When "
            "false (cron default) and the file already exists, the call "
            "returns the existing brief without re-running the digesters."
        ),
    )


class BriefRenderTool(Tool):
    default_posture: ClassVar[str] = "ask"

    risk_class: ClassVar[str] = "propose"

    group: ClassVar[str] = "checking-your-state"
    summary: ClassVar[str] = "Render today's daily brief by running the digester sub-agents."
    use_when: ClassVar[str] = (
        "Use when the operator asks to build or refresh the daily brief. "
        "Writes one file to memory-store/daily/briefs/, off stores this "
        "machine already holds."
    )
    not_when: ClassVar[str] = (
        "to read a brief that already exists, use `brief_read` instead, because it "
        "has no side effects."
    )
    depends_on: ClassVar[str] = ""

    def __init__(
        self,
        *,
        adapter: ModelAdapter | None = None,
        adapter_options: AdapterOptions | None = None,
        memory_store: MemoryStore | None = None,
        agents_dir: Path | None = None,
        briefs_dir: Path | None = None,
        event_store: "object | None" = None,
        vault_wiki_dir: Path | None = None,
    ) -> None:
        # Late-bind TESSERACT_HOME at constructor call time so a process
        # that toggles the env var post-import (test harness, alt-home
        # boot) still routes writes to the operator-chosen home. Capturing
        # the import-time constant instead would disagree with
        # brief_render._resolve_briefs_dir, which late-binds.
        home = Path(os.environ.get("TESSERACT_HOME") or TESSERACT_HOME).resolve()
        self._adapter = adapter
        self._adapter_options = adapter_options or AdapterOptions()
        self._memory_store = memory_store
        # `None` means the live pair of agent roots: the digester
        # cards are shipped, so they resolve out of the app tree unless the
        # operator shadows one. Naming a directory here restricts the load
        # to it, which is what the tests want and production does not.
        self._agents_dir = agents_dir
        self._briefs_dir = briefs_dir or (home / "memory-store" / "daily" / "briefs")
        self._event_store = event_store
        self._vault_wiki_dir = vault_wiki_dir or (home / "vault" / "wiki")
        self._home = home

    @property
    def name(self) -> str:
        return "brief_render"

    @property
    def input_schema(self) -> type[BaseModel]:
        return BriefRenderInput

    def is_concurrency_safe(self) -> bool:
        return False

    def check_permissions(self, tool_input: BaseModel, context: ToolContext) -> PermissionResult:
        return PermissionResult.PASSTHROUGH

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp: BriefRenderInput = tool_input  # type: ignore[assignment]
        target = _parse_target_date(inp.date)
        if target is None:
            return ToolResult(
                output=f"invalid date {inp.date!r}: expected YYYY-MM-DD",
                is_error=True,
            )

        renderer = BriefRenderer(
            briefs_dir=self._briefs_dir,
            invoke_digester=_make_digester_invoker(
                self._adapter, self._adapter_options, self._agents_dir,
            ),
            memory_store=self._memory_store,
            event_store=self._event_store,
            vault_wiki_dir=self._vault_wiki_dir,
            home=self._home,
        )
        try:
            result = await renderer.render(target, overwrite=inp.overwrite)
        except Exception as exc:  # noqa: BLE001
            logger.exception("brief_render failed")
            return ToolResult(output=f"brief_render failed: {exc!r}", is_error=True)

        if result.skipped_existing:
            return ToolResult(
                output=(
                    f"brief for {target.isoformat()} already exists at "
                    f"{result.path}; pass overwrite=true to replace."
                ),
                metadata={"path": str(result.path), "skipped_existing": True},
            )

        return ToolResult(
            output=(
                f"brief rendered for {target.isoformat()} → {result.path} "
                f"(sections: {', '.join(result.sections_rendered) or 'all empty'})"
            ),
            metadata={
                "path": str(result.path),
                "sections_rendered": result.sections_rendered,
                "sections_dropped": result.sections_dropped,
                "memory_id": result.memory_id,
                "workspace_event_id": result.workspace_event_id,
            },
        )


def _parse_target_date(raw: str) -> date | None:
    stripped = raw.strip()
    if not stripped:
        # The operator's today. Rendering "the brief" at 23:00 in +02:00
        # used to file it under tomorrow.
        return clock.today()
    try:
        return date.fromisoformat(stripped)
    except ValueError:
        return None


def _make_digester_invoker(
    adapter: ModelAdapter | None,
    options: AdapterOptions,
    agents_dir: Path | None,
):
    """Return an ``invoke_digester(name, payload)`` coroutine that loads
    the agent's Role/Inputs/Rules sections as a system prompt and calls
    the configured adapter. Mirrors :mod:`provider_watch`'s direct-adapter
    pattern so the renderer works in scheduler contexts where
    ``invoke_agent`` is not wired.
    """
    async def _invoke(name: str, payload: dict) -> str:
        if adapter is None:
            return ""
        system_prompt = _load_agent_system_prompt(name, agents_dir)
        if not system_prompt:
            return ""
        from tesseract.agents.invocations import record as _record_invocation

        _record_invocation(name, via="brief_render")
        body = _format_payload_for_prompt(payload)
        prompt = f"{system_prompt}\n\n---\n\nPayload:\n{body}\n\n---\n\nProduce your section now. Markdown body only, no preamble."
        try:
            text = await adapter.generate(prompt, options)
        except Exception as exc:  # noqa: BLE001
            logger.warning("brief: adapter call for %s failed (%s)", name, exc)
            return ""
        return (text or "").strip()

    return _invoke


def _load_agent_system_prompt(name: str, agents_dir: Path | None) -> str:
    try:
        agent = load_agent(name, agents_dir=agents_dir)
    except FileNotFoundError:
        logger.warning(
            "brief: agent %r not found under %s", name,
            agents_dir or "the shipped and operator agent roots",
        )
        return ""
    sections = ["Role", "Inputs", "Output structure", "Rules", "Anti-output"]
    parts: list[str] = []
    for section in sections:
        body = agent.get_section(section)
        if body:
            parts.append(f"## {section}\n{body}")
    return "\n\n".join(parts)


def _format_payload_for_prompt(payload: dict) -> str:
    import json
    try:
        return json.dumps(payload, indent=2, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        return repr(payload)


__all__ = ["BriefRenderTool", "BriefRenderInput"]

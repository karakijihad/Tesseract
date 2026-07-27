"""agent_promote tool — move a quarantined agent into the active set.

W7-A (audit M6 follow-up, 2026-04-29). `agent_create` writes new agents
under `tesseract/agents/pending/` so they cannot be invoked even if the
ASK gate is somehow bypassed. `agent_promote` is the explicit operator
action that moves a pending agent into the active directory and appends
its row to `INDEX.md`. Returns ASK so the executor consults the operator
even if a posture override would otherwise auto-allow.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import ClassVar, Literal, Optional

from pydantic import BaseModel, Field

from tesseract.agents.loader import (
    AgentDefinition,
    list_agents,
    list_pending_agents,
    load_agent,
)
from tesseract.kernel.tools.base import PermissionResult, Tool, ToolContext, ToolResult
from tesseract.workspace_events import EventStore

logger = logging.getLogger(__name__)


def promote_pending_agent(
    agents_dir: Path, name: str,
) -> tuple[AgentDefinition | None, str | None]:
    """Move ``pending/{name}.md`` into the active set + append the INDEX row.

    Stage 10 — the single promotion implementation shared by the
    `agent_promote` chat tool and the Workspace proposal card's approve
    route. Returns ``(loaded, None)`` on success, ``(None, error)`` on
    failure. The move is rolled back if the INDEX append fails so state
    stays clean.
    """
    pending = list_pending_agents(agents_dir)
    if name not in pending:
        return None, (
            f"No pending agent {name!r} in {agents_dir / 'pending'}. "
            f"Pending: {pending or '(none)'}"
        )

    if name in list_agents(agents_dir):
        return None, (
            f"Agent {name!r} already active. Remove it first if you "
            "want to replace it with the pending version."
        )

    # Load the pending agent to validate it parses cleanly before we move
    # anything. A malformed pending agent should fail loudly here rather
    # than become a half-promoted active file.
    try:
        loaded = load_agent(name, agents_dir=agents_dir, include_pending=True)
    except (FileNotFoundError, RuntimeError) as exc:
        return None, f"Pending agent {name!r} failed validation: {exc}"

    src = agents_dir / "pending" / f"{name}.md"
    dst = agents_dir / f"{name}.md"
    index_path = agents_dir / "INDEX.md"

    try:
        os.replace(str(src), str(dst))
    except OSError as exc:
        return None, f"Failed to promote {name!r}: {exc}"

    try:
        _append_index_row(index_path, loaded.name, loaded.model_role, loaded.description)
    except OSError as exc:
        # Roll back: move the file back to pending so state is clean.
        try:
            os.replace(str(dst), str(src))
        except OSError:
            pass
        return None, f"INDEX.md update failed; promotion rolled back: {exc}"

    logger.info("Agent promoted: %s (%s)", name, dst)
    return loaded, None


class AgentPromoteInput(BaseModel):
    name: str = Field(description="Slug of the pending agent to promote.")
    kind: Literal["tool", "agent"] = Field(
        default="agent",
        description=(
            "Promotion kind. 'agent' (default) runs the agents/pending → active "
            "flow. 'tool' is rejected — tools are now built via delegation and "
            "promoted by hand by the operator, not through this tool."
        ),
    )
    # MO-8-6 deviation from phase-MO-8-6 §2 (which spec'd default="tool"):
    # defaulting to "agent" preserves backward compatibility with every
    # existing call site (`AgentPromoteInput(name=...)`); flipping the
    # default would silently break every legacy promote test on first call.


class AgentPromoteTool(Tool):
    default_posture = "ask"

    risk_class: ClassVar[str] = "operator_gate"

    def __init__(self, agents_dir: Path, event_store: Optional[EventStore] = None) -> None:
        """``event_store`` (Stage 10) lets a chat-side promotion settle any
        open `agent_approval` proposal card for the same agent, so the two
        approval surfaces (tool + Workspace card) converge. Optional —
        REPL/tests without a store skip the settle."""
        self._agents_dir = agents_dir
        self._event_store = event_store

    @property
    def name(self) -> str:
        return "agent_promote"

    @property
    def description(self) -> str:
        return (
            "Promote a quarantined agent from agents/pending/ to the active "
            "set. Required before a newly created agent becomes invokable. "
            "Operator-approval-gated."
        )

    @property
    def input_schema(self) -> type[BaseModel]:
        return AgentPromoteInput

    def is_read_only(self) -> bool:
        return False

    def is_concurrency_safe(self) -> bool:
        return False

    def check_permissions(self, tool_input: BaseModel, context: ToolContext) -> PermissionResult:
        # Short-circuit `kind="tool"` to DENY so the executor surfaces the
        # error message in `run` without first prompting the operator for
        # approval on a request that will immediately fail.
        inp = (
            tool_input
            if isinstance(tool_input, AgentPromoteInput)
            else AgentPromoteInput(**tool_input.model_dump())
        )
        if inp.kind == "tool":
            return PermissionResult.DENY
        return PermissionResult.ASK

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp = (
            tool_input
            if isinstance(tool_input, AgentPromoteInput)
            else AgentPromoteInput(**tool_input.model_dump())
        )

        if inp.kind == "tool":
            return ToolResult(
                output=(
                    "agent_promote does not handle tool promotions. "
                    "New tools are built via delegation and placed/promoted "
                    "by the operator by hand."
                ),
                is_error=True,
            )

        loaded, err = promote_pending_agent(self._agents_dir, inp.name)
        if err is not None:
            return ToolResult(output=err, is_error=True)

        # Stage 10 — settle any open proposal card for this agent so the
        # Workspace Inbox doesn't keep offering a promotion that already
        # happened chat-side. Best-effort: a store failure must not fail
        # a promotion that has already completed on disk.
        if self._event_store is not None:
            try:
                open_cards = [
                    ev for ev in self._event_store.list_events(
                        kinds=("agent_approval",), status="pending",
                    )
                    if (ev.payload or {}).get("name") == inp.name
                ]
                for ev in open_cards:
                    self._event_store.update_event_status(
                        ev.event_id, "applied",
                        reason="promoted via agent_promote",
                    )
            except Exception:
                logger.warning(
                    "agent_promote: card settle failed for %s",
                    inp.name, exc_info=True,
                )

        dst = self._agents_dir / f"{inp.name}.md"
        logger.info("Agent promoted: %s (%s)", inp.name, dst)
        return ToolResult(
            output=(
                f"Promoted agent: {inp.name}\n"
                f"Active path: {dst}\n"
                "It is now invokable through `invoke_agent`."
            )
        )


def _append_index_row(index_path: Path, name: str, model_role: str, description: str) -> None:
    """Append a new row to the INDEX.md agent registry table."""
    current = index_path.read_text(encoding="utf-8") if index_path.exists() else ""
    safe_desc = (description or "").replace("\n", " ").strip()
    new_row = f"| {name} | {model_role} | {safe_desc} |"
    if current.strip():
        updated = current.rstrip("\n") + "\n" + new_row + "\n"
    else:
        updated = new_row + "\n"
    tmp_path = index_path.with_suffix(".md.tmp")
    tmp_path.write_text(updated, encoding="utf-8")
    os.replace(str(tmp_path), str(index_path))

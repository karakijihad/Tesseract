"""agent_create tool — propose a new markdown sub-agent under tesseract/agents/.

The entity (or Claude during dev sessions) drafts a new specialist agent with
a rationale. The tool validates the draft; attended sessions still ASK before
the write.

Approval contract (audit M6, 2026-04-29; amended Stage 10, 2026-07-16):
`check_permissions` returns ASK unconditionally, so `permissions.yaml`
overrides like `headless.agent_create: auto` never reach the policy layer.
Attended sessions route the ASK to the operator as before. Unattended (no
`ask_fn`), the executor's Stage 10 quarantine-write carve-out
(`headless_quarantine_write` ClassVar, honored by `permissions/decide.py`
from the CLASS only — kernel-owned source, not yaml) lets the call proceed
because the only write target is the uninvokable quarantine below. The
operator gate now sits at ACTIVATION: `agent_promote` or the Workspace
proposal card. Headless creates are additionally capped by
`runtime.yaml::agent_pending_cap` and blocked for names the operator
already rejected (`agents/rejected/`).

Every successful create (attended or headless) files an `agent_approval`
WorkspaceEvent — the operator's proposal card in the Mirror Inbox, carrying
the rendered markdown + rationale. The pending file is canonical; the card
is best-effort (a card failure warns TARS in the tool output, never loses
the file).

Quarantine (W7-A, 2026-04-29): the new agent is written to
`agents/pending/{name}.md`, NOT directly to `agents/{name}.md`. The
default `loader.list_agents()` does not surface pending agents, so even
if the ASK gate is somehow bypassed (operator error, future refactor),
generated agents are not callable until `agent_promote` moves them into
the active directory. Defense in depth around audit M6.

Use when:
- "I need a specialist for X"
- "We should create a [role] agent"
- "This task would benefit from a persistent expert in Y"

Writes: tesseract/agents/pending/{name}.md (quarantine) + an agent_approval
Workspace event. Check mode only for existing agents — never edits or
deletes.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Callable, ClassVar, Literal, Optional

from pydantic import BaseModel, Field

from tesseract.agents.loader import (
    AgentDefinition,
    list_agents,
    list_pending_agents,
    list_rejected_agents,
    load_agent,
)
from tesseract.config.runtime_limits import (
    default_runtime_config_path,
    load_agent_pending_cap,
)
from tesseract.kernel.tools.base import PermissionResult, Tool, ToolContext, ToolResult
from tesseract.orchestrator.background_event_bus import get_background_bus
from tesseract.workspace_events import EventStore, WorkspaceEvent
from tesseract.workspace_events.broadcast import broadcast_workspace_event

logger = logging.getLogger(__name__)

_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,31}$")
_PROVIDER_REF_RE = re.compile(r"^(api|cli|local)\.[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")


def _is_provider_ref(value: str) -> bool:
    return bool(_PROVIDER_REF_RE.match(value or ""))

# Columns in agents/INDEX.md — used to format the appended row.
_INDEX_HEADER = "| name | model_role | description |"


class AgentCreateInput(BaseModel):
    name: str = Field(
        description="Slug-style name, lowercase, hyphen-separated. Must be unique. 2–32 chars."
    )
    model_role: str = Field(
        description=(
            "Either a role name from roles.yaml (e.g. agents_default, chat_brain) "
            "or a provider-model ref from providers.yaml of shape "
            "<tier>.<provider>.<model_id> (e.g. api.openai.gpt54_nano). Must exist."
        )
    )
    description: str = Field(
        description="One-line human description used in INDEX.md and agent frontmatter."
    )
    role_body: str = Field(
        description="The '## Role' section body — system-prompt style stance description."
    )
    prompt_sections: dict[str, str] = Field(
        description=(
            "Additional named sections emitted under ## headers "
            "(e.g. {'Check Prompt': '...'}). At least one required. "
            "Do not include a 'Role' key — use role_body instead."
        )
    )
    rationale: str = Field(
        description="Why this agent is needed — shown to user at approval time. Required."
    )
    max_tokens_override: int | None = Field(default=None)
    version: str = Field(default="0.1")
    proposer: Literal["entity", "claude", "codex", "user"] = Field(
        default="entity",
        description="Who is proposing this agent — recorded for review.",
    )


class AgentCreateTool(Tool):
    default_posture: ClassVar[str] = "ask"
    risk_class: ClassVar[str] = "propose"
    # Stage 10 — decide.py quarantine-write carve-out: unattended calls may
    # proceed because every write lands in agents/pending/ (uninvokable
    # until agent_promote / the Workspace card). Class-level on purpose:
    # decide.py reads `type(tool)`, so neither permissions.yaml nor an
    # instance attribute can flip it.
    headless_quarantine_write: ClassVar[bool] = True

    def __init__(
        self,
        agents_dir: Path,
        models_config: dict,
        event_store: Optional[EventStore] = None,
        *,
        app_provider: Optional[Callable[[], Any]] = None,
    ) -> None:
        """``event_store`` receives the `agent_approval` proposal event;
        ``app_provider`` resolves the Mirror app at call time so the card
        broadcasts live to open Workspace tabs (same contract as
        `workspace_post`). Both optional — REPL/tests stay write-only."""
        self._agents_dir = agents_dir
        self._models_config = models_config
        self._event_store = event_store
        self._app_provider = app_provider

    @property
    def name(self) -> str:
        return "agent_create"

    @property
    def description(self) -> str:
        return (
            "Propose a new markdown sub-agent under tesseract/agents/. "
            "Attended: operator approves before the write. Unattended: the "
            "draft lands in the agents/pending/ quarantine and a proposal "
            "card is filed in the operator's Workspace Inbox — never "
            "invokable until promoted. Never edits or deletes existing "
            "agents."
        )

    @property
    def input_schema(self) -> type[BaseModel]:
        return AgentCreateInput

    def is_read_only(self) -> bool:
        return False

    def is_concurrency_safe(self) -> bool:
        return False

    def check_permissions(self, tool_input: BaseModel, context: ToolContext) -> PermissionResult:
        # Always ASK — the executor handles the no-ask_fn case as DENY for
        # non-read-only tools. Returning PASSTHROUGH would let
        # `permissions.yaml` headless overrides (`agent_create: auto`)
        # bypass the operator-approval invariant. Audit M6: 2026-04-29.
        return PermissionResult.ASK

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp = (
            tool_input
            if isinstance(tool_input, AgentCreateInput)
            else AgentCreateInput(**tool_input.model_dump())
        )

        # --- Validation (before any write) ---

        if not _NAME_RE.match(inp.name):
            return ToolResult(
                output=(
                    f"Invalid agent name {inp.name!r}. "
                    "Must be lowercase, start with a letter, hyphens allowed, 2–32 chars."
                ),
                is_error=True,
            )

        if inp.name in list_agents(self._agents_dir):
            return ToolResult(
                output=f"Agent {inp.name!r} already exists in {self._agents_dir}.",
                is_error=True,
            )
        if inp.name in list_pending_agents(self._agents_dir):
            return ToolResult(
                output=(
                    f"Agent {inp.name!r} already pending promotion in "
                    f"{self._agents_dir}/pending/. Promote or remove it first."
                ),
                is_error=True,
            )
        if inp.name in list_rejected_agents(self._agents_dir):
            reason = _read_rejection_reason(self._agents_dir, inp.name)
            return ToolResult(
                output=(
                    f"Agent {inp.name!r} was previously rejected by the operator"
                    + (f": {reason}" if reason else ".")
                    + " Address the rejection before re-proposing, or pick a "
                    "different name."
                ),
                is_error=True,
            )

        # Stage 10 flood guard — UNATTENDED proposals are capped by
        # runtime.yaml::agent_pending_cap so a headless loop can't fill the
        # quarantine. Attended creates went through the operator's ASK and
        # stay uncapped.
        if context.ask_fn is None:
            cap = load_agent_pending_cap(default_runtime_config_path())
            pending_now = len(list_pending_agents(self._agents_dir))
            if pending_now >= cap:
                return ToolResult(
                    output=(
                        f"agents/pending/ already holds {pending_now} proposals "
                        f"(cap {cap}). Ask the operator to review the open "
                        "proposal cards before proposing more agents."
                    ),
                    is_error=True,
                )

        # `model_role` accepts either a role name from roles.yaml (e.g.
        # `chat_brain`, `agents_default`) OR a provider-model reference of
        # shape `<tier>.<provider>.<model_id>` (e.g. `api.openai.gpt54_nano`)
        # so cheap-model selection per agent is one frontmatter line, not a
        # whole new role.
        valid_roles = set(self._models_config.get("roles", {}).keys())
        if not _is_provider_ref(inp.model_role) and inp.model_role not in valid_roles:
            return ToolResult(
                output=(
                    f"Unknown model_role {inp.model_role!r}. "
                    f"Valid roles: {sorted(valid_roles)} — "
                    "or a provider-model ref like `api.openai.gpt54_nano`."
                ),
                is_error=True,
            )

        if not inp.prompt_sections:
            return ToolResult(
                output="prompt_sections must contain at least one section.",
                is_error=True,
            )

        if "Role" in inp.prompt_sections:
            return ToolResult(
                output=(
                    "Do not include 'Role' in prompt_sections — use the role_body field instead. "
                    "The ## Role section is always the first section of every agent."
                ),
                is_error=True,
            )

        # --- Render + round-trip validation ---
        rendered = _render_agent_markdown(inp)

        roundtrip_error = _validate_roundtrip(rendered, inp)
        if roundtrip_error:
            return ToolResult(
                output=f"Rendered markdown failed loader round-trip: {roundtrip_error}",
                is_error=True,
            )

        # --- Atomic write to quarantine ---
        # W7-A: agents land in pending/ first; agent_promote moves them
        # to active. INDEX.md is the active-agent registry — we don't
        # touch it for pending agents.
        pending_dir = self._agents_dir / "pending"
        pending_dir.mkdir(parents=True, exist_ok=True)
        agent_path = pending_dir / f"{inp.name}.md"

        try:
            _atomic_write(agent_path, rendered)
        except OSError as exc:
            return ToolResult(output=f"Failed to write agent file: {exc}", is_error=True)

        # Publish on the background bus for any subscriber tracking agent
        # lifecycle events. Best-effort: a bus failure must not break
        # agent_create.
        try:
            get_background_bus().publish(
                "AgentCreated",
                {"agent": inp.name, "proposer": inp.proposer},
            )
        except Exception:
            logger.warning("agent_create: bus publish failed for %s", inp.name, exc_info=True)

        # Stage 10 — file the proposal card. The pending write above is
        # canonical; the card is best-effort (memory-write pattern: never
        # let a notification failure lose the artifact).
        card_note = ""
        if self._event_store is not None:
            event = WorkspaceEvent.new(
                kind="agent_approval",
                source="tars",
                title=f"Agent proposal: {inp.name}",
                summary=inp.rationale,
                payload={
                    "name": inp.name,
                    "model_role": inp.model_role,
                    "description": inp.description,
                    "rationale": inp.rationale,
                    "proposer": inp.proposer,
                    "rendered_markdown": rendered,
                    "session_id": context.session_id,
                },
            )
            try:
                self._event_store.append_event(event)
                card_note = (
                    f"\nProposal card filed in the Workspace Inbox "
                    f"({event.event_id})."
                )
            except Exception:
                logger.exception(
                    "agent_create: proposal event failed for %s", inp.name,
                )
                card_note = (
                    "\nWARNING: the proposal card could not be filed in the "
                    "Workspace Inbox — post a workspace_post note so the "
                    "operator knows this agent is pending."
                )
            else:
                # Live broadcast so open Workspace tabs re-render without a
                # refresh. Fail-soft; the on-disk event is already durable.
                try:
                    if self._app_provider is not None:
                        app = self._app_provider()
                        if app is not None:
                            await broadcast_workspace_event(app, event)
                except Exception:
                    logger.warning(
                        "agent_create: card broadcast failed for %s",
                        inp.name, exc_info=True,
                    )

        sections = ["Role"] + list(inp.prompt_sections.keys())
        logger.info("Agent created (pending): %s (%s)", inp.name, agent_path)
        return ToolResult(
            output=(
                f"Created agent (pending promotion): {inp.name}\n"
                f"File: {agent_path}\n"
                f"Sections: {sections}\n\n"
                "The agent is quarantined — it cannot be invoked until the "
                "operator promotes it (`agent_promote` or the Workspace "
                "proposal card)." + card_note
            )
        )


# ─── Helpers ─────────────────────────────────────────────


def _read_rejection_reason(agents_dir: Path, name: str) -> str:
    """Operator's reason from the reject sidecar, empty string if absent.
    Written by the Workspace reject route next to rejected/{name}.md."""
    reason_path = agents_dir / "rejected" / f"{name}.reason.txt"
    try:
        return reason_path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _render_agent_markdown(inp: AgentCreateInput) -> str:
    """Render the full .md content for an agent definition. Pure function."""
    lines: list[str] = ["---"]
    lines.append(f"name: {inp.name}")
    lines.append(f'version: "{inp.version}"')
    lines.append(f"model_role: {inp.model_role}")
    if inp.max_tokens_override is not None:
        lines.append(f"max_tokens_override: {inp.max_tokens_override}")
    lines.append("description: >")
    for line in inp.description.splitlines():
        lines.append(f"  {line}")
    lines.append("---")
    lines.append("")
    lines.append("## Role")
    lines.append("")
    lines.append(inp.role_body.strip())
    lines.append("")
    for section_name, body in inp.prompt_sections.items():
        lines.append(f"## {section_name}")
        lines.append("")
        lines.append(body.strip())
        lines.append("")
    return "\n".join(lines)


def _validate_roundtrip(rendered: str, inp: AgentCreateInput) -> str | None:
    """Write rendered markdown to a temp directory, load via load_agent, check sections.

    Returns an error message string if validation fails, None if all sections
    round-trip correctly. This catches accidental '## ' inside section bodies
    (which loader.py would interpret as new top-level sections).
    """
    expected_sections = {"Role"} | set(inp.prompt_sections.keys())
    tmp_dir = Path(tempfile.mkdtemp())
    tmp_agent_path = tmp_dir / f"{inp.name}.md"
    try:
        tmp_agent_path.write_text(rendered, encoding="utf-8")
        agent: AgentDefinition = load_agent(inp.name, agents_dir=tmp_dir)
        actual_sections = set(agent.sections.keys())
        missing = expected_sections - actual_sections
        unexpected = actual_sections - expected_sections
        if missing:
            return (
                f"Sections lost in loader round-trip: {sorted(missing)}. "
                "Check that no section body starts with a '## ' line — "
                "the loader would parse it as a new top-level section."
            )
        if unexpected:
            return (
                f"Unexpected sections in loader round-trip: {sorted(unexpected)}. "
                "A '## ' line inside a section body was parsed as a new top-level section. "
                "Use '### ' for sub-headers inside section bodies."
            )
        return None
    except Exception as exc:
        return str(exc)
    finally:
        try:
            tmp_agent_path.unlink(missing_ok=True)
            tmp_dir.rmdir()
        except OSError:
            pass


def _atomic_write(path: Path, content: str) -> None:
    """Write content to path atomically via a .tmp intermediate."""
    tmp_path = path.with_suffix(".md.tmp")
    tmp_path.write_text(content, encoding="utf-8")
    os.replace(str(tmp_path), str(path))


def description_for_approval(inp: AgentCreateInput) -> str:
    """Return the input_summary for the permission engine approval prompt."""
    rendered = _render_agent_markdown(inp)
    return (
        f"Proposer: {inp.proposer}\n"
        f"Rationale: {inp.rationale}\n\n"
        f"--- Rendered agent markdown ---\n\n{rendered}"
    )

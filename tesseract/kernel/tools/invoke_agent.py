"""invoke_agent tool — call a markdown-defined sub-agent.

Loads an agent definition from tesseract/agents/, composes a system prompt
from its sections, spins up a bounded ChatSession, and returns the final
text. Sub-agent gets a filtered, read-only subset of the parent registry by
default (overridable via frontmatter `tools:` list). All tool calls the
sub-agent makes flow through the parent's permission policy — so ASK
prompts still fire.

Scope (minimum viable):
- Sub-agent shares the parent's adapter + model. Agents whose `model_role`
  is `claude_cli` / `codex_cli` are rejected with guidance to use
  `delegate_coder` / `delegate_auditor` instead.
- Iteration / breaker caps come from the parent's chat_brain config
  (`roles.yaml::roles.chat_brain.{tool_iteration_cap, consecutive_error_cap}`)
  so the operator-tunable Loop Limits panel applies to sub-agents too. No
  compaction in the sub-session.
- Output is the concatenated assistant text from the sub-session.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import BaseModel, Field

from tesseract.agents.loader import AgentDefinition, list_agents, load_agent
from tesseract.brain.agent_factory import AgentBuildError, build_agent_session
from tesseract.brain.chat import ChatSession
from tesseract.brain.tools import AskFn, ToolRegistry
from tesseract.kernel.adapters.base import AdapterOptions, ChunkType, ModelAdapter
from tesseract.kernel.tools.base import (
    PermissionResult,
    SpawnCapExceeded,
    Tool,
    ToolContext,
    ToolResult,
    spawn_cap_tool_result,
)
from tesseract.permissions.policy import PermissionPolicy

if TYPE_CHECKING:
    from tesseract.brain.cost.ledger import CostLedger

logger = logging.getLogger(__name__)

# Default read-only tool subset a sub-agent inherits. Kept narrow on purpose:
# sub-agents should observe, analyze, and report — not write or mutate. The
# operator can override via the agent's frontmatter `tools:` list.
DEFAULT_TOOL_SUBSET = frozenset({
    "file_read",
    "glob",
    "grep",
    "pdf_read",
    "memory_search",
    "vault_search",
    "vault_query",
    "context7_lookup",
    "web_search",
    "tavily_search",
    "tavily_extract",
    # tool_search is read-only and REQUIRED for the extended-tier entries in
    # this subset (pdf_read, context7_lookup) — they ship no schema until a
    # tool_search call unlocks them, so without it a sub-agent structurally
    # cannot reach them (audit 2026-07-12).
    "tool_search",
    # The upward half of steering: a sub-agent that hit an ambiguity can ask
    # instead of guessing. Read-only in the sense that matters — it mutates
    # nothing, it waits.
    "agent_ask",
})

_SECTION_ORDER_HINT = ("Role", "Purpose", "Identity", "Rules", "When to Deploy")

# A steered sub-agent must still terminate. The tool-iteration cap bounds work
# WITHIN a turn; this bounds how many turns steering may add, so the module's
# "bounded ChatSession" contract survives an operator who keeps correcting.
_MAX_STEERS_PER_SPAWN = 12


def _question_timeout_s() -> float:
    """Seconds a parked `agent_ask` waits. Read at call time so the config
    watcher's reload applies without a restart."""
    from tesseract.config.runtime_limits import (
        default_runtime_config_path,
        load_agent_question_timeout_s,
    )

    return load_agent_question_timeout_s(default_runtime_config_path())


class _SteerBox:
    """The input channel that makes a sub-agent spawn steerable.

    A one-shot subprocess has no such channel — that is why `work_send`
    refuses one. A sub-agent runs a real turn loop, so a message can be
    handed to it between turns, and an in-flight turn can be cut short to
    make "between turns" happen now. That is the same cancel-and-resend
    semantic `work_send` already uses for interactive sessions.

    ``hard_cancel`` is kept separate from the per-turn event so a steer and
    a spawn_cancel cannot be confused: cancelling a turn to deliver a
    correction must not read as "the operator killed this spawn".
    """

    def __init__(self) -> None:
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.hard_cancel = asyncio.Event()
        self.turn_cancel: asyncio.Event | None = None
        # True only between a push() and the turn loop acting on it. Causation
        # must be recorded, never inferred from a non-empty queue: an external
        # cancellation racing a queued steer would otherwise be swallowed.
        self.steered_turn = False
        # Back-reference set right after `register()` returns. The task is
        # scheduled, not yet running, so the turn loop always sees it.
        self.handle: object | None = None

    def awaiting_answer(self) -> bool:
        """True when the sub-agent called `agent_ask` and nobody has replied."""
        return bool(getattr(self.handle, "question", None))

    def push(self, message: str) -> bool:
        """Queue a correction and cut the current turn short. Never raises —
        a failed steer is reported to the caller, not thrown at the spawn."""
        text = (message or "").strip()
        if not text or self.hard_cancel.is_set():
            return False
        self.queue.put_nowait(text)
        self.steered_turn = True
        if self.turn_cancel is not None:
            self.turn_cancel.set()
        return True

    def cancel(self) -> None:
        self.hard_cancel.set()
        if self.turn_cancel is not None:
            self.turn_cancel.set()

    def drain(self) -> str | None:
        """Every message queued since the last turn, oldest first."""
        parts: list[str] = []
        while True:
            try:
                parts.append(self.queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        return "\n\n".join(parts) if parts else None


class InvokeAgentInput(BaseModel):
    name: str = Field(description="Slug of a registered sub-agent (see AGENTS.md).")
    task: str = Field(
        description=(
            "Self-contained task prompt. The sub-agent has no access to this "
            "conversation — include every file path, constraint, and goal it needs."
        )
    )
    attachment_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Optional list of attachment IDs (image / PDF / audio) the operator "
            "uploaded in the current Mirror session. The sub-agent receives them "
            "as multipart message parts when its model supports them. Use this "
            "when delegating image questions to a vision agent."
        ),
    )
    model_role: str | None = Field(
        default=None,
        description=(
            "Override the model this agent runs on for THIS call only — a "
            "roles.yaml role name or a provider ref like 'api.<provider>."
            "<model>'. Leave unset to use the model the agent card declares. "
            "CLI-subscription roles are refused here; route those through a "
            "delegation seat instead."
        ),
    )
    background: bool = Field(
        default=True,
        description=(
            "Fire-and-track (default): runs the sub-agent as a background "
            "asyncio task and returns a spawn_handle immediately so the assistant "
            "can keep talking and dispatch other work. Use spawn_check / "
            "spawn_await to retrieve the result, or wait for the "
            "completion note. Pass false only when the very next step in "
            "THIS turn must consume the agent's answer inline."
        ),
    )


class InvokeAgentTool(Tool):
    default_posture = "ask"

    risk_class: ClassVar[str] = "propose"
    def __init__(
        self,
        agents_dir: Path,
        adapter: ModelAdapter,
        options: AdapterOptions,
        parent_registry: ToolRegistry,
        max_tool_iterations: int,
        max_consecutive_adapter_errors: int,
        tool_context: ToolContext | None = None,
        policy: PermissionPolicy | None = None,
        ask_fn: AskFn | None = None,
        cost_ledger: "CostLedger | None" = None,
    ) -> None:
        self._agents_dir = agents_dir
        self._adapter = adapter
        self._options = options
        self._parent_registry = parent_registry
        # Shared ledger so sub-agent ChatSessions bill spend + preflight the
        # daily cap — invoke_agent runs a real ChatSession which already meters;
        # it just needed the handle (2026-06-28 cost-ledger gap).
        self._cost_ledger = cost_ledger
        # Inherited from parent chat_brain config — single source of truth
        # is `roles.yaml::roles.chat_brain.{tool_iteration_cap,
        # consecutive_error_cap}` (see boot.ChatBrainConfig).
        self._max_tool_iterations = max_tool_iterations
        self._max_consecutive_adapter_errors = max_consecutive_adapter_errors
        # `tool_context` / `ask_fn` are constructor-time fallbacks for the
        # REPL where there is exactly one operator. The Mirror passes
        # `tool_context=None` and threads the per-session ToolContext +
        # ask_fn through `context` at run() time.
        self._tool_context = tool_context
        self._policy = policy
        self._ask_fn = ask_fn

    @property
    def name(self) -> str:
        return "invoke_agent"

    @property
    def description(self) -> str:
        return (
            "Dispatch a bounded task to a markdown sub-agent from tesseract/agents/. "
            "Sub-agent runs with a read-only tool subset and its own system prompt. "
            "Backgrounds by default (fire-and-track): returns a spawn_handle; "
            "retrieve the reply via spawn_check / spawn_await or the completion "
            "note. Pass background=false to await the final text inline. Use when a "
            "task benefits from a persistent specialist stance (reviewer, planner, "
            "domain expert). For heavy CLI work use delegate_coder / delegate_auditor."
        )

    @property
    def input_schema(self) -> type[BaseModel]:
        return InvokeAgentInput

    def is_read_only(self) -> bool:
        return False  # sub-agent can call read-only tools, but dispatch itself is side-effecting

    def is_concurrency_safe(self) -> bool:
        return False

    def check_permissions(self, tool_input: BaseModel, context: ToolContext) -> PermissionResult:
        return PermissionResult.PASSTHROUGH

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        inp = (
            tool_input
            if isinstance(tool_input, InvokeAgentInput)
            else InvokeAgentInput(**tool_input.model_dump())
        )

        # Phase 4: background spawn. Same pattern as delegate_*.
        # background is the default; a context without a
        # SpawnRegistry (headless / REPL / autonomy) degrades to foreground
        # instead of erroring so callers keep the pre-P3 semantics.
        registry = getattr(context, "spawns", None)
        if inp.background and registry is not None:
            fg_input = InvokeAgentInput(
                name=inp.name,
                task=inp.task,
                attachment_ids=list(inp.attachment_ids),
                model_role=inp.model_role,
                background=False,
            )
            # Reviewer finding (2026-07-09): the sub-agent's read-only tools
            # (file_read / glob / grep / pdf_read) raise CancelledError when
            # context.cancel_event is set — and the session-lifetime event is
            # set by the Stop button of ANY later turn. A detached spawn gets
            # its own event (same precedent as fork_for_synthetic);
            # spawn_cancel still reaches it via cancel_fn.
            # The box owns the per-turn event; the context gets it too so
            # tools already running see a steer as a turn boundary.
            steerbox = _SteerBox()
            steerbox.turn_cancel = asyncio.Event()
            spawn_context = dataclasses.replace(
                context, cancel_event=steerbox.turn_cancel
            )
            try:
                handle = registry.register(
                    kind=f"invoke_agent:{inp.name}",
                    goal=inp.task,
                    coro=self._run_foreground(fg_input, spawn_context, steerbox),
                    cancel_fn=steerbox.cancel,
                    steer_fn=steerbox.push,
                )
            except SpawnCapExceeded as exc:
                return spawn_cap_tool_result(exc)
            steerbox.handle = handle
            return ToolResult(
                output=(
                    f"invoke_agent({inp.name}) spawned in background: handle="
                    f"{handle.handle_id}. Use spawn_check or spawn_await "
                    f"to retrieve the result, or work_send to steer it."
                ),
                metadata={
                    "spawn_handle": handle.handle_id,
                    "spawn_kind": f"invoke_agent:{inp.name}",
                    "started_at": handle.started_at,
                    "status": "running",
                    "steerable": True,
                },
            )

        return await self._run_foreground(inp, context)

    async def _run_foreground(
        self,
        inp: InvokeAgentInput,
        context: ToolContext,
        steerbox: "_SteerBox | None" = None,
    ) -> ToolResult:
        # Per-session ToolContext + ask_fn travel through `context` (Mirror).
        # Fall back to constructor values when they are absent (REPL).
        sub_tool_context = context or self._tool_context or ToolContext()
        sub_ask_fn = (context.ask_fn if context is not None else None) or self._ask_fn

        try:
            sub_session = build_agent_session(
                name=inp.name,
                agents_dir=self._agents_dir,
                parent_adapter=self._adapter,
                parent_options=self._options,
                parent_registry=self._parent_registry,
                max_tool_iterations=self._max_tool_iterations,
                max_consecutive_adapter_errors=self._max_consecutive_adapter_errors,
                tool_context=sub_tool_context,
                policy=self._policy,
                ask_fn=sub_ask_fn,
                cost_ledger=self._cost_ledger,
                model_role=inp.model_role,
            )
        except AgentBuildError as exc:
            return ToolResult(output=str(exc), is_error=True)

        # Operator visibility — name the agent + the *resolved* primary
        # model so the chat surface shows what's happening rather than a
        # silent pause. Status emit is best-effort; never fail the call.
        sub_options = sub_session.options  # type: ignore[attr-defined]
        if context is not None and context.status_emit is not None:
            try:
                await context.status_emit(
                    f"delegating to {inp.name} ({sub_options.model})…"
                )
            except Exception:  # noqa: BLE001
                logger.debug("invoke_agent: status_emit failed", exc_info=True)

        # Build the user message — multipart when attachments are supplied so
        # vision-capable sub-agents see the image. Falls back to plain text.
        user_input: Any = inp.task
        if inp.attachment_ids:
            user_input = await _build_multipart_user_input(
                inp.task, inp.attachment_ids, sub_tool_context.session_id,
            )

        # Reload agent metadata for the output record (model_role).
        try:
            _agent_meta = load_agent(inp.name, agents_dir=self._agents_dir)
            # Report what this run actually used, not what the card declares.
            _model_role = (inp.model_role or "").strip() or _agent_meta.model_role
        except FileNotFoundError:
            _model_role = ""

        collected: list[str] = []
        tool_calls = 0
        iterations = 0
        stop_reason = ""
        error: str | None = None
        steers_applied = 0

        # One pass per turn. Without a steer box there is exactly one, which
        # is the pre-steering behaviour. With one, a queued correction starts
        # another turn instead of letting the sub-agent finish — and a turn
        # cut short to deliver that correction is NOT an error, so the
        # CancelledError it raises is swallowed here and nowhere else.
        turn_input: Any = user_input
        while True:
            if steerbox is not None:
                steerbox.steered_turn = False
            try:
                async for chunk in sub_session.send(turn_input):
                    if chunk.type == ChunkType.TEXT:
                        collected.append(chunk.text)
                    elif chunk.type == ChunkType.TOOL_CALL_END:
                        tool_calls += 1
                    elif chunk.type == ChunkType.STOP:
                        stop_reason = chunk.stop_reason
                        iterations += 1
                    elif chunk.type == ChunkType.ERROR:
                        error = chunk.error
                        break
            except asyncio.CancelledError:
                # A hard cancel is the operator's and must propagate; a steer
                # only ends the turn so the correction can be delivered.
                if steerbox is None or steerbox.hard_cancel.is_set():
                    raise
                if not steerbox.steered_turn:
                    raise
            except Exception as e:  # surface, don't hide
                logger.exception("invoke_agent: sub-session failed")
                error = f"{type(e).__name__}: {e}"

            if error or steerbox is None or steerbox.hard_cancel.is_set():
                break
            if steers_applied >= _MAX_STEERS_PER_SPAWN:
                stop_reason = "max_steers"
                logger.warning(
                    "invoke_agent(%s): steer cap %d reached; ending the run",
                    inp.name, _MAX_STEERS_PER_SPAWN,
                )
                break
            correction = steerbox.drain()
            if correction is None:
                if not steerbox.awaiting_answer():
                    break
                # The sub-agent asked its parent something. Ending here would
                # return a half-answer as if it were the result, so wait —
                # bounded only by spawn_cancel, which sets hard_cancel.
                waiter = asyncio.ensure_future(steerbox.queue.get())
                stopper = asyncio.ensure_future(steerbox.hard_cancel.wait())
                try:
                    await asyncio.wait(
                        {waiter, stopper},
                        timeout=_question_timeout_s(),
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    stopper.cancel()
                if not waiter.done():
                    waiter.cancel()
                    if steerbox.hard_cancel.is_set():
                        break
                    # Nobody answered. Resuming beats both hanging on a slot
                    # forever and discarding the work already done.
                    logger.warning(
                        "invoke_agent(%s): no answer to its question in %.0fs; "
                        "resuming on assumption", inp.name, _question_timeout_s(),
                    )
                    correction = (
                        "No answer came back in time. State the assumption you "
                        "are proceeding on, explicitly, and finish the task."
                    )
                else:
                    correction = waiter.result()
            steers_applied += 1
            # Do NOT swap the Event object. `build_agent_session` hands the
            # ChatSession a `copy.copy` of this context, so reassigning
            # `context.cancel_event` here would never reach the object the
            # sub-session actually reads — the first steer would interrupt and
            # every later one would not. `ChatSession.send()` clears the event
            # at the top of each turn, so reusing it is both correct and what
            # the substrate already expects.
            turn_input = (
                "The operator has redirected this task mid-flight. Apply this "
                f"and continue:\n\n{correction}"
            )

        final_text = "".join(collected).strip()

        if error:
            return ToolResult(
                output=(
                    f"[{inp.name}] sub-session errored: {error}\n"
                    f"Partial output ({len(final_text)} chars):\n{final_text}"
                    if final_text
                    else f"[{inp.name}] sub-session errored: {error}"
                ),
                is_error=True,
            )

        metadata = {
            "agent": inp.name,
            "model_role": _model_role,
            "steers_applied": steers_applied,
            "iterations": iterations,
            "tool_calls": tool_calls,
            "stop_reason": stop_reason,
        }
        return ToolResult(
            output=(
                f"[{inp.name} · {iterations} iter · {tool_calls} tool call(s) · {stop_reason or 'end'}]\n\n"
                f"{final_text or '(empty response)'}"
            ),
            metadata=metadata,
        )


async def _build_multipart_user_input(
    task: str,
    attachment_ids: list[str],
    session_id: str,
) -> list[dict[str, Any]] | str:
    """Resolve the operator's uploaded attachments by ID and emit OpenAI-style
    multipart message parts (`{type:"text"}` + `{type:"image"|"file"|"audio"}`).

    Late-imports the Mirror upload helpers so the `kernel/tools` package
    stays loadable in REPL/test contexts that haven't started the Mirror.
    Unknown / orphaned attachment IDs are silently skipped — the sub-agent
    sees only the parts it can actually use, and the task text is always
    included as the first part so the agent has something to ground on.
    """
    try:
        from tesseract.mirror.server.uploads import load_attachment
        from tesseract.mirror.server.uploads._storage import attachment_part_for_model
    except ImportError:
        # Mirror upload module not available — fall back to text-only.
        logger.warning(
            "invoke_agent: attachment_ids supplied but Mirror upload module "
            "unavailable; sub-agent receives task text only",
        )
        return task

    parts: list[dict[str, Any]] = []
    if task:
        parts.append({"type": "text", "text": task})
    for att_id in attachment_ids:
        att = load_attachment(session_id, att_id)
        if att is None:
            logger.info(
                "invoke_agent: attachment %s not found for session %s — skipping",
                att_id, session_id,
            )
            continue
        part = await attachment_part_for_model(att)
        if part is not None:
            parts.append(part)
    return parts if parts else task


_PROVIDER_REF_RE = re.compile(r"^(api|cli|local)\.[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")


def _is_provider_ref(model_role: str) -> bool:
    """True for the post-2026-04-30 provider-model reference shape."""
    return bool(_PROVIDER_REF_RE.match(model_role or ""))


def _is_cli_role(model_role: str) -> bool:
    """Match every shape that signals a CLI subscription model that
    `invoke_agent` cannot drive directly:
      * ``claude_cli`` / ``codex_cli``
      * ``cli_claude`` / ``cli_codex`` (Phase-1 agents)
      * ``cli.<provider>.<model>`` (post-2026-04-30 provider-ref shape)
    Each indicates the subscription CLI path; operator must use
    ``delegate_coder`` / ``delegate_auditor`` instead.
    """
    return bool(model_role) and (
        model_role.startswith("cli_")
        or model_role.endswith("_cli")
        or model_role.startswith("cli.")
    )


def _build_sub_registry(parent: ToolRegistry, agent: AgentDefinition) -> ToolRegistry:
    """Filter the parent registry to the subset this agent is allowed to use.

    Frontmatter `tools:` list (when present) overrides the default read-only
    subset. Unknown names are silently dropped — the sub-agent simply won't
    see them.
    """
    override = _tools_override_from_frontmatter(agent)
    allowed = override if override is not None else DEFAULT_TOOL_SUBSET

    sub = ToolRegistry()
    for tool_name, tool in parent.tools.items():
        if tool_name in allowed:
            sub.tools[tool_name] = tool
    return sub


def _tools_override_from_frontmatter(agent: AgentDefinition) -> frozenset[str] | None:
    """Read the agent's `tools:` frontmatter list.

    Returns None when the agent omits the field (sub-agent inherits
    DEFAULT_TOOL_SUBSET). Returns an empty frozenset when the agent
    explicitly declares `tools: []` (text-only sub-session).
    """
    if agent.tools is None:
        return None
    return frozenset(agent.tools)


def _compose_sub_system_prompt(agent: AgentDefinition, tool_names: list[str]) -> str:
    """Concatenate agent sections into a system prompt, with a tool roster footer."""
    header = f"# {agent.name}\n\n{agent.description.strip()}\n" if agent.description else f"# {agent.name}\n"

    # Sections: prefer known order, then append anything else.
    section_order = [name for name in _SECTION_ORDER_HINT if name in agent.sections]
    section_order += [name for name in agent.sections if name not in _SECTION_ORDER_HINT]

    body_parts: list[str] = [header]
    for section_name in section_order:
        body = agent.sections[section_name].strip()
        if not body:
            continue
        body_parts.append(f"## {section_name}\n\n{body}\n")

    if tool_names:
        body_parts.append(
            "## Tools you have\n\n"
            f"{', '.join(sorted(tool_names))}\n\n"
            "Tool calls flow through the operator's permission policy — some may "
            "prompt for approval. You have no write access by default."
        )
    else:
        body_parts.append(
            "## Tools you have\n\nNone — you're text-only for this task."
        )

    return "\n".join(body_parts).strip() + "\n"


def _sub_agent_options(parent: AdapterOptions, agent: AgentDefinition) -> AdapterOptions:
    """Clone the parent adapter options, applying agent's max_tokens_override.

    Kept for backward-compat; new code should use ``_resolve_sub_adapter``
    which returns the adapter alongside options (and honors ``model_role``).
    """
    override = agent.max_tokens_override
    if override is None or override <= 0:
        return parent
    # AdapterOptions is a frozen-ish dataclass — use a simple reconstruction.
    fields: dict[str, Any] = {
        "model": parent.model,
        "provider": parent.provider,
        "temperature": parent.temperature,
        "max_output_tokens": override,
        "context_window": parent.context_window,
        "reasoning_effort": parent.reasoning_effort,
    }
    return AdapterOptions(**fields)


def _resolve_sub_adapter(
    parent_adapter: ModelAdapter,
    parent_options: AdapterOptions,
    agent: AgentDefinition,
) -> tuple[ModelAdapter, AdapterOptions]:
    """Resolve the (adapter, options) pair for a sub-agent invocation.

    Codex audit 2026-05-19 P1 #3: agents declare ``model_role`` in their
    frontmatter, but ``_sub_agent_options`` historically only applied
    ``max_tokens_override`` — the sub-session always ran on the parent's
    chat_brain adapter regardless of the role declared. This helper:

    1. When ``agent.model_role`` is empty / ``chat_brain`` / equal to the
       parent's resolved role → return ``(parent_adapter, _sub_agent_options(...))``
       to preserve today's behaviour for the common case.
    2. When ``agent.model_role`` points at another active role with a
       buildable chain → build a role-specific ``FallbackAdapter`` and
       return its primary options. The sub-session truly runs on the
       declared model.
    3. When the declared role is missing / inactive / has no buildable
       entry → log a warning and fall back to the parent adapter so a
       misconfigured agent doesn't blank the turn.

    The CLI-role rejection upstream in ``InvokeAgentTool.run`` still
    blocks ``cli.*`` / ``*_cli`` roles before we get here.
    """
    role_name = (agent.model_role or "").strip()
    if not role_name or role_name == "chat_brain" or role_name == parent_options.role:
        return parent_adapter, _sub_agent_options(parent_options, agent)

    # Lazy import — ``tesseract.brain.boot`` imports ``InvokeAgentTool``
    # at module load, so the top of this file cannot pull from boot
    # without a circular import. The function-local import breaks the
    # cycle. Catch only config-level failures (missing key / malformed
    # YAML / build error in the chain); logic + ImportError surface so
    # structural problems aren't silently swallowed.
    from tesseract.brain.boot import (
        build_fallback_adapter,
        resolve_provider_ref_runtime,
        resolve_role_runtime,
    )

    # Provider refs (api.openai.gpt54_mini) — codex audit-2 P2: the
    # agent-writer doc accepts these as a model_role value, so honor
    # them by building a single-entry adapter from the catalog. No
    # fallback chain (the operator pinned an exact model — falling
    # back would silently change the pin).
    if _is_provider_ref(role_name):
        try:
            ref_resolved = resolve_provider_ref_runtime(role_name)
        except (RuntimeError, ValueError, KeyError):
            logger.exception(
                "invoke_agent: resolve_provider_ref_runtime raised for ref=%r — using parent adapter",
                role_name,
            )
            return parent_adapter, _sub_agent_options(parent_options, agent)
        if ref_resolved is None:
            logger.warning(
                "invoke_agent: agent=%r declares model_role=%r (provider ref) "
                "but it's unresolvable/unbuildable — falling back to parent adapter",
                agent.name, role_name,
            )
            return parent_adapter, _sub_agent_options(parent_options, agent)
        _ref_cfg, ref_adapter, ref_options = ref_resolved
        override = agent.max_tokens_override
        if override is not None and override > 0:
            import dataclasses as _dc
            ref_options = _dc.replace(ref_options, max_output_tokens=override)
        return ref_adapter, ref_options

    try:
        resolved = resolve_role_runtime(role_name)
    except (RuntimeError, ValueError, KeyError):
        logger.exception(
            "invoke_agent: resolve_role_runtime raised for role=%r — using parent adapter",
            role_name,
        )
        return parent_adapter, _sub_agent_options(parent_options, agent)

    if resolved is None:
        logger.warning(
            "invoke_agent: agent=%r declares model_role=%r but role is missing/"
            "inactive/unbuildable — falling back to parent chat_brain adapter",
            agent.name, role_name,
        )
        return parent_adapter, _sub_agent_options(parent_options, agent)

    _primary_cfg, primary_adapter, primary_options, chain = resolved
    # If the role has a usable fallback chain (>1 entry), wrap it in a
    # FallbackAdapter so the sub-session enjoys the same resilience as
    # the parent. Single-entry chains use the primary adapter directly.
    if len(chain) > 1:
        try:
            sub_adapter: ModelAdapter = build_fallback_adapter(chain)
        except Exception:  # noqa: BLE001
            logger.exception(
                "invoke_agent: build_fallback_adapter raised for role=%r — using primary entry",
                role_name,
            )
            sub_adapter = primary_adapter
    else:
        sub_adapter = primary_adapter

    # Apply agent's max_tokens_override on top of the role's options
    # (override wins over role default for this specific agent). Use
    # dataclasses.replace so future AdapterOptions fields (think /
    # keep_alive / ...) carry through without an explicit listing.
    override = agent.max_tokens_override
    if override is not None and override > 0:
        import dataclasses as _dc
        primary_options = _dc.replace(primary_options, max_output_tokens=override)
    return sub_adapter, primary_options

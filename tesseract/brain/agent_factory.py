"""Factory for constructing a sub-agent ChatSession.

Extracted from ``tesseract.kernel.tools.invoke_agent`` so the construction
block can be reused by ``AgentSessionBackend`` (multi-turn agent sessions)
without duplicating the load / validate / compose / resolve logic.

The helpers ``_build_sub_registry``, ``_compose_sub_system_prompt``, and
``_resolve_sub_adapter`` remain in ``invoke_agent`` (where they originated)
and are imported here to minimise churn.
"""

from __future__ import annotations

import dataclasses

import copy
from pathlib import Path
from typing import Any

from tesseract.agents.loader import AgentDefinition, list_agents, load_agent
from tesseract.brain.chat import ChatSession
from tesseract.brain.cost.ledger import CostLedger
from tesseract.brain.tools import AskFn, ToolRegistry
from tesseract.kernel.adapters.base import AdapterOptions, ModelAdapter
from tesseract.kernel.tools.base import ToolContext
from tesseract.permissions.policy import PermissionPolicy


class AgentBuildError(Exception):
    """Raised when an agent cannot be built as a ChatSession.

    Covers: agent not found, agent disabled, agent declares a CLI-role
    model (which invoke_agent cannot drive directly).
    """


def build_agent_session(
    *,
    name: str,
    agents_dir: Path,
    parent_adapter: ModelAdapter,
    parent_options: AdapterOptions,
    parent_registry: ToolRegistry,
    max_tool_iterations: int,
    max_consecutive_adapter_errors: int,
    tool_context: ToolContext | None,
    policy: PermissionPolicy | None,
    ask_fn: AskFn | None,
    cost_ledger: CostLedger | None = None,
    model_role: str | None = None,
) -> ChatSession:
    """Load an agent definition and construct (but do not start) a ChatSession.

    ``model_role`` overrides the card's declared role for this run only — a
    roles.yaml role name or a bare provider ref. The card stays the default;
    nothing is written back. CLI roles are refused the same way either
    source names one, since a CLI subscription has no in-process adapter.

    Raises ``AgentBuildError`` when the agent is unknown, disabled, or declares
    a CLI-role model. The caller is responsible for sending the first message.
    """
    # Import helpers from their origin module to avoid duplication.
    from tesseract.kernel.tools.invoke_agent import (
        _build_sub_registry,
        _compose_sub_system_prompt,
        _is_cli_role,
        _resolve_sub_adapter,
    )

    try:
        agent: AgentDefinition = load_agent(name, agents_dir=agents_dir)
        if model_role and model_role.strip():
            agent = dataclasses.replace(agent, model_role=model_role.strip())
    except FileNotFoundError:
        available = ", ".join(list_agents(agents_dir)) or "(none)"
        raise AgentBuildError(
            f"Unknown agent: {name!r}. Available: {available}. "
            "Use agent_create to propose a new one."
        )

    if agent.disabled:
        raise AgentBuildError(
            f"Agent {name!r} is disabled. Re-enable it from the "
            "Agents tab (frontmatter `disabled: false`) before invoking."
        )

    if _is_cli_role(agent.model_role):
        raise AgentBuildError(
            f"Agent {name!r} wants model_role={agent.model_role!r} "
            "(a CLI subscription model). invoke_agent only drives the API-side "
            "chat model today. Use delegate_coder / delegate_auditor with the "
            "agent's Role/Rules prepended to the task prompt instead."
        )

    sub_registry = _build_sub_registry(parent_registry, agent)
    system_prompt = _compose_sub_system_prompt(agent, sub_registry.names())
    sub_adapter, sub_options = _resolve_sub_adapter(parent_adapter, parent_options, agent)

    # Codex audit 2026-05-25 C-2: the nested ChatSession's __post_init__
    # assigns its OWN spawns / interactive_sessions onto its tool_context.
    # If we hand it the parent context directly, those assignments clobber
    # the parent's registries — orphaning any interactive-session handle the
    # caller already registered (session_tools) and disturbing the parent's
    # spawn wiring (invoke_agent). Give the child a shallow copy so the
    # registry reassignments land on the child while every shared plumbing
    # field (ask_fn, cancel_event, workspace_root, mission ids, …) is
    # preserved by reference.
    if tool_context is None:
        effective_context = ToolContext()
    else:
        effective_context = copy.copy(tool_context)
    # trio W3 — a sub-agent session is one nesting level deeper than its
    # parent; the cap rides along on the copied context. The child's
    # ChatSession.__post_init__ stamps both onto its own SpawnRegistry,
    # which raises SpawnDepthExceeded at register() past the cap.
    effective_context.spawn_depth = effective_context.spawn_depth + 1
    # tool_search in the child must search the CHILD's allowed surface —
    # the copied provider still pointed at the parent's full registry, so a
    # sub-agent's tool_search advertised tools it couldn't execute
    # (audit 2026-07-12).
    effective_context.tool_registry_provider = lambda: sub_registry
    effective_ask_fn = (
        (tool_context.ask_fn if tool_context is not None else None)
        or ask_fn
    )

    return ChatSession(
        adapter=sub_adapter,
        system_prompt=system_prompt,
        max_tool_iterations=max_tool_iterations,
        max_consecutive_adapter_errors=max_consecutive_adapter_errors,
        options=sub_options,
        registry=sub_registry,
        tool_context=effective_context,
        ask_fn=effective_ask_fn,
        policy=policy,
        cost_ledger=cost_ledger,
        # M5 — inherit the parent's concurrent-spawn cap so a sub-agent's own
        # fan-out is bounded too (was uncapped: child registry never got it).
        spawn_max_concurrent=effective_context.spawn_max_concurrent,
    )

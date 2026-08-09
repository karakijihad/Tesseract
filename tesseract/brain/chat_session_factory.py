"""Shared ChatSession construction for Mirror and the autonomy controller.

Both call sites build a ``ToolContext`` + ``ChatSession`` from mostly the
same fields:

- ``tesseract/mirror/server/session_factory.py::_build_chat_session``
- ``tesseract/scripts/agent_controller.py::ControllerRuntime._build_chat_session``

Each site keeps its own resolution logic (Mirror's fallback-adapter chain
and channel prompt overlay; the controller's coder-filtered registry and
coder-constraint prompt builder) and hands the already-resolved values to
:func:`build_chat_session` via a :class:`ChatSessionWiring`. This module
owns only the ~shared constructor call, not the site-specific wiring.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from tesseract.brain.cost import BudgetExhausted, CostLedger
from tesseract.brain.tools import AskFn, ToolRegistry
from tesseract.kernel.adapters.base import AdapterOptions, ModelAdapter
from tesseract.kernel.tools.base import (
    CliSink,
    LaneManagerProvider,
    NamedLaneManagerProvider,
    PtyDispatcher,
    SchedulerProvider,
    StatusEmit,
    ToolContext,
    ToolRegistryProvider,
)
from tesseract.permissions.policy import PermissionPolicy

if TYPE_CHECKING:
    from tesseract.brain.chat import ChatSession


@dataclass(frozen=True)
class ChatSessionWiring:
    """Resolved construction inputs for one ``ChatSession``.

    Fields with no default are supplied by both current call sites
    (each with its own resolution logic). Fields defaulting to ``None``
    (or the matching underlying ``ChatSession``/``ToolContext`` default)
    are genuinely divergent — today only one of the two call sites ever
    sets them; leaving the other at its default reproduces its current
    behavior exactly.
    """

    adapter: ModelAdapter
    system_prompt: str
    max_tool_iterations: int
    max_consecutive_adapter_errors: int
    workspace_root: str
    session_id: str
    cli_sink: CliSink | None
    scheduler_provider: SchedulerProvider | None
    tool_registry_provider: ToolRegistryProvider | None
    lane_manager_provider: LaneManagerProvider | None
    named_lane_manager_provider: NamedLaneManagerProvider | None
    ask_fn: AskFn | None
    policy: PermissionPolicy | None
    prompt_builder: Callable[[], str] | None
    registry: ToolRegistry | None
    spawn_depth_cap: int | None

    # Divergent knobs — Mirror-only unless noted otherwise.
    pty_dispatcher: PtyDispatcher | None = None
    status_emit: StatusEmit | None = None
    session_emit: Any = None  # controller-only
    options: AdapterOptions | None = None
    compact_threshold: float | None = None
    keep_recent_turns: int | None = None
    cost_ledger: CostLedger | None = None
    overage_ask_fn: Callable[[BudgetExhausted], Awaitable[bool]] | None = None
    session_kind: str = "cockpit"
    channel_display_name: str | None = None
    spawn_stall_seconds: float | None = None
    spawn_max_concurrent: int | None = None


def build_chat_session(wiring: ChatSessionWiring) -> "ChatSession":
    """Construct the ``ToolContext`` + ``ChatSession`` shared by both builders.

    ``options`` / ``compact_threshold`` / ``keep_recent_turns`` are omitted
    from the ``ChatSession`` call when ``None`` on the wiring so the
    dataclass's own defaults apply — matching the controller's existing
    "only pass what the config actually has" behavior.

    ``ChatSession`` is imported locally (call-time, not module-level) so
    tests that monkeypatch ``tesseract.brain.chat.ChatSession`` — both
    original call sites did their import inside the method for the same
    reason — keep working unchanged.
    """
    from tesseract.brain.chat import ChatSession

    tool_context = ToolContext(
        workspace_root=wiring.workspace_root,
        session_id=wiring.session_id,
        cli_sink=wiring.cli_sink,
        pty_dispatcher=wiring.pty_dispatcher,
        scheduler_provider=wiring.scheduler_provider,
        tool_registry_provider=wiring.tool_registry_provider,
        lane_manager_provider=wiring.lane_manager_provider,
        named_lane_manager_provider=wiring.named_lane_manager_provider,
        ask_fn=wiring.ask_fn,
        status_emit=wiring.status_emit,
        session_emit=wiring.session_emit,
        spawn_depth_cap=wiring.spawn_depth_cap,
    )
    kwargs: dict[str, Any] = dict(
        adapter=wiring.adapter,
        system_prompt=wiring.system_prompt,
        max_tool_iterations=wiring.max_tool_iterations,
        max_consecutive_adapter_errors=wiring.max_consecutive_adapter_errors,
        prompt_builder=wiring.prompt_builder,
        registry=wiring.registry,
        tool_context=tool_context,
        ask_fn=wiring.ask_fn,
        policy=wiring.policy,
        cost_ledger=wiring.cost_ledger,
        overage_ask_fn=wiring.overage_ask_fn,
        session_kind=wiring.session_kind,
        channel_display_name=wiring.channel_display_name,
        spawn_stall_seconds=wiring.spawn_stall_seconds,
        spawn_max_concurrent=wiring.spawn_max_concurrent,
    )
    if wiring.options is not None:
        kwargs["options"] = wiring.options
    if wiring.compact_threshold is not None:
        kwargs["compact_threshold"] = wiring.compact_threshold
    if wiring.keep_recent_turns is not None:
        kwargs["keep_recent_turns"] = wiring.keep_recent_turns
    return ChatSession(**kwargs)

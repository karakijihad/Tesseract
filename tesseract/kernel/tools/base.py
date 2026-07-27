"""Tool ABC — the interface all tools implement.

Each tool declares its concurrency safety and read-only status.
Tools return security-layer hints only via ``check_permissions``;
``tesseract.permissions.decide.evaluate`` is the live decision engine
that resolves policy, posture, and approvals. ``is_concurrency_safe``
gates per-turn parallel execution in ``_run_pending_calls``.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, ClassVar, Optional

from pydantic import BaseModel

CliSink = Callable[[str, str, dict[str, Any]], Awaitable[None]]
PtyDispatcher = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]
# Optional per-session status callback. Tools that delegate to a sub-agent
# or call an external generator (image_generate, transcribe_audio, the
# vision flavor of invoke_agent) emit a one-line "what's happening" string
# so the operator sees role + resolved model rather than a silent pause.
# Mirror plumbs a coroutine that wraps the WS `tool_status` envelope; REPL
# wires a stderr printer; tests pass `None`.
StatusEmit = Callable[[str], Awaitable[None]]
# Phase 18 Task B — schedule_create / schedule_remove tools call the
# live SchedulerEngine. Resolved per-call via a getter so the registry
# can be built before the engine in `_on_startup`.
SchedulerProvider = Callable[[], Any]
# X-4 — `lane_*` tools call the live LaneManager (controller-owned).
# Same per-call getter pattern; the lane manager outlives any brain
# reload, so the provider just returns the singleton attribute.
LaneManagerProvider = Callable[[], Any]
# X-5 — `lane_named_*` tools call the live NamedLaneManager. Separate
# provider from `lane_manager_provider` so the name→lane_id binding
# layer can be wired independently (e.g. a test could exercise lane
# tools without bringing up named-lane persistence). Resolved per-call
# for the same reason as the lane provider.
NamedLaneManagerProvider = Callable[[], Any]
# `tool_search` (and any tool needing the live registry) calls this to
# reach the LIVE registry instance held by the calling layer (Mirror's
# `app["tool_registry"]` / REPL's local). Resolved per-call so the
# registry can be built before the tool context is constructed (same
# pattern as scheduler_provider).
ToolRegistryProvider = Callable[[], Any]
# AskFn forward-declared as a generic Callable to avoid importing
# `brain.tools.AskFn` here (would create a circular import — brain.tools
# already imports Tool/ToolContext from this module). Concrete signature
# matches `brain.tools.AskFn`: (Tool, BaseModel, ToolContext) -> bool.
# The context carries `posture_source` (set by `decide.evaluate` before
# the call) so ask_fn implementations can record durable approval-ledger
# rows alongside the UI prompt.
AskFn = Callable[[Any, Any, Any], Awaitable[bool]]


class PermissionResult(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"
    PASSTHROUGH = "passthrough"


class SpawnCapExceeded(RuntimeError):
    """Raised by `SpawnRegistry.register` (brain layer) when the per-session
    concurrent spawn cap (`runtime.yaml::max_concurrent_spawns_per_session`)
    is hit. Lives here — not in `brain.spawns` — so background-capable kernel
    tools can catch it without a kernel→brain import."""

    def __init__(self, running: int, cap: int) -> None:
        super().__init__(f"spawn cap reached: {running} running, cap {cap}")
        self.running = running
        self.cap = cap


class SpawnDepthExceeded(SpawnCapExceeded):
    """Raised by `SpawnRegistry.register` when the owning session already sits
    at `runtime.yaml::max_spawn_depth` nesting levels (trio W3 — OpenClaw
    maxSpawnDepth analog, the structural backstop against spawn-inside-spawn
    runaways). Subclasses `SpawnCapExceeded` so every existing background-
    capable call site handles it without changes."""

    def __init__(self, depth: int, cap: int) -> None:
        RuntimeError.__init__(
            self, f"spawn depth cap reached: session depth {depth}, cap {cap}"
        )
        self.running = depth  # base-class field shape, reused as depth
        self.cap = cap
        self.depth = depth


def spawn_cap_tool_result(exc: SpawnCapExceeded) -> "ToolResult":
    """Shared cap-hit ToolResult for every background-capable tool."""
    if isinstance(exc, SpawnDepthExceeded):
        return ToolResult(
            output=(
                f"spawn depth cap reached (nesting level {exc.depth}, cap "
                f"{exc.cap}): this session is already a nested spawn and may "
                "not spawn deeper. Do the work inline or report back to the "
                "parent instead."
            ),
            is_error=True,
            metadata={
                "reason": "spawn_depth_exceeded",
                "depth": exc.depth,
                "cap": exc.cap,
            },
        )
    return ToolResult(
        output=(
            f"spawn cap reached ({exc.running} running, cap {exc.cap}): "
            "spawn_await or spawn_cancel an existing handle first, or "
            "pass background=false to run inline."
        ),
        is_error=True,
        metadata={"reason": "spawn_cap_exceeded", "running": exc.running, "cap": exc.cap},
    )


@dataclass(frozen=True)
class ToolResult:
    output: str
    is_error: bool = False
    metadata: dict[str, Any] | None = None
    denied_hard: bool = False
    deny_reason: str = ""
    # True when the tool exited because its wallclock budget elapsed,
    # NOT because the underlying operation logically failed. The autonomy
    # runner uses this to distinguish "ran out of time, park-not-discard"
    # from "tool error, mark FAILED". Tools that have no notion of
    # wallclock timeouts leave it False; tools that DO (delegate_claude /
    # delegate_codex) set it in their timeout
    # branch alongside is_error=True.
    # lane_turn is a second, intentional exception: its `is_error` reflects
    # the lane turn's own outcome (turn_ended.payload["is_error"]) when the
    # turn completes, not tool-plumbing/timeout status. On timeout,
    # `timed_out=True` pairs with `is_error=False` — meaning "turn still
    # running, partial result returned" — and lane_turn appends an explicit
    # marker to `output` so the model sees the incompleteness even though
    # only `.output` reaches it as tool-message content.
    timed_out: bool = False


@dataclass
class ToolContext:
    workspace_root: str = "."
    session_id: str = ""
    current_call_id: str = ""
    # Set by `decide.evaluate` before `ask_fn` is invoked so ask_fn
    # implementations can write durable approval-ledger rows with the
    # effective posture source. One of: "security", "path_validator",
    # "path", "mode", "default", "tool", "mission". Empty when no
    # decision has been resolved yet.
    posture_source: str = ""
    cli_sink: Optional[CliSink] = field(default=None, repr=False)
    pty_dispatcher: Optional[PtyDispatcher] = field(default=None, repr=False)
    scheduler_provider: Optional[SchedulerProvider] = field(default=None, repr=False)
    lane_manager_provider: Optional[LaneManagerProvider] = field(default=None, repr=False)
    named_lane_manager_provider: Optional[NamedLaneManagerProvider] = field(default=None, repr=False)
    tool_registry_provider: Optional[ToolRegistryProvider] = field(default=None, repr=False)
    ask_fn: Optional[AskFn] = field(default=None, repr=False)
    # Per-session "tool is doing X" status callback. None = silent. See
    # StatusEmit type alias above. Tools should guard with `if status_emit:`
    # rather than assume it's wired so test fixtures don't have to plumb it.
    status_emit: Optional[StatusEmit] = field(default=None, repr=False)
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    # Phase 3 (CLI parity) — per-session todo list, Claude Code's
    # TodoWrite analog. Mutated by `tasks_set` / `tasks_update`. The WS
    # layer reads the post-call state on TOOL_RESULT and emits a
    # `tasks_state` envelope so the chat-embedded TodosCard re-renders.
    # Each entry: {"id": str, "title": str, "status":
    # "pending"|"in_progress"|"completed"}. Ephemeral per session; not
    # persisted yet (Phase 1 will fold into schema-2 day-files).
    todos: list[dict[str, Any]] = field(default_factory=list, repr=False)
    # Phase 4 (CLI parity) — background-spawn registry hook. The
    # owning ChatSession populates this with its `SpawnRegistry`
    # instance so tools (delegate_claude with await=False, plus the
    # spawn_check / spawn_await / spawn_cancel control tools) can
    # register or look up handles without importing
    # tesseract.brain.spawns from the kernel layer (which would cross
    # the brain → kernel direction). `Any` to avoid the circular type
    # import; runtime contract is `SpawnRegistry`.
    spawns: Any = field(default=None, repr=False)
    # Interactive multi-turn sessions (claude/codex CLI + own agents),
    # keyed by handle. Wired by the owning ChatSession, same as `spawns`.
    interactive_sessions: Any = field(default=None, repr=False)
    # A10 — per-session event bridge for interactive-session tools.
    # When set by the controller's _build_chat_session, session_* tools
    # emit raw event dicts through this callable so session open/close/
    # streaming output surfaces on the TUI rail + detail pane, keyed by
    # the session handle. None = no forwarding (REPL / Mirror paths).
    session_emit: Any = field(default=None, repr=False)
    # Lean-agent-os P1 Task 2 — the owning ChatSession's live set of
    # extended-tool names `tool_search` has surfaced this session. Same
    # cross-link pattern as `spawns`: `ChatSession.__post_init__` shares
    # its `_enabled_extended_tools` set instance here so `tool_search.run`
    # can mutate it in place and the next `schemas_for_adapter` call sees
    # the addition. `None` in contexts with no owning session (tests,
    # sub-agent contexts that don't care about tiering).
    enabled_extended_tools: Any = field(default=None, repr=False)
    # trio W3 — spawn nesting level of the owning session. Root sessions sit
    # at 0; `agent_factory.build_sub_session` bumps the copied context by 1
    # per nesting. `ChatSession.__post_init__` stamps both onto its
    # SpawnRegistry; `register()` raises SpawnDepthExceeded when
    # depth >= cap. `spawn_depth_cap=None` (REPL / tests) = uncapped.
    spawn_depth: int = 0
    spawn_depth_cap: int | None = None
    # M5 — per-session concurrent-spawn cap, carried on the context so
    # sub-agent sessions inherit their parent's fan-out limit instead of
    # running uncapped. `ChatSession.__post_init__` stamps this from the
    # owning session's `spawn_max_concurrent`; `agent_factory` reads it off the
    # copied child context. `None` = uncapped (REPL / tests).
    spawn_max_concurrent: int | None = None


class Tool(ABC):
    # Class-declared baseline posture — single source of truth for "what
    # should this tool default to?". `permissions.yaml::tools[name]` overrides
    # this; `modes.<mode>.overrides` and `path_overrides` then layer on top.
    # Boot asserts every registered tool sets this to "auto"|"ask"|"deny" —
    # adding a new tool without declaring a baseline becomes a startup error.
    default_posture: ClassVar[str] = ""

    # AU-3 — risk class for autonomy admission. Every concrete subclass
    # MUST declare one of "autonomous" | "propose" | "operator_gate" |
    # "absolute_deny" per `Docs/Plan/autonomy/_shared/risk-class-taxonomy.md`.
    # Boot raises if missing or invalid. The AgendaStore (AU-4) compares
    # the dispatched tool's class against the agenda item's class at
    # admission and rejects if the item is more permissive than the tool
    # allows.
    risk_class: ClassVar[str] = ""

    # Audit-3 M9 — tools whose output may contain attacker-controlled or
    # third-party text (file contents, web pages, vault articles, search
    # snippets) set this True. ``ChatSession._run_pending_calls`` wraps
    # their output in an ``UNTRUSTED_TOOL_OUTPUT`` envelope before
    # appending to model history so the model treats the body as data,
    # not instructions. Defaults False — kernel-internal tools
    # (filesystem write, scheduler, mission control) are trusted.
    untrusted_source: ClassVar[bool] = False

    # Lean-agent-os P1 Task 2 — schema-visibility tier. "core" tools'
    # schemas are always sent to the chat model; "extended" tools are
    # omitted from the per-turn payload unless the session has surfaced
    # them via ``tool_search`` (``ToolRegistry.schemas_for_adapter``).
    # VISIBILITY ONLY — an extended tool invoked by name still resolves
    # and runs; `permissions.yaml` postures and `decide.evaluate` are
    # untouched by this attribute. Boot marks the pinned core set in
    # `brain/boot.py::_CORE_TOOL_NAMES`; every other tool defaults here
    # to "extended".
    tier: ClassVar[str] = "extended"

    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    @abstractmethod
    def description(self) -> str: ...

    @property
    @abstractmethod
    def input_schema(self) -> type[BaseModel]: ...

    def is_concurrency_safe(self) -> bool:
        return False

    def is_read_only(self) -> bool:
        return False

    def check_permissions(self, tool_input: BaseModel, context: ToolContext) -> PermissionResult:
        return PermissionResult.PASSTHROUGH

    @abstractmethod
    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult: ...

    def to_schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema.model_json_schema(),
        }

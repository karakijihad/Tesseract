"""Tool ABC — the interface all tools implement.

Each tool declares its concurrency safety and read-only status.
Tools return security-layer hints only via ``check_permissions``;
``tesseract.permissions.decide.evaluate`` is the live decision engine
that resolves policy, posture, and approvals. ``is_concurrency_safe``
gates per-turn parallel execution in ``_run_pending_calls``.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, ClassVar, Optional

from pydantic import BaseModel

_log = logging.getLogger(__name__)

CliSink = Callable[[str, str, dict[str, Any]], Awaitable[None]]
PtyDispatcher = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]
# Optional per-session status callback. Tools that delegate to a sub-agent
# or call an external generator (image_generate, transcribe_audio, the
# vision flavor of invoke_agent) emit a one-line "what's happening" string
# so the operator sees role + resolved model rather than a silent pause.
# Mirror plumbs a coroutine that wraps the WS `tool_status` envelope; REPL
# wires a stderr printer; tests pass `None`.
StatusEmit = Callable[[str], Awaitable[None]]
# The schedule_create / schedule_remove tools call the
# live SchedulerEngine. Resolved per-call via a getter so the registry
# can be built before the engine in `_on_startup`.
SchedulerProvider = Callable[[], Any]
# `lane_*` tools call the live LaneManager (controller-owned).
# Same per-call getter pattern; the lane manager outlives any brain
# reload, so the provider just returns the singleton attribute.
LaneManagerProvider = Callable[[], Any]
# `lane_named_*` tools call the live NamedLaneManager. Separate
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
    at `runtime.yaml::max_spawn_depth` nesting levels — the structural
    backstop against spawn-inside-spawn runaways, which a width cap alone
    cannot catch. Subclasses `SpawnCapExceeded` so every existing background-
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
    # wallclock timeouts leave it False; tools that DO (delegate_coder /
    # delegate_auditor) set it in their timeout
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
    # Which conversation this call is running in, when it is running in one.
    # A session id is the connection; this is the chat, and it is what a thing
    # the assistant leaves behind has to remember in order to reach the
    # conversation that made it. A card drawn in one chat and pressed an hour
    # later has to wake THAT chat, not whichever is in focus. Empty for the
    # REPL, autonomy, sub-agents and the scheduler, all of which have no chat.
    chat_id: str = ""
    # The channel this call is running on, when it came through one. Set only
    # by a bridge; empty on the cockpit, the REPL, autonomy, sub-agents and the
    # scheduler. `chat_id` cannot answer this on its own: every chat is stamped
    # with one, so a tool asking "am I confined to one conversation?" by
    # testing `chat_id` for emptiness confines the operator's own window too.
    channel: str = ""
    # The channel's OWN id for this chat (a Telegram numeric chat id), which is
    # not `chat_id`: that one is the runtime's durable conversation id, and it
    # is what history reads and surface ownership key off. Anything stored by
    # the bridge under the adapter's identity — inbound media under
    # `uploads/channels/<channel>/<chat_id>/` — is reachable only by this one.
    # Empty everywhere `channel` is empty.
    channel_chat_id: str = ""
    current_call_id: str = ""
    # The turn this call runs inside, and the way to tie that turn to a task.
    # Both stamped per call by the owning `ChatSession` beside
    # `current_call_id`; empty and `None` where no turn is recorded (the REPL,
    # the scheduler, autonomy). `bind_task` is the turn recorder's own method,
    # handed down rather than the recorder itself, because a tool needs to say
    # one thing about the turn and nothing else.
    turn_id: str = ""
    bind_task: Optional[Callable[[str], None]] = field(default=None, repr=False)
    # The MCP client identity this call is being made on behalf of, when it
    # came in over the hub. Durable resource ownership hangs off this, not off
    # `session_id` — a session id changes every reconnect, so a lane owned by
    # one would be orphaned the moment its client came back. Empty means an
    # in-process call with no MCP principal (the assistant's own turn, autonomy, the
    # scheduler): those reach the substrate directly rather than through a
    # gateway, and are the operator's work.
    caller_principal: str = ""
    # Set by `decide.evaluate` before `ask_fn` is invoked so ask_fn
    # implementations can write durable approval-ledger rows with the
    # effective posture source. One of: "security", "path_validator",
    # "path", "mode", "default", "tool", "mission". Empty when no
    # decision has been resolved yet.
    posture_source: str = ""
    # Written BY an ask_fn implementation on its way out, so a caller that
    # reports to a person can tell the two events a `False` return covers
    # apart: the operator said no, and nobody answered. The ledger has kept
    # them separate since it was written (`result: deny` vs `result:
    # timeout`); a surface downstream of the bool that collapses them tells
    # the operator they declined something they never saw.
    # One of the ledger's own values — "allow_once", "deny", "timeout",
    # "cancelled", "park_timeout". Empty means the asker made no claim, and
    # a caller must then name both possibilities rather than pick one.
    ask_outcome: str = ""
    # Which forced-ASK security checks this call tripped, written by
    # `bash_tool` and `command_run` on their way to returning ASK and read by
    # `decide.evaluate` to decide whether the call may run with nobody
    # watching. Empty for every other tool and for a command that tripped
    # nothing.
    #
    # A context field rather than a richer `check_permissions` return, because
    # that signature is implemented by every tool and exactly two of them have
    # anything to say here. `decide.evaluate` reads it and clears it in the
    # same breath, so a value written by one call can never be read as a claim
    # about the next: contexts are built with `dataclasses.replace` off a
    # session-lifetime object, and a stale tuple would relax an unrelated
    # tool's approval.
    security_checks: tuple[int, ...] = ()
    # Shared CostLedger singleton, threaded so a tool that makes a PAID
    # call of its own can bill it. `JobContext` has carried this since the
    # 2026-06-28 cost-ledger gap; tools had no equivalent, so `screen_look`
    # shipped a metered vision call that reached no ledger row and counted
    # against no cap. None for REPL/test contexts — metering is a no-op.
    cost_ledger: Optional[Any] = field(default=None, compare=False, repr=False)
    cli_sink: Optional[CliSink] = field(default=None, repr=False)
    pty_dispatcher: Optional[PtyDispatcher] = field(default=None, repr=False)
    scheduler_provider: Optional[SchedulerProvider] = field(default=None, repr=False)
    lane_manager_provider: Optional[LaneManagerProvider] = field(default=None, repr=False)
    named_lane_manager_provider: Optional[NamedLaneManagerProvider] = field(default=None, repr=False)
    tool_registry_provider: Optional[ToolRegistryProvider] = field(default=None, repr=False)
    ask_fn: Optional[AskFn] = field(default=None, repr=False)
    # The effective PermissionPolicy for this call, synced by
    # `brain.tools.execute_tool` alongside `ask_fn`. A tool that dispatches to
    # another tool MUST forward this: `decide.evaluate` resolves a posture from
    # `permissions.yaml` only when a policy is present, so a nested call made
    # without one silently proceeds at PASSTHROUGH — skipping every ASK and
    # DENY the operator configured. Typed loosely to avoid importing
    # `permissions.policy` here (circular).
    policy: Optional[Any] = field(default=None, repr=False)
    # Per-session "tool is doing X" status callback. None = silent. See
    # StatusEmit type alias above. Tools should guard with `if status_emit:`
    # rather than assume it's wired so test fixtures don't have to plumb it.
    status_emit: Optional[StatusEmit] = field(default=None, repr=False)
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    # Per-session todo list, Claude Code's
    # TodoWrite analog. Mutated by `tasks_set` / `tasks_update`. The WS
    # layer reads the post-call state on TOOL_RESULT and emits a
    # `tasks_state` envelope so the chat-embedded TodosCard re-renders.
    # Each entry: {"id": str, "title": str, "status":
    # "pending"|"in_progress"|"completed"}. Ephemeral per session; not
    # persisted yet.
    todos: list[dict[str, Any]] = field(default_factory=list, repr=False)
    # Background-spawn registry hook. The
    # owning ChatSession populates this with its `SpawnRegistry`
    # instance so tools (delegate_coder with await=False, plus the
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
    # Spawn nesting level of the owning session. Root sessions sit
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
    # The owning ChatSession's own measurement of this conversation's shape:
    # tokens, turns, the fold it is heading for, the cache split of the last
    # turn. Wired by `ChatSession.__post_init__`, same cross-link pattern as
    # `spawns` — a callable rather than the session itself, because a tool
    # asking how full the context is has no business reaching the history.
    # `None` where there is no conversation (the scheduler, autonomy, a
    # sub-agent), and `context_read` says so rather than reporting zeros.
    context_report: Optional[Callable[[], dict[str, Any]]] = field(
        default=None, repr=False
    )
    # What the turn asks to happen to itself once it is over: "fold",
    # "handoff" or "reflect". Wired by `ChatSession.__post_init__` beside
    # `context_report`, and a callable for the same reason that one is — a tool
    # deciding this conversation is finished has no business reaching its
    # history. It only records the decision; `mirror/server/after_turn.py` acts
    # on it at the turn boundary, because folding or wiping mid-turn would
    # discard the assistant message carrying the pending `tool_use` block
    # before its `tool_result` is appended. `None` where there is no
    # conversation to continue (the scheduler, autonomy, a sub-agent), and
    # `session_continue` says so rather than claiming it worked.
    request_continuation: Optional[Callable[[str], None]] = field(
        default=None, repr=False
    )


class Tool(ABC):
    # Class-declared baseline posture — single source of truth for "what
    # should this tool default to?". `permissions.yaml::tools[name]` overrides
    # this; `modes.<mode>.overrides` and `path_overrides` then layer on top.
    # Boot asserts every registered tool sets this to "auto"|"ask"|"deny" —
    # adding a new tool without declaring a baseline becomes a startup error.
    default_posture: ClassVar[str] = ""

    # Risk class for autonomy admission. Every concrete subclass
    # MUST declare one of "autonomous" | "propose" | "operator_gate" |
    # "absolute_deny" in the risk-class taxonomy.
    # Boot raises if missing or invalid. The AgendaStore compares
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
    # `working_set.yaml::core`, which the operator owns; every other tool
    # defaults here to "extended".
    tier: ClassVar[str] = "extended"

    # Where this tool came from. "shipped" is everything registered by name in
    # `brain/boot.py`; "custom" is set on the INSTANCE by
    # `kernel/home_tools.py` for a tool loaded out of the operator's own tree.
    #
    # One registry, one tag, and every surface filters on it rather than
    # keeping a second list: the glossary excludes custom, the generated Guide
    # and the kernel manifest describe shipped only, and Settings, the usage
    # heatmap and the working set show all of them with the custom ones
    # marked. A second roster would be a second thing to keep in step, which
    # is the defect this whole initiative exists to remove.
    origin: ClassVar[str] = "shipped"

    # ── The tool contract ────────────────────────────────────────────────
    # Four fields that answer, in order, the questions asked when choosing a
    # tool: which section, what is this, is this the one, what outranks it.
    # `description` composes from them, so the disambiguation lands on the
    # ONE surface the model always reads — the schema — rather than in prose
    # a prompt mode may or may not inline.
    #
    # `group` is a slug from `taxonomy.py::GROUPS`; boot raises on anything
    # else. `summary` is capped at 90 chars because the glossary renders one
    # per tool on every turn and has a budget. `use_when` and `not_when` are
    # uncapped and reach the API description.

    group: ClassVar[str] = ""

    # One sentence, ≤90 chars: what this is. Renders in the glossary.
    summary: ClassVar[str] = ""

    # When this tool is the right answer.
    use_when: ClassVar[str] = ""

    # What outranks it, naming the SIBLING TOOL rather than a category.
    # "Not for large files" is not a `not_when`; "use `glob` when you want
    # paths rather than contents" is. A tool that genuinely competes with
    # nothing may set "", but it must set it — the empty string is a decision
    # on the record, and the missing field is an oversight the boot guard
    # refuses. This is the field the P11 regression needed and did not have:
    # the disambiguating sentence existed, in a file the prompt never loaded,
    # while the schema carried the sentence that misled.
    not_when: ClassVar[str] = ""

    # What this tool cannot work without, and therefore what its breaker is
    # named after. `role:<roles.yaml key>`, `service:<providers.yaml services
    # key>`, or "" for a tool with nothing behind it —
    # `kernel/tools/dependency.py` owns the shapes and resolves them to a
    # catalog ref.
    #
    # Declared here rather than inferred, and "" is a decision on the record
    # exactly as `not_when`'s is. The empty string is what keeps `file_read`
    # ungated: a tool with no dependency has no capability that can be down,
    # so "File not found" is the model calling it wrong and must never count
    # against anything. That is the whole caller-versus-dependency split, and
    # it is structural rather than a table of error strings.
    depends_on: ClassVar[str] = ""

    # Input fields whose CONTENT must not reach a log. Declared here, on the
    # class, for the same reason the four fields above are: a rule kept
    # somewhere else is a rule the next tool does not inherit.
    #
    # Two long-lived plain-text logs record a tool call. `pc.jsonl` takes the
    # input of every browser verb, and `approvals.jsonl` takes the input of
    # every ASK-posture call whether the operator allows it or refuses it. A
    # password typed into a page reaches both, and a redaction wired into one
    # of them is a redaction that reads as done and is not.
    redacted_input_fields: ClassVar[tuple[str, ...]] = ()

    @classmethod
    def redact_input(cls, payload: dict[str, Any] | None) -> dict[str, Any]:
        """`payload` with the declared fields replaced by their shape.

        What a log is for survives: which tool, which element, how much. What
        it must not keep is the characters. Three shapes cover what tool
        inputs are: a string, a list of strings, and a list of objects with a
        `value` (which is how a form's fields arrive).
        """
        if not payload:
            return {}
        if not cls.redacted_input_fields:
            return dict(payload)
        safe = dict(payload)
        for field_name in cls.redacted_input_fields:
            if field_name not in safe:
                continue
            safe[field_name] = _redacted_shape(safe[field_name])
        return safe

    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    def description(self) -> str:
        """What the model is told this tool is, composed from the contract.

        A tool that still authors its own `description` overrides this — that
        is the migration path, one taxonomy group at a time, and the boot
        guard is what eventually makes the composed form the only form.
        """
        parts = [self.summary, self.use_when]
        if self.not_when:
            parts.append(f"Not for: {self.not_when}")
        return " ".join(p for p in parts if p)

    @property
    @abstractmethod
    def input_schema(self) -> type[BaseModel]: ...

    def is_concurrency_safe(self) -> bool:
        return False

    def is_read_only(self) -> bool:
        return False

    def check_permissions(self, tool_input: BaseModel, context: ToolContext) -> PermissionResult:
        return PermissionResult.PASSTHROUGH

    def ask_reason(self, validated: BaseModel) -> str:
        """One sentence saying what is being approved, and why it was asked.

        Empty by default: most tools are explained by their own arguments,
        which every approval surface already shows. A tool overrides this when
        the arguments do not explain the prompt, and the two that most needed
        it are `bash` and `command_run`, where a security check can force the
        question in a mode the operator has already relaxed.

        Declared here rather than looked for with `getattr`, because two
        surfaces read it and a contract that exists only where somebody
        remembered to duck-type it is how one of them goes quiet.
        """
        del validated
        return ""

    @abstractmethod
    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult: ...

    def to_schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema.model_json_schema(),
        }


def ask_reason_for(tool: "Tool", validated: BaseModel) -> str:
    """The tool's own explanation of a prompt, or nothing, never a raise.

    Both approval surfaces call this: the cockpit's `ask_gate` and the channel
    gate a phone answers. One implementation, because the operator has to be
    told the same thing wherever the question reaches them, and because a
    tool whose explanation raises must cost a sentence rather than the
    approval.
    """
    try:
        return str(tool.ask_reason(validated) or "").strip()
    except Exception:
        _log.exception("ask_reason raised for %s; leaving it blank", tool.name)
        return ""


#: The risk classes a concrete tool may declare. Lives here, beside the
#: `risk_class` ClassVar it constrains, because two callers now check it:
#: `brain/boot.py::_wire_tool_defaults` raises on a shipped tool that fails,
#: and `kernel/home_tools.py` skips a home tool that does. A second copy of
#: this set is a second answer to what the autonomy gate admits.
VALID_RISK_CLASSES = frozenset(
    {"autonomous", "propose", "operator_gate", "absolute_deny"}
)


def _redacted_shape(value: Any) -> Any:
    """Say how much there was, never what it was."""
    if isinstance(value, str):
        return f"<{len(value)} character(s)>"
    if isinstance(value, list):
        if value and all(isinstance(v, dict) and "value" in v for v in value):
            return [
                {**v, "value": f"<{len(str(v.get('value') or ''))} character(s)>"}
                for v in value
            ]
        return [f"<{len(value)} item(s)>"]
    if value is None:
        return None
    return "<redacted>"


#: What one glossary line may cost. The glossary carries every tool and rides
#: every turn, so this is a budget rather than a style rule. It lived only in a
#: test, and that is the one contract rule that actually drifted: two summaries
#: went over and stayed over for weeks while boot went on succeeding.
SUMMARY_MAX_CHARS = 90


def check_tool_contract(tool: Tool) -> None:
    """Every tool declares what it is, when to use it, and what outranks it.

    Called from `brain/boot.py::_wire_tool_defaults`, beside the posture and
    risk-class checks, so boot fails on an under-declared tool exactly as it
    already fails on one with no `default_posture`.

    The guard exists because the alternative was prose. The sentence that
    disambiguated two tools lived in a workspace file the prompt inlines in one
    mode out of two, so it reached a payload on no turns at all while the
    schema kept saying something else. A field the runtime refuses to boot
    without cannot decay that way.

    `not_when` may be `""` — a tool that genuinely competes with nothing says
    so, and the empty string is a decision on the record rather than an
    oversight. The other three may not be empty. An unknown `group` raises
    rather than warns: the glossary renders by group, so a typo'd slug is a
    tool that silently appears in no section of it.
    """
    from tesseract.kernel.tools.taxonomy import GROUPS

    cls = type(tool)
    missing = [
        field
        for field in ("group", "summary", "use_when")
        if not str(getattr(cls, field, "") or "").strip()
    ]
    # Declared, not merely inherited. `Tool.not_when` defaults to `""`, which
    # is a legitimate value, so a type check alone passes a tool that never
    # thought about the field at all — the one field of the four whose absence
    # the guard could not see.
    #
    # The walk STOPS at `Tool`, and that is the whole check: `Tool` declares
    # the field itself, so a walk that included it would find one on every
    # subclass ever written and answer yes unconditionally.
    # Declared, not merely inherited — same walk, same reason, for both of the
    # fields whose empty string is legitimate.
    for field in ("not_when", "depends_on"):
        declared = False
        for klass in cls.__mro__:
            if klass is Tool:
                break
            if field in klass.__dict__:
                declared = True
                break
        if not declared or not isinstance(getattr(cls, field, None), str):
            missing.append(field)
    if missing:
        raise RuntimeError(
            f"tool '{tool.name}' (class {cls.__name__}) is missing "
            f"tool-contract field(s) {sorted(missing)}. Declare them at the "
            f"class level — `description` composes from them."
        )
    if cls.group not in GROUPS:
        raise RuntimeError(
            f"tool '{tool.name}' (class {cls.__name__}) declares "
            f"group={cls.group!r}, which is not a taxonomy slug. Use one of "
            f"{sorted(GROUPS)}, or add the group to "
            f"tesseract/kernel/tools/taxonomy.py."
        )
    if len(cls.summary) > SUMMARY_MAX_CHARS:
        raise RuntimeError(
            f"tool '{tool.name}' (class {cls.__name__}) has a {len(cls.summary)}"
            f"-character summary; the glossary budget is {SUMMARY_MAX_CHARS}. "
            "Shorten it — the glossary carries one line per tool and every "
            "turn pays for the whole list."
        )
    # A declaration the runtime cannot resolve is a typo, and a typo here is a
    # tool that silently has no breaker. Raised beside the group check for the
    # same reason that one is. A config that will not load at all is NOT this
    # check's business — boot's own config layer raises for that, and failing
    # here would report a missing providers.yaml as a broken tool.
    from tesseract.config.loader import ConfigError
    from tesseract.kernel.tools.dependency import DependencyError, resolve

    try:
        resolve(cls.depends_on)
    except DependencyError as exc:
        raise RuntimeError(
            f"tool '{tool.name}' (class {cls.__name__}) declares "
            f"depends_on={cls.depends_on!r}, which does not resolve: {exc}"
        ) from exc
    except ConfigError:
        pass

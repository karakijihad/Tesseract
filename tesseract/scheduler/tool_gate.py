"""Which tool a scheduled row is allowed to invoke, and when that is decided.

A job fires with nobody watching. There is no prompt to answer and nobody to
answer it, so the runtime's honest response to an ASK tool in that situation is
to deny (`permissions/decide.py::evaluate`). A row pointed at an ASK tool would
therefore look armed, fire on time, and do nothing every single time.

Worse than useless, it is a way in. "The row is armed, so the operator must
have meant it" turns a schedule into a standing approval, and creating a
schedule is a much quieter act than approving a send. Anything that can write
a row could then reach a tool the operator has deliberately kept behind a
prompt.

So the posture is the gate, and it is checked before a row can run rather than
argued about after it fires. A tool set to `ask` or `deny` cannot be scheduled.
The operator changes `permissions.yaml`, which is the file that decides this
for the whole runtime, and that is one decision made once in the open instead
of an approval a schedule grants itself every five minutes.

**Every door that arms a row asks, and so does the fire itself.** Creating
one, enabling one, a reload that changes what a row runs, and the moment it
fires. The answer is never copied onto the row: `permissions.yaml` can change
after a row is armed, and a row that was legal in March must not still be
running in June on March's answer. The fire-time check is the one that holds
the line, because every other door can be bypassed by writing the file, and
`schedule_update` is itself auto under full autonomy. Counting the doors here
is how the next one goes unnoticed, so the rule is the property: nothing arms
or fires without asking.

**Two questions, not one, because the runtime asks two.**
`decide.py::evaluate` consults `tool.check_permissions` BEFORE the policy, and
a tool that answers ASK there is not overruled by a posture: `bash` forces ASK
on its own security checks whatever `permissions.yaml` says, and
`agent_create` and `skill_create` return ASK unconditionally so a headless
override cannot bypass the operator. A gate that read the posture alone called
those rows schedulable, and they arm, fail on every fire and end up disabled by
the generic breaker with nothing said to anybody.

**Deliberately stricter than `evaluate`, in one place.** Unattended, `evaluate`
lets an ASK tool through when it is read-only. This does not: the rule the
operator set is that a scheduled tool is an `auto` tool, and "it depends
whether the tool only reads" is not a rule anybody can hold in their head while
deciding whether a row is safe to arm. So the gate refuses strictly more than
the runtime would, never less, and that is the direction an error here has to
lean.

Nothing here replaces the permission check. The tool still goes through
`execute_tool`, so path overrides, mode overrides and the bash rules all apply
exactly as they do to a tool the assistant calls in a chat. This decides only
whether a row may exist at all.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

log = logging.getLogger(__name__)

#: The one handler that invokes a tool. Named here rather than in the job
#: module so the engine can consult the gate without importing the job.
TOOL_CALL_HANDLER = "tesseract.scheduler.tasks.tool_call.ToolCallJob"

#: The only posture a scheduled tool may resolve to.
REQUIRED_POSTURE = "auto"


class ToolNotSchedulable(ValueError):
    """A row named a tool it may not run, with the reason a person reads.

    `unknown` separates the two refusals that must not be answered the same
    way. "This tool needs approval" is a settled fact about the row, and the
    row should turn itself off over it. "I cannot see the permission settings
    from here" is a fact about the MOMENT, and turning a good row off over it
    would be the guard causing the outage.

    The shipped boot order does not produce that moment: `config/boot.yaml`
    puts `tool_registry` in the `core` layer and the scheduler in `wiring`,
    and layers run in sequence. What does produce it is an engine built
    without an app at all, which is every test harness and every direct
    construction, and a registry that failed to build. Both refuse, and only
    one is permanent.
    """

    def __init__(self, tool_name: str, reason: str, *, unknown: bool = False) -> None:
        super().__init__(reason)
        self.tool_name = tool_name
        self.reason = reason
        self.unknown = unknown


def scheduled_context(run_id: str = "", app: Any = None) -> Any:
    """The context a scheduled call is made under, built in one place.

    The gate asks the tool whether a call needs approval and the job then makes
    that call, and the two must ask under the same conditions or the gate is
    answering about a different situation than the one that happens. No tool
    reads the context inside `check_permissions` today, so nothing depends on
    that yet; a second constructor is how it would stop being true without
    anyone noticing, which is why this takes an argument rather than the fire
    path building its own.

    **The providers are what a tool needs to DO its work, and half the roster
    is useless without them.** `tool_search`, `schedule_run` and every other
    tool that reaches back into the runtime answers "registry not wired in
    this runtime" when they are absent, so a row naming one passed the gate
    and then failed on every single fire. They are execution wiring rather
    than an input to the decision, which is why the gate may ask without them
    and the fire may not run without them.

    `ask_fn` is left unset on purpose, at both. Unattended is the truth about
    a scheduled run, and a tool that wants an operator has to be told there is
    not one.
    """
    from tesseract.kernel.tools.base import ToolContext

    if app is None or not hasattr(app, "get"):
        return ToolContext(session_id="scheduler", current_call_id=run_id, run_id=run_id)
    return ToolContext(
        session_id="scheduler",
        current_call_id=run_id,
        run_id=run_id,
        tool_registry_provider=lambda: app.get("tool_registry"),
        scheduler_provider=lambda: app.get("scheduler"),
    )


class _Snapshot:
    """One reading of the registry, held for the length of one fire.

    `registry.tools` is rebound wholesale by `home_tools.py::sync_home_tools`,
    from a worker thread, whenever a tool of the operator's own is reloaded.
    The gate resolves a tool and the dispatcher resolves the same NAME again a
    moment later, and between those two lookups the dict can be replaced: what
    ran would then be a different object from the one that was approved, and
    `evaluate` is deliberately looser than the gate, so a read-only
    replacement answering ASK would be allowed through where the gate would
    have refused it.

    `ToolRegistry.schemas_for_adapter` already answers this the same way and
    says so in its own comment. This is that discipline applied to the one
    other place that reads the registry twice about one call.
    """

    #: `get` is the whole surface, deliberately. It is the only member the
    #: dispatch path touches (`brain/tools.py::_dispatch_tool`), and the policy
    #: is read off the LIVE registry by `runtime_from_app` before anything is
    #: snapshotted, so carrying a second copy of it here would be a second
    #: answer nobody asked for. A caller needing more of the registry should
    #: take the live one and say why.
    def __init__(self, registry: Any) -> None:
        self._tools = dict(getattr(registry, "tools", {}) or {})

    def get(self, name: str) -> Any:
        return self._tools.get(name)


def snapshot_registry(registry: Any) -> Any:
    """The registry as it stands now, for a caller that must read it twice."""
    if registry is None:
        return None
    return _Snapshot(registry)


def read_call(config: Mapping[str, Any] | None) -> tuple[str, dict[str, Any]]:
    """The tool name and arguments out of a row's config block.

    Raises `ToolNotSchedulable` when the block does not name a tool, so a row
    that cannot say what it runs is refused at the same door as one naming a
    tool it may not run.
    """
    cfg = dict(config or {})
    name = str(cfg.get("tool") or "").strip()
    if not name:
        raise ToolNotSchedulable(
            "",
            "this job runs a tool, so its config needs a `tool` naming which "
            "one. Add it, or use a different handler.",
        )
    args = cfg.get("args")
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise ToolNotSchedulable(
            name,
            f"`args` for {name} has to be a mapping of argument names to "
            f"values, and this one is a {type(args).__name__}.",
        )
    return name, dict(args)


def check_call(
    tool_name: str,
    args: Mapping[str, Any],
    *,
    registry: Any,
    policy: Any,
    context: Any = None,
) -> None:
    """Refuse unless this exact call would run unattended without asking.

    Resolved with the row's real arguments, not the tool name alone, because
    a path override or the bash read-only allowlist can change the answer for
    one call and not another.

    **On the VALIDATED input, which is what `evaluate` resolves on**
    (`decide.py::evaluate` calls `policy.get_posture(name, validated)`, and
    that dumps the model). An input model may normalise the very field a path
    override matches on: `FileWriteInput` prepends `tesseract/` to a bare
    relative path, so the raw string and the validated one are two different
    prefix matches. Resolving on the raw dict made the gate and the runtime
    two answers to one question, which is the thing this module exists to
    stop.
    """
    if registry is None or policy is None:
        raise ToolNotSchedulable(
            tool_name,
            "this part of the runtime cannot see the permission settings just "
            "now, so it cannot tell whether the job would need approval. "
            "Nothing ran. Try again from the running app.",
            unknown=True,
        )
    tool = registry.get(tool_name)
    if tool is None:
        raise ToolNotSchedulable(
            tool_name,
            f"there is no tool called {tool_name}. Check the name, and if it "
            "is one of your own, make sure it loaded.",
        )
    try:
        validated = tool.input_schema(**dict(args))
    except Exception as exc:  # noqa: BLE001 — any validation error is the same answer
        raise ToolNotSchedulable(
            tool_name,
            f"those arguments are not what {tool_name} accepts, so the job "
            f"would fail every time it ran: {exc}",
        ) from exc

    posture = policy.resolve_posture(tool_name, validated.model_dump())
    if posture == "deny":
        raise ToolNotSchedulable(
            tool_name,
            f"{tool_name} is set to deny, so it does not run anywhere, on a "
            "schedule or otherwise. Nothing was armed.",
        )
    if posture != REQUIRED_POSTURE:
        raise ToolNotSchedulable(
            tool_name,
            f"{tool_name} asks before it runs, and a scheduled job fires when "
            "nobody is there to answer, so it would be refused every time. "
            f"Nothing was armed. Set {tool_name} to auto in permissions.yaml "
            "if you want it running on its own, or leave it as it is and ask "
            "for it when you need it.",
        )
    # Not an `else`, and not skipped when the posture is already `auto`. The
    # case this exists for IS the auto one: `bash` under a mode that sets it
    # auto still answers ASK for the commands its own checks single out.
    _check_the_tools_own_answer(tool, tool_name, validated, context)


def _check_the_tools_own_answer(
    tool: Any, tool_name: str, validated: Any, context: Any = None
) -> None:
    """The half of the decision that is not the posture's to make.

    `decide.py::evaluate` asks the tool before it asks the policy, and what the
    tool says is not overruled by a posture. `bash` answers ASK for the
    commands its own security checks single out, whatever `permissions.yaml`
    says about `bash`; `agent_create` and `skill_create` answer ASK always, so
    that a headless override cannot approve on the operator's behalf. Reading
    the posture alone called all three schedulable.

    A tool that raises here is refused rather than trusted. It is being asked a
    question about a call that has not happened yet, and one that cannot answer
    is not one to arm unattended.
    """
    from tesseract.kernel.tools.base import PermissionResult

    try:
        answer = tool.check_permissions(validated, context or scheduled_context())
    except Exception as exc:  # noqa: BLE001 — cannot answer means not armed
        raise ToolNotSchedulable(
            tool_name,
            f"{tool_name} could not say whether this call needs approval, so "
            f"it is not something to run unattended: {exc}",
        ) from exc
    if answer in (PermissionResult.PASSTHROUGH, PermissionResult.ALLOW):
        return
    raise ToolNotSchedulable(
        tool_name,
        f"{tool_name} decides for itself that this particular call needs you, "
        "whatever its setting says, so a scheduled run would be refused every "
        "time. Change what the job asks it to do. Setting it to auto will not "
        "help, and is not meant to.",
    )


def check_job(
    handler: str,
    config: Mapping[str, Any] | None,
    *,
    registry: Any,
    policy: Any,
    context: Any = None,
) -> None:
    """Gate a whole row. A row on any other handler is not this gate's business."""
    if handler != TOOL_CALL_HANDLER:
        return
    tool_name, args = read_call(config)
    check_call(tool_name, args, registry=registry, policy=policy, context=context)


def runtime_from_app(app: Any) -> tuple[Any, Any]:
    """`(registry, policy)` off the running app, or `(None, None)`.

    Both come back empty together when either is missing: `check_call` refuses
    on either, and a caller reasoning about one of them alone would be
    reasoning about half an answer.
    """
    if app is None or not hasattr(app, "get"):
        return None, None
    registry = app.get("tool_registry")
    if registry is None:
        return None, None
    policy = getattr(registry, "permission_policy", None)
    if policy is None:
        return None, None
    return registry, policy


__all__ = [
    "REQUIRED_POSTURE",
    "scheduled_context",
    "snapshot_registry",
    "TOOL_CALL_HANDLER",
    "ToolNotSchedulable",
    "check_call",
    "check_job",
    "read_call",
    "runtime_from_app",
]

"""context_set — turn the fold setting, from wherever the operator is.

`context_read` answers "how full is this conversation" on any surface. Nothing
answered "make it fold sooner" anywhere but the Settings pane, and rulings 22
and 23 are that a control which exists only in the cockpit is a control that
does not exist for the three days someone is away from the desk. This one is
known to have been mistuned for months, which is how the whole workstream
started.

**A writer, not a channel feature.** `brain/compaction_control.py` is the one
implementation and the Settings pane calls the same function. A second writer
that forgot to update the live sessions would be a setting that works until
restart, and a second set of bounds would be a channel that can set something
the pane refuses.

`default_posture="ask"`: it rewrites `roles.yaml` and changes how every open
conversation behaves. The operator answers that, on whatever surface they are
on.
"""

from __future__ import annotations

import logging
from typing import ClassVar, Optional

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult

log = logging.getLogger(__name__)


class ContextSetInput(BaseModel):
    fold_at: Optional[float] = Field(
        default=None,
        description=(
            "How full a conversation may get before it folds, as a share of "
            "the context window. 0.25 means a quarter. Lower folds sooner and "
            "keeps less in front of the model; higher carries more and costs "
            "more per turn."
        ),
    )
    keep_recent_turns: Optional[int] = Field(
        default=None,
        description=(
            "How many recent turns survive a fold word for word. A turn is one "
            "message from the person and everything that happened before their "
            "next one, so a turn with twenty tool calls is still one turn."
        ),
    )


class ContextSetTool(Tool):
    default_posture = "ask"

    risk_class: ClassVar[str] = "operator_gate"

    group: ClassVar[str] = "checking-your-state"
    summary: ClassVar[str] = (
        "Change when a conversation folds, and how much of it survives."
    )
    use_when: ClassVar[str] = (
        "Use when you are asked to fold sooner or later, to hold more or less "
        "of a conversation in front of you, or to change how much survives a "
        "fold. It applies to every open conversation and to the next one, not "
        "just this one, and it lasts until it is changed again."
    )
    not_when: ClassVar[str] = (
        "to find out where things stand, which is `context_read`; to fold this "
        "conversation right now, which is not a setting; for the model's own "
        "window, which is the catalog's and not an operator setting."
    )
    depends_on: ClassVar[str] = ""

    def __init__(self, app_provider=None) -> None:
        """`app_provider` resolves the Mirror app at call time.

        The change has to reach every live chat session and the sub-agent
        tools, and those live on the app. Resolved late rather than captured,
        because the tool is built at boot and the app outlives individual
        wirings.
        """
        self._app_provider = app_provider

    @property
    def name(self) -> str:
        return "context_set"

    @property
    def input_schema(self) -> type[BaseModel]:
        return ContextSetInput

    def is_concurrency_safe(self) -> bool:
        return False

    def is_read_only(self) -> bool:
        return False

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        assert isinstance(tool_input, ContextSetInput)
        from tesseract.brain import compaction_control

        if tool_input.fold_at is None and tool_input.keep_recent_turns is None:
            return ToolResult(
                output=(
                    "context_set: nothing to change. Give `fold_at`, "
                    "`keep_recent_turns`, or both."
                ),
                is_error=True,
            )

        try:
            ratio = (
                compaction_control.validate_ratio(tool_input.fold_at)
                if tool_input.fold_at is not None
                else None
            )
            keep = (
                compaction_control.validate_keep_recent(tool_input.keep_recent_turns)
                if tool_input.keep_recent_turns is not None
                else None
            )
        except ValueError as exc:
            return ToolResult(output=f"context_set: {exc}", is_error=True)

        app = self._app_provider() if self._app_provider is not None else None
        if app is None:
            # Nothing durable has happened, and saying so matters: a caller
            # told only "failed" cannot tell whether the setting took.
            return ToolResult(
                output=(
                    "context_set: the runtime this would change is not "
                    "reachable from here, so nothing was changed. The setting "
                    "is unchanged and this conversation still folds where it "
                    "did."
                ),
                is_error=True,
            )

        try:
            compaction_control.apply_compaction(
                app, _roles_yaml(app), ratio=ratio, keep_recent=keep
            )
        except KeyError as exc:
            return ToolResult(
                output=(
                    f"context_set: roles.yaml has no {exc}, so the setting "
                    "could not be written. Nothing was changed."
                ),
                is_error=True,
            )
        except Exception as exc:
            log.exception("context_set: writing the fold setting failed")
            return ToolResult(
                output=(
                    f"context_set: the setting did not finish being applied "
                    f"({type(exc).__name__}). The file is written first, so it "
                    "probably carries the new value while some open "
                    "conversations do not. They will pick it up on restart; "
                    "check Settings if you need to be sure."
                ),
                is_error=True,
            )

        # The CALLER's conversation, through the same hook `context_read`
        # measures with. `compaction_control.describe` walks open sessions and
        # answers with the first it can read, which is not necessarily this
        # one, and "this one sits at N tokens" has to be this one.
        now = _this_conversation(context) or compaction_control.describe(app)
        return ToolResult(output=_describe(ratio, keep, now), metadata=now)


def _roles_yaml(app):
    """The same file the Settings pane writes.

    `app['tesseract_dir']` is resolved once at boot; `config_dir()` re-resolves
    the environment at call time. They agree in a running process and differ the
    moment `TESSERACT_HOME` moves, and two surfaces writing different files is
    the fork this tool exists on the shared writer to avoid.
    """
    from tesseract.paths import config_dir

    root = app.get("tesseract_dir")
    return (root / "config" / "roles.yaml") if root else config_dir() / "roles.yaml"


def _this_conversation(context: ToolContext) -> dict | None:
    """Where the ASKING conversation now folds, or None if it cannot be read.

    `context.context_report` is the hook `context_read` uses, so this is the
    same measurement and not a second one."""
    reader = getattr(context, "context_report", None)
    if reader is None:
        return None
    try:
        from tesseract.brain import context_report

        report = reader()
        return {
            "fold_at_tokens": context_report.fold_ceiling(report),
            "tokens_now": int(report.get("foldable_tokens") or report.get("tokens") or 0),
            "context_window": int(report.get("context_window") or 0),
        }
    except Exception:
        log.exception("context_set: measuring the asking conversation failed")
        return None


def _describe(ratio: float | None, keep: int | None, now: dict) -> str:
    """What changed, and what it means for the conversation asking.

    Answering with the number that was just typed tells the operator nothing.
    The fold point is the thing they were actually setting.
    """
    said = []
    if ratio is not None:
        said.append(f"a conversation now folds at {round(ratio * 100)}% of the window")
    if keep is not None:
        said.append(f"a fold now keeps the last {keep} turns word for word")
    lines = [". ".join(said) + "."]

    fold_at = int(now.get("fold_at_tokens") or 0)
    tokens = int(now.get("tokens_now") or 0)
    if fold_at:
        if tokens >= fold_at:
            lines.append(
                f"This conversation is already past that ({tokens:,} of "
                f"{fold_at:,} tokens), so it folds at the end of this turn."
            )
        else:
            lines.append(
                f"This one sits at {tokens:,} tokens and folds at {fold_at:,}."
            )
    else:
        lines.append(
            "No open conversation could be measured, so this takes effect on "
            "the next one."
        )
    lines.append("It applies to every open conversation and lasts until changed.")
    return " ".join(lines)

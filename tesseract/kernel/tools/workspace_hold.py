"""workspace_hold — which of the operator's own documents must be asked about.

Six documents used to get one answer. Under a mode that acts without asking,
a change to `OPERATING.md`, the rules the assistant works by, was applied as
freely as a line added to `DIARY.md`. Those are not the same act, and until
now nothing in the runtime could tell them apart.

So the operator names the ones held back, and the mode decides the rest. A
held document files its change in the workspace inbox and waits, whatever the
mode says. Nothing here can go the other way: choosing not to hold a document
lets it follow the mode, and the mode's own answer is still the stricter of
the two.

**It is a tool and not a field on a panel.** The Settings panel sends this,
the same words typed in the cockpit send this, and the same words said on a
phone send this. Away from the desk for three days, a control that lives only
in the cockpit is a control that does not exist, and the choice about which
documents the assistant may rewrite unattended is exactly the one an operator
wants while they are away.

`default_posture="ask"`: it decides what the assistant is allowed to change
without asking, so the assistant asking to change it is asked. The operator
pressing the panel's switch, or typing the command, runs through `run_slash`,
which skips posture because they already are the approval.
"""

from __future__ import annotations

import asyncio
import logging
from typing import ClassVar

from pydantic import BaseModel, Field

from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult

logger = logging.getLogger(__name__)


class WorkspaceHoldInput(BaseModel):
    document: str = Field(
        default="",
        description=(
            "Which document, by file name (for example 'OPERATING.md'). "
            "Leave it out to be told what every document answers now."
        ),
    )
    must_ask: bool | None = Field(
        default=None,
        description=(
            "True to hold the document back, so a change to it always waits "
            "for the operator. False to let it follow the security mode. "
            "Leave it out to read the current answer without changing it."
        ),
    )


class WorkspaceHoldTool(Tool):
    default_posture = "ask"

    risk_class: ClassVar[str] = "propose"

    group: ClassVar[str] = "checking-your-state"
    summary: ClassVar[str] = (
        "Says which of the operator's own documents a change must be asked "
        "about, and changes that."
    )
    use_when: ClassVar[str] = (
        "Use when the operator wants one of their documents held back from "
        "unattended editing, or let go again, or wants to know which ones are "
        "held now. Call it with no arguments to read the list. It answers with "
        "every document and what a change to it does, so relay that rather "
        "than saying it was changed."
    )
    not_when: ClassVar[str] = (
        "to propose a change to one of those documents, which is "
        "`propose_change`; to settle a change already waiting, which is "
        "`workspace_decide`; to change the security mode, which is not a "
        "setting a tool reaches."
    )
    depends_on: ClassVar[str] = ""

    @property
    def name(self) -> str:
        return "workspace_hold"

    @property
    def input_schema(self) -> type[BaseModel]:
        return WorkspaceHoldInput

    def is_concurrency_safe(self) -> bool:
        # It writes a file. This only keeps two calls in ONE turn apart; two
        # turns, or the panel's switch and a phone at once, are held by
        # `set_document_hold`'s own lock, which is where the round trip is.
        return False

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        from tesseract import paths
        from tesseract.orchestrator.panel_refresh import (
            WORKSPACE_DOCUMENTS_KEY,
            publish_stale,
        )
        from tesseract.permissions.workspace_holds import (
            HoldError,
            set_document_hold,
        )

        inp: WorkspaceHoldInput = tool_input  # type: ignore[assignment]
        policy = getattr(context, "policy", None)
        if policy is None:
            return ToolResult(
                output=(
                    "No permission policy is loaded here, so what the "
                    "documents answer could not be read and nothing was "
                    "changed."
                ),
                is_error=True,
            )

        document = inp.document.strip()
        if not document or inp.must_ask is None:
            return ToolResult(
                output=_said(policy),
                metadata={"changed": False},
            )

        try:
            was, now = await asyncio.to_thread(
                set_document_hold, paths.config_dir(), document, inp.must_ask
            )
        except HoldError as exc:
            return ToolResult(output=str(exc), is_error=True)
        except (OSError, ValueError) as exc:
            logger.exception("workspace_hold: the block could not be written")
            return ToolResult(
                output=(
                    "The permission file could not be written, so nothing "
                    f"changed and {document} still answers the way it did "
                    f"({type(exc).__name__}). The backend log names the file."
                ),
                is_error=True,
            )

        # The file is what the next boot reads; this is what the next turn
        # reads. The config watcher would reload the policy a moment from now
        # either way, and a decision the operator just took should be in force
        # before that rather than after it.
        try:
            policy.set_workspace_document_hold(document, now)
        except (AttributeError, ValueError):
            logger.warning(
                "workspace_hold: live policy not updated for %s", document
            )

        # The Settings switch shows what is in force, and this call is what
        # put it there. Best effort, and after the write: a decision that
        # landed must not be undone because no panel was listening.
        if was != now:
            publish_stale(WORKSPACE_DOCUMENTS_KEY)

        head = (
            f"{document} was already {_answer(was)}."
            if was == now
            else f"{document} was {_answer(was)} and is now {_answer(now)}."
        )
        return ToolResult(
            output=f"{head}\n\n{_said(policy)}",
            metadata={"document": document, "was": was, "held": now, "changed": was != now},
        )


def _answer(held: bool) -> str:
    return "held back" if held else "following the security mode"


def _said(policy: object) -> str:
    """Every document and what a change to it does now.

    Read through the policy's own resolver rather than joined here, so this
    and the Settings panel cannot describe one file two ways.
    """
    postures = policy.workspace_document_postures()  # type: ignore[attr-defined]
    held = policy.workspace_document_holds  # type: ignore[attr-defined]
    lines = []
    for name, posture in postures.items():
        if posture == "ask":
            what = "a change waits for you"
        elif posture == "auto":
            what = "a change is applied and filed for you to read"
        else:
            what = "a change is refused"
        why = "held back" if name in held else "following the security mode"
        lines.append(f"- {name}: {what} ({why})")
    return "What a proposed change to each of them does:\n" + "\n".join(lines)


__all__ = ["WorkspaceHoldInput", "WorkspaceHoldTool"]

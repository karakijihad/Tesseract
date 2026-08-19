"""screen_look — answer a question about what is on the operator's screen. ASK.

Everything else the assistant knows about the Mirror is *structure*: which view
is open, what a store holds, whether a card was registered. None of it can see
that a card rendered as a black rectangle, that text is clipped, or that a
button is missing — which is the class of problem the operator actually asks
about ("this video isn't working, look at it").

It answers a QUESTION rather than returning a description. Two reasons, and the
second is the design:

- A targeted pass is cheaper and more useful than a scene description the
  caller has to hope contains the answer.
- `ToolResult.output` is a `str`. A tool cannot hand the model a picture, and
  making it able to would mean per-adapter work with a real chance of failing
  on the primary. So the picture goes to a vision model here, and what comes
  back is text. The cost is honest and worth stating: the caller gets an
  ANSWER, not the frame, and cannot re-examine it — ask again with a different
  question instead.

The frame is never written to disk. It is a photograph of the operator's
screen — it can hold a key, a private conversation, or an unrelated
application — and a file that lives until the next capture is a file that may
live forever. The bytes go to the vision model from memory and are dropped.

Every call takes one frame of one thing: the display the app window is on, at
native resolution. There is no window crop, no widening flag and no second
capture shape, because each of those needed the answer to explain which of
several pictures it was looking at.

ASK posture is not incidental. A capture is a picture of the operator's screen
going to a model that may not be local, which is an outbound action under the
project's own rule that every outbound call prompts. `permissions.yaml` is the
authority — one posture, one prompt, and it relaxes under `headless`. This tool
used to raise a second approval of its own for the wider capture; that is what
turned "the window was not found" into a refusal the operator's spoken yes
could not clear, because they had answered a different question.
"""

from __future__ import annotations

import logging
from typing import ClassVar

from pydantic import BaseModel, Field

from tesseract.config.runtime_limits import (
    default_runtime_config_path,
    load_screen_look_answer_chars,
)
from tesseract.kernel.tools.base import Tool, ToolContext, ToolResult
from tesseract.orchestrator.screen.capture import capture_screen

log = logging.getLogger(__name__)


class ScreenLookInput(BaseModel):
    question: str = Field(
        description=(
            "What you want to know about the screen — 'is the video card "
            "playing or black?', 'what does the error say?', 'which panel is "
            "in front?'. Ask something specific; a vague question gets a vague "
            "answer and costs the same."
        )
    )


class ScreenLookTool(Tool):
    default_posture: ClassVar[str] = "ask"
    risk_class: ClassVar[str] = "autonomous"
    # The screen is full of text this runtime did not write — a web page in a
    # browser surface, a terminal buffer, an inbound channel message. A vision
    # model transcribes whatever is there, so the answer is untrusted input
    # wearing a tool result's clothes, and "ignore previous instructions" on a
    # page the operator happens to have open must not arrive unwrapped.
    untrusted_source: ClassVar[bool] = True

    group: ClassVar[str] = "looking-for-yourself"
    summary: ClassVar[str] = "Look at the operator's own screen and answer one question about it."
    use_when: ClassVar[str] = (
        "The question is about what something LOOKS like — a card that rendered "
        "blank, text cut off, a control that isn't there, an error the operator "
        "can see and you cannot. No other tool can: the rest report structure, "
        "not pixels. Ask one specific thing; you get words back, not the image."
    )
    not_when: ClassVar[str] = (
        "The page is one you opened headlessly. That is `browser_screenshot`. "
        "This photographs the operator's real display — it prompts them every "
        "time, and it sees whatever else they have open on it."
    )

    @property
    def name(self) -> str:
        return "screen_look"

    @property
    def input_schema(self) -> type[BaseModel]:
        return ScreenLookInput

    async def run(self, tool_input: BaseModel, context: ToolContext) -> ToolResult:
        # Late import: the vision handler reaches into `brain.boot` to resolve
        # the chat_brain chain, and `boot` registers this tool — importing it
        # at module scope closes that loop and nothing loads. Same reason
        # `invoke_agent` late-imports the Mirror upload helpers.
        from tesseract.integrations._handlers.image import (
            ImageHandlerError,
            describe_image,
        )

        inp: ScreenLookInput = tool_input  # type: ignore[assignment]
        question = inp.question.strip()
        if not question:
            return ToolResult(
                output="screen_look needs a question — what do you want to know about the screen?",
                is_error=True,
            )

        try:
            answer_chars = load_screen_look_answer_chars(default_runtime_config_path())
        except (FileNotFoundError, ValueError) as exc:
            return ToolResult(output=f"screen_look: {exc}", is_error=True)

        try:
            capture = await capture_screen()
        except ImportError:
            return ToolResult(
                output=(
                    "screen_look: Pillow is not installed, so nothing can capture the "
                    "screen. Reinstall the app's Python dependencies."
                ),
                is_error=True,
            )
        except Exception as exc:  # noqa: BLE001 — surface as a clean tool error
            log.exception("screen_look: capture failed")
            return ToolResult(output=f"screen_look: capture failed: {exc}", is_error=True)

        try:
            answer = await describe_image(
                capture.png,
                mime="image/png",
                prompt=_prompt_for(question, capture.monitor),
                max_chars=answer_chars,
                # A paid vision call. Every other paid path in this runtime
                # bills the ledger; without this one the spend is invisible to
                # the daily cap, and `headless` auto-allows this tool.
                cost_ledger=getattr(context, "cost_ledger", None),
            )
        except ImageHandlerError as exc:
            return ToolResult(
                output=(
                    f"screen_look: the frame was captured but no vision model could read "
                    f"it ({exc}). Nothing here can answer from the source."
                ),
                is_error=True,
            )

        return ToolResult(
            output=answer,
            metadata={
                "width": capture.width,
                "height": capture.height,
                "monitor": capture.monitor,
            },
        )


def _prompt_for(question: str, monitor: str) -> str:
    where = f" ({monitor})" if monitor else ""
    return (
        f"This is a screenshot of the operator's screen{where} — the display "
        f"the TESSERACT application is on, including anything else open on it. "
        f"Answer this question about it as directly as you can, from what is "
        f"visible: {question}\n\n"
        "If the answer is not visible in the image, say so plainly rather than "
        "guessing. Report what is actually on screen even when it looks broken "
        "— a blank panel, an error, a missing control are all useful answers. "
        "No preamble, no markdown."
    )

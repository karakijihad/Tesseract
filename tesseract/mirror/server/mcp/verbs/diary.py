"""diary.* and feedback.* MCP verbs — the narrative and the lesson.

Two small surfaces that close the same gap from opposite ends. `diary.append`
writes the narrative the assistant reads back, so work done in a CLI is part of the same
story rather than an unexplained change in the store. `feedback.propose` records
a lesson as a proposal — it changes nothing by itself, which is why it can run
unattended: a proposal the operator never sees is the thing being fixed, not a
risk being taken.
"""

from __future__ import annotations

from tesseract.kernel.tools.diary_append import DiaryAppendInput
from tesseract.kernel.tools.propose_change import ProposeChangeInput
from tesseract.mirror.server.mcp.verbs._base import make_tool_verb

diary_append = make_tool_verb("diary_append", DiaryAppendInput)
feedback_propose = make_tool_verb("propose_change", ProposeChangeInput)

__all__ = ["diary_append", "feedback_propose"]

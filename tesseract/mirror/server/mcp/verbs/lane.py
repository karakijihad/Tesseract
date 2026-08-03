"""lane.* MCP verbs (P3) — route to the lane_* kernel tools, which drive the
controller-owned lanes over IPC via ``lane_manager_provider`` /
``named_lane_manager_provider``. Verb ``ensure`` maps to ``lane_named_ensure``."""

from __future__ import annotations

from tesseract.kernel.tools.lane_close import LaneCloseInput
from tesseract.kernel.tools.lane_named_ensure import LaneNamedEnsureInput
from tesseract.kernel.tools.lane_read import LaneReadInput
from tesseract.kernel.tools.lane_send import LaneSendInput
from tesseract.kernel.tools.lane_turn import LaneTurnInput
from tesseract.mirror.server.mcp.verbs._base import make_tool_verb

lane_ensure = make_tool_verb("lane_named_ensure", LaneNamedEnsureInput)
lane_send = make_tool_verb("lane_send", LaneSendInput)
lane_turn = make_tool_verb("lane_turn", LaneTurnInput)
lane_read = make_tool_verb("lane_read", LaneReadInput)
lane_close = make_tool_verb("lane_close", LaneCloseInput)

__all__ = ["lane_ensure", "lane_send", "lane_turn", "lane_read", "lane_close"]

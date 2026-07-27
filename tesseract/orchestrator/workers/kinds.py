"""Shared worker-kind vocabulary for the autonomy worker-lifecycle package.

Extracted from the (now-deleted) mission orchestrator's domain models —
this enum is stored on-disk in worker/activity records, so member names
and values are byte-identical to the original.
"""

from __future__ import annotations

from enum import Enum


class WorkerKind(str, Enum):
    TARS_SELF = "tars_self"
    CLAUDE_CLI = "claude_cli"
    CODEX_CLI = "codex_cli"
    MARKDOWN_AGENT = "markdown_agent"
    TERMINAL = "terminal"
    TARS_CONTROLLER = "tars_controller"

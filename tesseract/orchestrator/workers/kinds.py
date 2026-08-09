"""Shared worker-kind vocabulary for the autonomy worker-lifecycle package.

Values are config keys: `agenda.yaml::worker_timeouts` and the
`mirror.yaml` worker-lane block are keyed by them. They name a delegation
seat, never a provider — which CLI or API model fills a seat is roles.yaml,
so a seat survives the operator repointing it.
"""

from __future__ import annotations

from enum import Enum


class WorkerKind(str, Enum):
    AGENT_SELF = "agent_self"
    CODER_SEAT = "coder_seat"
    AUDITOR_SEAT = "auditor_seat"
    MARKDOWN_AGENT = "markdown_agent"
    TERMINAL = "terminal"
    AGENT_CONTROLLER = "agent_controller"

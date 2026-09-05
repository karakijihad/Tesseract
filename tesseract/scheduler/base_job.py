from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar

from tesseract.scheduler.types import JobContext, JobResult


class BaseJob(ABC):
    """Contract: return JobResult(ok=False, detail=...) on failure — never raise."""

    # `True` for jobs that talk to a chat-style adapter (LLM). The Mirror
    # Schedule view reads this to decide whether to render the role
    # dropdown for the row. The matching default role lives in
    # `default_model_role` so a `schedule.yaml::model_role` override always
    # has a baseline to fall back to. Subclasses MUST keep these in sync —
    # `uses_llm=True` requires a non-empty `default_model_role` or
    # `default_model_chain`.
    uses_llm: ClassVar[bool] = False
    default_model_role: ClassVar[str | None] = None
    # The chain this job rides when it names no role. Roles are pillars, and a
    # job that is not one of them would otherwise get a role invented for it
    # hold a budget line — this is what removes the need. A job declares one or
    # the other; an operator's `model_role` override still wins over both.
    default_model_chain: ClassVar[str | None] = None
    # The manifest entry this job's spend belongs to, when that is not the row
    # name. A row named after its entry needs nothing here and bills to itself.
    # A handler armed under many operator-chosen names does: without it every
    # armed row spends under a name no ceiling is declared for, so the ceiling
    # that names the entry refuses nothing while still raising the global cap.
    # Checked against `scheduler/manifest/registry.py` by the AR-7b suite.
    billing_entry: ClassVar[str] = ""

    @abstractmethod
    async def run(self, ctx: JobContext) -> JobResult:
        ...

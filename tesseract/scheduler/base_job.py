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
    # has a baseline to fall back to. Subclasses MUST keep these two in
    # sync — `uses_llm=True` requires a non-empty `default_model_role`.
    uses_llm: ClassVar[bool] = False
    default_model_role: ClassVar[str | None] = None

    @abstractmethod
    async def run(self, ctx: JobContext) -> JobResult:
        ...

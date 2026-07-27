"""Vetter response schema + lenient JSON parsing.

Mirrors ``autonomy_heartbeat._parse_response``: extract the first JSON
object embedded in the model's raw output, validate, and drop
individually-invalid verdicts rather than discarding the whole batch.
Empty/garbage input yields an empty ``VetBatchResult`` — the job's
fail-safe then leaves the corresponding items UNVETTED.

A ``MERGE`` verdict with no ``merge_into`` id is downgraded to
``REJECT`` here (can't merge into nothing). A ``MERGE`` verdict whose
``merge_into`` id doesn't resolve to a real agenda item is downgraded
to ``REJECT`` by the job (``scheduler/tasks/autonomy_vetter.py``) —
the parser has no store access to check existence.
"""

from __future__ import annotations

import json
import logging
import re
from enum import Enum

from pydantic import BaseModel, Field, ValidationError, model_validator

log = logging.getLogger(__name__)


class VetVerdict(str, Enum):
    PROMOTE = "promote"
    REJECT = "reject"
    MERGE = "merge"


class VetItemResult(BaseModel):
    id: str
    verdict: VetVerdict
    score: float = Field(ge=0.0, le=1.0, default=0.0)
    reason: str = Field(default="", max_length=500)
    merge_into: str | None = None

    @model_validator(mode="after")
    def _downgrade_unmergeable(self) -> "VetItemResult":
        if self.verdict == VetVerdict.MERGE and not self.merge_into:
            self.verdict = VetVerdict.REJECT
        return self


class VetBatchResult(BaseModel):
    verdicts: list[VetItemResult] = Field(default_factory=list)


_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def parse_vet_response(raw: str) -> VetBatchResult:
    """Extract + validate the verdicts envelope. Empty/garbage input
    returns an empty ``VetBatchResult``."""
    if not raw or not raw.strip():
        return VetBatchResult()
    match = _JSON_OBJECT_RE.search(raw)
    if match is None:
        log.warning("vetter: no JSON object in adapter output")
        return VetBatchResult()
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        log.warning("vetter: JSON parse failed")
        return VetBatchResult()
    if not isinstance(data, dict):
        return VetBatchResult()
    try:
        return VetBatchResult.model_validate(data)
    except ValidationError as exc:
        items = data.get("verdicts") or []
        if not isinstance(items, list):
            return VetBatchResult()
        kept: list[VetItemResult] = []
        for item in items:
            try:
                kept.append(VetItemResult.model_validate(item))
            except ValidationError:
                continue
        if not kept:
            log.warning("vetter: response failed validation: %s", exc)
        return VetBatchResult(verdicts=kept)


__all__ = ["VetBatchResult", "VetItemResult", "VetVerdict", "parse_vet_response"]

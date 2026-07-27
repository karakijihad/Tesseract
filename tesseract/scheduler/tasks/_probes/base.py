"""ProbeResult dataclass + RoleProbe Protocol — the AU-14 contract."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal, Protocol

DriftKind = Literal[
    "none",
    "uniform_output",
    "shape_mismatch",
    "http_error",
    "empty_output",
    "latency_spike",
    "schema_error",
    "unavailable",
]


@dataclass(frozen=True)
class ProbeResult:
    """A single probe's outcome.

    ``ok=True`` MUST imply ``drift_kind == "none"`` and vice versa — the
    JSONL reader (``tesseract.orchestrator.provider_health.tail_recent``)
    uses ``ok`` to bucket rows fast without parsing ``drift_kind``.
    ``evidence`` is free-form, recorded verbatim into the JSONL line so
    the AU-5 mapper has enough context to draft a proposal.
    """

    role: str
    ref: str
    ok: bool
    drift_kind: DriftKind
    evidence: dict[str, Any]
    probed_at: str  # ISO8601 UTC
    latency_ms: float
    source: Literal["probe", "production_tripwire"] = "probe"
    extra: dict[str, Any] = field(default_factory=dict)


class RoleProbe(Protocol):
    """Per-kind probe — implementations live in ``image_role.py`` etc.

    Implementations MUST be:
      - Stateless (the orchestrator constructs them lazily per tick).
      - Failure-safe — never raise; failures come back as ``ok=False``
        ``ProbeResult`` rows with the original exception stringified
        into ``evidence``.
    """

    role_kind: ClassVar[str]

    async def probe(self, role_name: str, ref: str) -> ProbeResult: ...


__all__ = ["DriftKind", "ProbeResult", "RoleProbe"]

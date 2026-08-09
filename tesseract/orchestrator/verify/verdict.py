"""The auditor's machine-readable verdict — schema, models, and a strict parser.

`failure_scenario` is required on every finding, and that is the load-bearing
part of the contract rather than a nicety. A critic with nothing to say is
still rewarded for looking useful; demanding a concrete inputs-to-wrong-output
statement is the cheapest available brake on that, and it makes each finding
independently checkable instead of a claim someone has to take on faith.

`file` + `line` + a normalised `claim` is the dedupe key the relay uses to tell
"the auditor is repeating an unaddressed finding" from "the auditor found
something new" — a set comparison rather than a judgment call.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

_SCHEMA_PATH = Path(__file__).resolve().parent / "schemas" / "verdict.json"

_WHITESPACE_RUN = re.compile(r"\s+")


class VerdictError(ValueError):
    """A reply that is not a usable verdict.

    Raised rather than returned so no caller can reach a default-constructed
    `Verdict()` and read its empty `findings` as CLEAN. An unparseable auditor
    reply means the review did not happen; it must not look like a review that
    found nothing.
    """


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file: str
    line: int
    severity: Literal["critical", "major", "minor"]
    claim: str
    failure_scenario: str
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)

    @property
    def dedupe_key(self) -> tuple[str, int, str]:
        return (self.file.replace("\\", "/").lower(), self.line, _normalise_claim(self.claim))


class Verdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: Literal["CLEAN", "ISSUES"]
    findings: list[Finding]

    @property
    def is_clean(self) -> bool:
        return self.verdict == "CLEAN"


def _normalise_claim(claim: str) -> str:
    return _WHITESPACE_RUN.sub(" ", claim).strip().lower()


@lru_cache(maxsize=1)
def verdict_schema() -> dict[str, Any]:
    """The JSON Schema as a dict — the form Claude's `--json-schema` takes inline."""
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


def verdict_schema_path() -> Path:
    """The schema on disk — the form Codex's `--output-schema` takes.

    Codex reads the file itself, so this must be a real path in the shipped
    tree. It is source, not generated state: the relay is inert without it.
    """
    if not _SCHEMA_PATH.is_file():
        raise VerdictError(f"verdict schema missing from the tree: {_SCHEMA_PATH}")
    return _SCHEMA_PATH


def parse_verdict(raw: Any) -> Verdict:
    """Coerce a CLI reply into a `Verdict`, or raise `VerdictError`.

    Accepts the dict Claude hands back under `structured_output` and the JSON
    string Codex emits as its final agent message. Every rejection path raises;
    none of them returns an empty verdict.
    """
    if raw is None:
        raise VerdictError("auditor returned no structured output")

    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            raise VerdictError("auditor returned an empty reply")
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise VerdictError(
                f"auditor reply is not valid JSON ({exc}); first 200 chars: {text[:200]!r}"
            ) from exc

    if not isinstance(raw, dict):
        raise VerdictError(f"auditor reply is {type(raw).__name__}, expected an object")

    try:
        verdict = Verdict.model_validate(raw)
    except ValidationError as exc:
        raise VerdictError(f"auditor reply does not match the verdict schema: {exc}") from exc

    # Cross-field consistency the JSON Schema cannot express. Either shape means
    # the relay has nothing it can act on, and the CLEAN-shaped one is the
    # dangerous direction: it would close a round while findings sit unread.
    if verdict.verdict == "CLEAN" and verdict.findings:
        raise VerdictError(
            f"auditor returned CLEAN with {len(verdict.findings)} finding(s) — contradictory"
        )
    if verdict.verdict == "ISSUES" and not verdict.findings:
        raise VerdictError("auditor returned ISSUES with no findings — nothing to act on")

    return verdict


__all__ = [
    "Finding",
    "Verdict",
    "VerdictError",
    "parse_verdict",
    "verdict_schema",
    "verdict_schema_path",
]

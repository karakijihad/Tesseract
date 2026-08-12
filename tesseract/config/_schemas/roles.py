"""Pydantic schema for ``roles.yaml``.

Strict at the top-level role/voice/embeddings blocks; permissive at the
per-role body because roles carry diverse override knobs
(``compact_threshold``, ``reasoning_effort_override``,
``daily_budget_usd``, etc).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

# The one definition of catalog-ref syntax, imported rather than restated —
# a second copy here would drift from what the loader actually enforces.
from tesseract.config.loader import _REF_RE as _CATALOG_REF_RE
from tesseract.config.loader import ROLE_MODES, ROLE_MODE_INACTIVE

# The loader keeps inactive roles as unresolved stubs (`_build_role` skips
# `_chain_refs` entirely), so a stale chain or ref on one never blocks boot.
# This gate must refuse exactly what the loader refuses and no more, or an
# otherwise-valid edit is rejected before it can be written.
_STUB_MODE = ROLE_MODE_INACTIVE


class _Permissive(BaseModel):
    model_config = ConfigDict(extra="allow")


class Embeddings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    primary: str


class Reranker(BaseModel):
    model_config = ConfigDict(extra="forbid")
    primary: str


class RoleBody(_Permissive):
    """A single cognition role. Everything not named here (overrides,
    budgets, notes) flows through as extras.

    A role names its chain one of two ways: `primary` (+ optional
    `fallbacks`) written out, or `chain` naming an entry in the top-level
    ``chains:`` block. Exactly one — the loader refuses both, because a
    role that says two different things about the same slot has no
    answer, only a precedence rule nobody would remember.
    """

    mode: str = "active"
    primary: str | None = None
    fallbacks: list[str] = Field(default_factory=list)
    chain: str | None = None
    notes: str | None = None

    @model_validator(mode="after")
    def _one_wiring_form(self) -> "RoleBody":
        """Refuse `chain` beside `primary`/`fallbacks`, and malformed refs.

        This schema is the gate a proposed yaml edit passes before it is
        written (`kernel/workspace_changes.py`), and the write is atomic. The
        loader refuses these, so without the check an approved edit could
        commit a `roles.yaml` that no longer boots — the failure surfacing
        later, as a rebuild error, against a file already on disk.

        Inactive roles are exempt for the same reason the loader exempts
        them: they are stubs it never resolves.
        """
        if self.mode not in ROLE_MODES:
            raise ValueError(
                f"mode {self.mode!r} must be one of {', '.join(sorted(ROLE_MODES))}"
            )
        if self.mode == _STUB_MODE:
            return self
        if self.chain is not None and (self.primary is not None or self.fallbacks):
            raise ValueError(
                f"sets both `chain: {self.chain}` and its own primary/fallbacks "
                "— keep `chain` to follow the shared chain, or drop it to pin "
                "this role independently"
            )
        for label, ref in [("primary", self.primary)] + [
            ("fallbacks", f) for f in self.fallbacks
        ]:
            if ref is not None and not _CATALOG_REF_RE.match(ref):
                raise ValueError(
                    f"{label} '{ref}' must match <tier>.<provider>.<model> "
                    "with tier in (api, cli, local)"
                )
        return self


class VoiceLane(_Permissive):
    """One ``voice.stt`` / ``voice.tts`` lane.

    `mode` is validated against the same set as a role's. `boot.py` gates the
    lane on ``mode != "active"``, so an unrecognised value here silently
    switches speech off — the role half of this exact defect is what
    `ROLE_MODES` was introduced to close, and leaving the sibling struct
    outside the set is how it would come back.
    """

    mode: str = "active"
    # Optional, and required back by the validator only when the lane is
    # active. The loader returns None for an inactive lane BEFORE it reads
    # `primary`, so a lane switched off with no model wired is a shape it
    # accepts — and a write gate that refuses what the runtime accepts is
    # its own defect, not extra safety.
    primary: str | None = None
    fallbacks: list[str] = Field(default_factory=list)
    settings: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _known_mode(self) -> "VoiceLane":
        if self.mode not in ROLE_MODES:
            raise ValueError(
                f"mode {self.mode!r} must be one of {', '.join(sorted(ROLE_MODES))}"
            )
        if self.mode != ROLE_MODE_INACTIVE and not self.primary:
            # Same sentence the loader raises for this yaml (`require_field`),
            # so the operator reads one message whether the edit went through
            # the governed write path or was hand-written and hit boot.
            raise ValueError("missing required key 'primary'")
        return self


class VoiceBlock(_Permissive):
    stt: VoiceLane | None = None
    tts: VoiceLane | None = None


class RolesConfig(BaseModel):
    """Top-level shape of ``roles.yaml``."""

    model_config = ConfigDict(extra="forbid")

    embeddings: Embeddings
    reranker: Reranker | None = None
    chains: dict[str, list[str]] = Field(default_factory=dict)
    roles: dict[str, RoleBody] = Field(default_factory=dict)
    voice: VoiceBlock | None = None

    @model_validator(mode="after")
    def _chains_resolve(self) -> "RolesConfig":
        """Every chain holds well-formed refs, and every `chain:` names one
        that exists and is not empty.

        `RoleBody` alone cannot check this — it never sees the `chains:`
        block. Without it the write gate would still accept an edit naming a
        chain that is missing or empty, which is the same unbootable-commit
        the per-role validator closes for the other half of the contract.

        Chain contents are checked whether or not a role names the chain: an
        unreferenced chain is still config that a later edit will point at.
        Roles are checked only when active, matching the loader's treatment
        of inactive roles as stubs.
        """
        for chain_name, refs in self.chains.items():
            for ref in refs:
                if not _CATALOG_REF_RE.match(ref):
                    raise ValueError(
                        f"chains.{chain_name} entry '{ref}' must match "
                        "<tier>.<provider>.<model> with tier in (api, cli, local)"
                    )
        for name, body in self.roles.items():
            if body.chain is None or body.mode == _STUB_MODE:
                continue
            if body.chain not in self.chains:
                known = ", ".join(sorted(self.chains)) or "(none defined)"
                raise ValueError(
                    f"roles.{name}.chain names '{body.chain}', which is not in "
                    f"chains — known chains: {known}"
                )
            if not self.chains[body.chain]:
                raise ValueError(
                    f"roles.{name}.chain names '{body.chain}', which is empty "
                    "— a chain needs at least a primary"
                )
        return self

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
from tesseract.config.loader import (
    DEFAULT_COMPACT_RATIO,
    DEFAULT_HEADROOM_MULTIPLIER,
    DEFAULT_PROMPT_CHAR_BUDGET,
    ROLE_MODES,
    ROLE_MODE_INACTIVE,
)

# The loader keeps inactive roles as unresolved stubs (`_build_role` skips
# `chain_refs` entirely), so a stale chain or ref on one never blocks boot.
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


class Boundary(BaseModel):
    """What bounds a conversation that keeps deciding to carry the work on.

    Top level beside `compaction:` and for the same reason: it describes the
    mechanism rather than who is using it. Compaction is what happens when a
    surface cannot clear a conversation at all; this is what stops the work
    going round inside one.

    There is deliberately no bound on HOW MANY times a conversation may carry
    on. There was one and it was the only refusal that fired without evidence,
    stopping real multi-phase work for arriving at a count while two steps
    alternating forever sailed past every other check.
    """

    model_config = ConfigDict(extra="forbid")

    #: How much repetition is still work rather than a loop. Read twice: the
    #: same next action at this many consecutive boundaries, and this many
    #: boundaries reporting nothing the conversation had not already reported.
    repeat_limit: int = Field(gt=0)
    #: How far back a cycle counts as a cycle. Must exceed `repeat_limit`,
    #: because the boundaries BEFORE the window are what "already reported" is
    #: judged against; at or below it there is no history to judge with and the
    #: check can never fire.
    cycle_window: int = Field(gt=0)
    #: The same tool failing this many times in a row, which is the one
    #: pathological pattern promoted from a hint to a hard boundary.
    tool_failure_limit: int = Field(gt=0)

    @model_validator(mode="after")
    def _window_has_history_behind_it(self) -> "Boundary":
        if self.cycle_window <= self.repeat_limit:
            raise ValueError(
                f"cycle_window ({self.cycle_window}) must exceed repeat_limit "
                f"({self.repeat_limit}), or the cycle check has nothing behind "
                f"its window to judge 'already reported' against and can never "
                f"fire"
            )
        return self


class Compaction(BaseModel):
    """How compaction bounds a conversation, for every role at once.

    Top level rather than per role because these describe the mechanism and
    not who is using it: a fold always leaves the head anchor and the verbatim
    tail behind, whichever model is answering.
    """

    model_config = ConfigDict(extra="forbid")

    #: What share of the context window a conversation may fill before a fold
    #: runs, and the ONE thing that decides it. Named `compact_ratio` because
    #: it is the setting and not a fallback for one; it was `default_ratio`
    #: while a second trigger derived from `prompt_char_budget` quietly
    #: outranked it on every real configuration.
    compact_ratio: float = Field(default=DEFAULT_COMPACT_RATIO, gt=0.0, lt=1.0)
    #: How far clear of that unfoldable floor the trigger must sit. At or below
    #: it, a fold cannot get under the line it just crossed and fires again on
    #: the next turn, and every turn after, spending a summarisation call each
    #: time. Must exceed 1.0 or it is not headroom.
    headroom_multiplier: float = Field(default=DEFAULT_HEADROOM_MULTIPLIER, gt=1.0)
    #: What the operator is advised to leave, as a multiple of the floor.
    #: Advice rather than a limit: nothing refuses a setting below it.
    comfortable_multiplier: float = Field(default=2.0, gt=1.0)
    #: The hard ceiling on one assembled prompt, in characters. Characters and
    #: not tokens because one chain member rejects on characters. An EMERGENCY
    #: guard: `compact_ratio` is what bounds a conversation, and this is what
    #: stops a single turn that outgrew it from failing every model in the
    #: chain. Boot refuses a `compact_ratio` this cannot carry rather than
    #: silently folding earlier than the operator asked for.
    prompt_char_budget: int = Field(default=DEFAULT_PROMPT_CHAR_BUDGET, gt=0)


class RolesConfig(BaseModel):
    """Top-level shape of ``roles.yaml``."""

    model_config = ConfigDict(extra="forbid")

    embeddings: Embeddings
    reranker: Reranker | None = None
    # Optional here and required by the LOADER. This gate validates proposed
    # edits, which are routinely partial documents, and a gate that refuses
    # what the runtime accepts is its own defect. `load_boundary_bounds` raises
    # loudly on a missing key at boot; what this adds is that a boundary block
    # which IS present cannot be written in a shape that would never fire.
    boundary: Boundary | None = None
    compaction: Compaction = Field(default_factory=Compaction)
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

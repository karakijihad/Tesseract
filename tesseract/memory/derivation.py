"""The rule that a record cannot be a summary of a summary of a summary.

One module because the three halves of the rule have to agree: what a record
declares it was written from, how deep that makes it, and what the depth costs
it when retrieval ranks. Split across the writer and the retriever they drift,
and the drift is invisible — every stage still reports success while the store
fills with restatement.

The invariants, held together rather than one at a time:

1. **Depth is computed, never declared.** A caller hands over `derived_from`
   and this module reads the sources' own depths. A writer cannot call itself
   shallow.
2. **The cap refuses at write time.** Not a nightly sweep: a record past the
   cap is never written, so nothing downstream has to know about it.
3. **A derived record is never the sole seed of a derivation.** Depth alone
   permits a two-link chain of pure paraphrase; this forbids that shape at any
   depth. Every derivation touches something nobody here wrote.
4. **A locator outside the store is raw; a record id that does not read is
   not.** A vault file or a daily note is material this runtime did not
   write, so it ends a chain at depth 0. A `mem_` id that fails to resolve is
   a deleted source or a typo, and treating it as raw would let one bogus id
   satisfy invariant 3 and reset a chain's depth. The store makes that
   distinction because only the store can resolve a locator; this module
   refuses on it.
5. **Refusing says why.** The chain that produced the refusal is in the
   message, because the next question is always "which summary of which".
"""

from __future__ import annotations

from dataclasses import dataclass

import yaml

from tesseract.paths import config_dir
from tesseract.memory.types import MemoryFrontmatter, SourceTier

_CONFIG_FILE = "memory.yaml"
_SECTION = "derivation"


@dataclass(frozen=True)
class DerivationConfig:
    max_depth: int
    tier_weights: dict[SourceTier, float]


@dataclass(frozen=True)
class DerivationRefusal:
    """Why a write was refused, in the words the log and the caller both use."""

    reason: str
    chain: tuple[str, ...]

    def message(self) -> str:
        if not self.chain:
            return self.reason
        return f"{self.reason} (chain: {' <- '.join(self.chain)})"


def _require(d: dict, key: str, where: str):
    if key not in d:
        raise RuntimeError(f"missing required key '{key}' in {where}")
    return d[key]


def load_derivation_config() -> DerivationConfig:
    """Read ``memory.yaml::derivation``; raise loudly on missing keys.

    Same shape as ``brain.auto_recall.load_auto_recall_config`` — no
    ``.get(..., default)`` for an infrastructure value, and the path resolves
    at call time so a config reload is seen.
    """
    raw = yaml.safe_load((config_dir() / _CONFIG_FILE).read_text(encoding="utf-8"))
    section = _require(raw, _SECTION, _CONFIG_FILE)
    weights_raw = _require(section, "tier_weights", f"{_CONFIG_FILE} {_SECTION}")
    weights: dict[SourceTier, float] = {}
    for tier in SourceTier:
        weights[tier] = float(
            _require(weights_raw, tier.value, f"{_CONFIG_FILE} {_SECTION}.tier_weights")
        )
    return DerivationConfig(
        max_depth=int(_require(section, "max_depth", f"{_CONFIG_FILE} {_SECTION}")),
        tier_weights=weights,
    )


def tier_weight(tier: SourceTier | None, config: DerivationConfig) -> float:
    """What a hit's tier multiplies its score by. Unknown ranks lowest."""
    if tier is None:
        return config.tier_weights[SourceTier.DERIVED]
    return config.tier_weights[tier]


def resolve(
    frontmatter: MemoryFrontmatter,
    sources: dict[str, MemoryFrontmatter | None],
    config: DerivationConfig,
    *,
    unresolved: set[str] | None = None,
) -> tuple[MemoryFrontmatter, DerivationRefusal | None]:
    """Stamp the computed depth, or refuse and say which chain did it.

    ``sources`` maps each locator in ``derived_from`` to the record it names,
    or to ``None`` when the locator does not name a record in the store.
    ``unresolved`` is the subset of those that LOOKED like a record and did
    not read: a deleted source, a typo, a race with a delete. The store makes
    that distinction because only the store can; this module refuses to treat
    the second kind as raw material, which is the whole reason it is passed
    separately rather than inferred from ``None``.
    """
    if frontmatter.source_tier is not SourceTier.DERIVED:
        # Raw material and transcripts have no chain. Depth 0 is stamped
        # rather than assumed so a record that changes tier cannot keep a
        # depth it is no longer entitled to.
        return frontmatter.model_copy(update={"derivation_depth": 0}), None

    locators = list(frontmatter.derived_from)
    if not locators:
        return frontmatter, DerivationRefusal(
            reason=(
                "a derived record must say what it was written from: "
                "derived_from is empty"
            ),
            chain=(),
        )

    dangling = sorted(unresolved or set())
    if dangling:
        # Fail closed. One id that no longer reads would otherwise satisfy
        # invariant 3 on its own and reset the chain's depth to zero, so a
        # deleted source becomes a laundering route for an unbounded chain of
        # paraphrase. The remedy is to cite what is actually there.
        return frontmatter, DerivationRefusal(
            reason=(
                "a derived record names a memory that does not exist, so what "
                "it was written from cannot be checked. Cite the records that "
                "are still there"
            ),
            chain=tuple(dangling),
        )

    # Invariant 3. A locator resolving to nothing here is material outside the
    # store — a vault file, a daily note — which is exactly the raw end of a
    # chain, so it satisfies the rule. A dangling record id never reaches
    # this point.
    if all(
        sources.get(loc) is not None
        and sources[loc].source_tier is SourceTier.DERIVED
        for loc in locators
    ):
        return frontmatter, DerivationRefusal(
            reason=(
                "a derived record cannot be written from derived records "
                "alone: every summary has to read something nobody here wrote"
            ),
            chain=tuple(locators),
        )

    depths = []
    for loc in locators:
        src = sources.get(loc)
        # Invariant 4. `None` at this point is material outside the store, so
        # depth 0 is the right answer and the chain ends there.
        depths.append(0 if src is None else src.derivation_depth)
    depth = 1 + max(depths)

    if depth > config.max_depth:
        return frontmatter, DerivationRefusal(
            reason=(
                f"this would be {depth} steps from raw material and the limit "
                f"is {config.max_depth}"
            ),
            chain=tuple(
                f"{loc} (depth {d})" for loc, d in zip(locators, depths)
            ),
        )

    return frontmatter.model_copy(update={"derivation_depth": depth}), None

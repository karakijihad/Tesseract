"""Operating-rules loader + trio-relay prompt block for TARS's system prompt.

Split out of `tesseract/brain/prompt.py` (module-size cleanup, Task 7.5).

CR-2 (2026-05-22): the 15 inline English rule constants that used to live in
`prompt.py` moved to `tesseract/brain/rules/NN-name.md` (numbered prefix
preserves assembly order). `assemble_system_prompt` reads the directory at
call time via `_load_rules`. The legacy constants stay accessible via module
`__getattr__` on `tesseract.brain.prompt` (bound to `_legacy_rule_attr`
below) so existing tests (`test_mirror_f3.py`, `test_audit_subagents.py`)
keep importing the same names.

Why move: rules drift independent of code logic — they want to be
editable as content, reviewed as content, diffed as content. Inline
Python literals turned every wording tweak into a code commit and
hid them from operator-side review. See
`Docs/Plan/context-recall/phase-CR-2-prompt-consolidation.md`.

The ``# Trio`` block (trio W2) rides alongside the rules here rather than
its own module — both are config/disk-driven content blocks injected
right after the rules card in `assemble_system_prompt`, and the block
itself is ~30 lines; a dedicated file would be mostly boilerplate.
"""

from __future__ import annotations

import logging
from pathlib import Path

# Logger name pinned to "tesseract.brain.prompt" — see prompt_time.py's
# module docstring for why this is hardcoded rather than `__name__`.
logger = logging.getLogger("tesseract.brain.prompt")

RULES_DIR = Path(__file__).resolve().parent / "rules"

# Back-compat aliases — old `_ALIVE_NUDGE_TEXT` etc. constants are
# resolved lazily from disk via `tesseract.brain.prompt.__getattr__`
# (bound to `_legacy_rule_attr` below). The keys map to filenames in
# RULES_DIR. New code should call `_load_rules` instead; these stay only
# so existing tests don't break on the move.
_RULE_NAME_TO_FILE: dict[str, str] = {
    "_ALIVE_NUDGE_TEXT": "01-interaction-style.md",
    "_TOOL_USE_RULE_TEXT": "02-tool-use.md",
    "_CAPABILITY_GAP_RULE_TEXT": "03-capability-gap.md",
    "_TASKS_NUDGE_TEXT": "04-tasks.md",
    "_PARALLEL_DELEGATION_NUDGE_TEXT": "05-parallel-delegation.md",
    "_WORKSPACE_THREAD_ISOLATION_TEXT": "07-workspace-thread-isolation.md",
    "_REFLECT_DIRECTIVE_TEXT": "08-reflect-directive.md",
    "_TEMPORAL_AWARENESS_DIRECTIVE_TEXT": "09-temporal-awareness.md",
    "_AUDIT_LOOP_DIRECTIVE_TEXT": "10-audit-loop.md",
    "_ERROR_RECOVERY_DIRECTIVE_TEXT": "11-error-recovery.md",
    "_VAULT_REFLEX_RULE_TEXT": "12-vault-reflex.md",
    "_STATE_NUDGE_TEXT": "13-state.md",
    "_MULTIMODAL_BODY_TEXT": "14-multimodal-body.md",
    "_OUTPUT_CONTRACT_RULE_TEXT": "16-output-contract.md",
    "_SOURCE_OF_TRUTH_TEXT": "15-source-of-truth.md",
}


def _load_rules(rules_dir: Path) -> list[str]:
    """Read every ``*.md`` file in ``rules_dir`` in name-sorted order.

    Returns one blob per file (stripped of leading/trailing whitespace),
    skipping unreadable ones with a warning. Missing directory returns
    an empty list — assembly degrades to "no rules block" rather than
    raising.
    """
    if not rules_dir.exists():
        logger.warning("rules dir missing: %s — assembling without rules block", rules_dir)
        return []
    blobs: list[str] = []
    for path in sorted(rules_dir.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            logger.warning("rules file unreadable: %s (%s)", path, exc)
            continue
        if text:
            blobs.append(text)
    return blobs


def _build_trio_block() -> str:
    """Render the ``# Trio`` block from ``cockpit.yaml`` — lane names/kinds
    (roles are pillars; never hardcoded in rules text) + relay tunables the
    trio-verification card references. Config is authoritative (M8): a
    missing/malformed config does NOT silently drop the block — it renders a
    visible ``relay disabled`` marker and logs at error level, so the model and
    operator see the broken config instead of a vanished instruction. Assembly
    still never dies on this block."""
    try:
        from tesseract.config.cockpit import load_trio_lanes, load_trio_relay

        lanes = load_trio_lanes()
        relay = load_trio_relay()
    except Exception as exc:  # noqa: BLE001 — surface, don't kill assembly
        logger.error("trio config unavailable (%s) — relay disabled", exc)
        return (
            "# Trio\n\n"
            f"**Trio relay unavailable — cockpit.yaml is missing or invalid "
            f"({exc}). The coder↔auditor relay is disabled until it is fixed.**"
        )
    lane_lines = "\n".join(
        f"- {lane['role']} lane: `{lane['name']}` (kind={lane['kind']})"
        for lane in lanes
    )
    return (
        "# Trio\n\n"
        f"{lane_lines}\n"
        f"- relay round cap: {relay['max_rounds']}\n"
        f"- verify-by-default: {'on' if relay['verify_by_default'] else 'off'}"
    )


def _legacy_rule_attr(name: str) -> str:
    """Lazy back-compat for the legacy ``_*_TEXT`` constants. Loaded
    from ``RULES_DIR`` on demand; callers should prefer ``_load_rules``.

    Bound as ``tesseract.brain.prompt.__getattr__`` (PEP 562) so
    ``from tesseract.brain.prompt import _ALIVE_NUDGE_TEXT`` and
    ``hasattr(prompt, "_X_TEXT")`` keep working post-split.

    Raises ``AttributeError`` when the rule file is missing — so
    ``hasattr(prompt, "_X_TEXT")`` only returns True when the rule
    actually exists on disk. Returning an empty string would give a
    false-positive on probes (caller thinks the constant exists when
    it doesn't).
    """
    if name in _RULE_NAME_TO_FILE:
        path = RULES_DIR / _RULE_NAME_TO_FILE[name]
        if not path.exists():
            raise AttributeError(
                f"module 'tesseract.brain.prompt' has no attribute {name!r} "
                f"(rule file missing: {path})"
            )
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise AttributeError(
                f"module 'tesseract.brain.prompt' attribute {name!r} unreadable "
                f"({path}: {exc})"
            ) from exc
    raise AttributeError(f"module 'tesseract.brain.prompt' has no attribute {name!r}")

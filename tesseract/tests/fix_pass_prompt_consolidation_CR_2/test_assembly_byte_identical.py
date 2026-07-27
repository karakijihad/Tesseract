"""CR-2: assembly is content-preserving.

After migrating the 13 Python rule constants into ``brain/rules/*.md``,
the assembled system prompt must contain the EXACT same rule text it
did before. Migration is content-preserving — bullet-by-bullet — to
avoid silent behavior drift.
"""

from __future__ import annotations

from tesseract.brain import prompt as prompt_module


# Verbatim snippets that must survive the migration. Each is a
# distinctive phrase from one of the 15 rules that no other rule
# contains, so a per-rule regression shows up as a missing match.
_RULE_SIGNATURES = [
    "Vary acknowledgment phrasing",                    # alive-nudge
    "Form a hypothesis first",                         # tool-use
    "STOP. The default response when the gap",         # capability-gap
    "chat-embedded checklist",                         # tasks-nudge
    "default to BACKGROUND",                           # parallel-delegation
    "Concrete miss (2026-05-18, daily brief thread",   # workspace-isolation
    "persist it yourself immediately",                 # reflect-directive (session_reflect retired with the REPL, 2026-07-13)
    "Render them as natural prose",                    # temporal-awareness
    "audit-N.md as input",                             # audit-loop
    "[chat_brain error]",                              # error-recovery
    "vault is the authoritative",                      # vault-reflex
    "`happy` after a real",                            # state-nudge
    "Auto-transcribed by local Whisper",               # multimodal-body
    "authoritative for what is *actually wired*",      # source-of-truth
    "Every text emission MUST be wrapped",             # output-contract
]


def test_every_rule_signature_present_in_assembled_prompt() -> None:
    rendered = prompt_module.assemble_system_prompt(mode="manifest")
    for sig in _RULE_SIGNATURES:
        assert sig in rendered, (
            f"rule content drift — signature missing after CR-2 migration: "
            f"{sig!r}"
        )


def test_rules_appear_in_canonical_order() -> None:
    """Numbered prefix on rule files (01- → 15-) preserves the original
    appendance order. A reshuffle that breaks the cache or the
    cognitive flow shows up here."""
    rendered = prompt_module.assemble_system_prompt(mode="manifest")
    positions = []
    for sig in _RULE_SIGNATURES:
        idx = rendered.find(sig)
        assert idx >= 0, f"missing: {sig!r}"
        positions.append(idx)
    # Strictly increasing == canonical order preserved.
    assert positions == sorted(positions), (
        f"rule order drifted: positions = {positions}"
    )

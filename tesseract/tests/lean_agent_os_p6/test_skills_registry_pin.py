"""Skill-tool surface pin.

Skill folders (SKILL.md + optional bundled `scripts/`) are prose + plain
files, never an execution surface — bundled scripts run through the EXISTING
bash/subprocess ASK path.

P6 Task 4b originally pinned "zero skill tools registered". Capability-growth
Phase 4 (2026-07-18) intentionally adds the governed skill LIFECYCLE tools
`skill_create` (quarantine-write, operator-promote-gated), `skill_promote`
(operator_gate), and `skill_refine` (files a refinement card; never applies).
This pin now asserts those are the ONLY skill/workshop tools —
a static assertion against the LIVE registry so any OTHER skill/workshop tool
(especially a skill-EXECUTION surface) can't hot-register silently.
"""

from __future__ import annotations

from tesseract.brain.boot import build_tool_registry

# Phase 4 governed lifecycle tools — all gated (draft lands in quarantine or a
# proposal card; activation always needs operator approval). None EXECUTES a
# skill — skills stay prose.
_ALLOWED_SKILL_TOOLS = {"skill_create", "skill_promote", "skill_refine"}


def test_only_governed_skill_lifecycle_tools_registered() -> None:
    registry, _mood, _voice, _bundle, _alarms = build_tool_registry(policy=None)
    names = registry.names()
    matched = {n for n in names if "skill" in n.lower() or "workshop" in n.lower()}
    offenders = matched - _ALLOWED_SKILL_TOOLS
    assert not offenders, (
        f"an unexpected skill/workshop tool is registered — only the governed "
        f"lifecycle tools {sorted(_ALLOWED_SKILL_TOOLS)} are allowed (no skill "
        f"EXECUTION surface): {sorted(offenders)}"
    )

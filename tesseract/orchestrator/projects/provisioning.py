"""Make a registered directory hub-reachable: trust it, then wire the CLIs.

Registering a project marks it trusted rather than adding a second prompt —
the registry is a superset of ``trusted_dirs.json``, and an operator who just
told TESSERACT "this is the project" has answered the trust question.

**Why one ``terminal`` provision and not three.** Both CLIs' MCP configs are
global now (``~/.claude.json``, ``~/.codex/config.toml``), so a directory does
not get its own hub entry to write. ``provision(kind="terminal")`` is the call
that wires *both* CLIs for a hand-launched shell, and its identity precedence
leaves any live lane identity alone. Provisioning the ``claude``/``codex`` lane
kinds here instead would claim that one global entry for a lane that does not
exist. ``cleanup_dirs=[root]`` is the part that is still per-project: it reaps
a stale project-scope ``.mcp.json`` left by the old scheme, which would
otherwise shadow the user-scope entry for anything run in that tree.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

log = logging.getLogger(__name__)


async def trust_and_provision(root: Path) -> str:
    """Mark ``root`` trusted and wire the CLIs to the hub from it.

    Returns a one-line status for the caller to narrate. Provisioning failure
    is reported, never raised: a project is still worth registering on a
    machine whose hub token is not exported, and the operator needs to be told
    which half did not happen rather than losing the registration to it.
    """
    try:
        from tesseract.orchestrator.agent_controller.trust import mark_trusted

        # Inside the guard, not before it: `mark_trusted` writes a file and
        # re-raises on failure, so leaving it outside made the "never raises"
        # contract false for the half most likely to hit a disk error.
        await asyncio.to_thread(mark_trusted, root)
        trust_note = "trusted"
    except Exception as exc:  # noqa: BLE001 — reported, never fatal
        log.warning("project trust grant failed for %s", root, exc_info=True)
        # Reported, not fatal, and it does not suppress the other half: the two
        # write different files for different reasons and neither depends on
        # the other succeeding.
        trust_note = f"NOT trusted ({exc})"

    try:
        from tesseract.config.mcp import load_mcp_config
        from tesseract.orchestrator.agent_controller.lanes import mcp_provision

        ok = await asyncio.to_thread(
            lambda: mcp_provision.provision(
                "terminal",
                load_mcp_config(),
                cleanup_dirs=[root],
            )
        )
    except Exception as exc:  # noqa: BLE001 — reported, never fatal
        log.warning("project provisioning failed for %s", root, exc_info=True)
        return f"{trust_note}; MCP provisioning failed ({exc})"
    if not ok:
        return f"{trust_note}; MCP provisioning incomplete (config busy — retried per lane turn)"
    return f"{trust_note}; MCP provisioned (claude + codex reach the hub from here)"


__all__ = ["trust_and_provision"]

"""budget.* MCP verbs (P3) — read/control the shared CostLedger directly (no
kernel tool). ``status`` (AUTO) reads the spend snapshot; ``set_cap`` /
``pause_source`` (ASK) are operator budget controls."""

from __future__ import annotations

from typing import Any

from tesseract.mirror.server.mcp.verbs._base import MCPVerbError, VerbContext


def _ledger(ctx: VerbContext):
    ledger = ctx.app.get("cost_ledger")
    if ledger is None:
        raise MCPVerbError(503, "cost ledger not ready")
    return ledger


async def budget_status(ctx: VerbContext) -> dict[str, Any]:
    ledger = _ledger(ctx)
    summary = ledger.budget_summary()
    role = ctx.params.get("role")
    if role:
        state = ledger.budget_state(str(role))
        summary["role"] = {
            "role": role,
            "spent_usd": state.role_spent_usd,
            "cap_usd": state.role_cap_usd,
            "blocked": state.blocked,
        }
    return summary


async def budget_set_cap(ctx: VerbContext) -> str:
    ledger = _ledger(ctx)
    role = str(ctx.params.get("role") or "").strip()
    if not role:
        raise MCPVerbError(400, "budget.set_cap requires 'role'")
    if "cap_usd" not in ctx.params:
        raise MCPVerbError(400, "budget.set_cap requires 'cap_usd'")
    try:
        cap = float(ctx.params["cap_usd"])
    except (TypeError, ValueError):
        raise MCPVerbError(400, "budget.set_cap 'cap_usd' must be a number")
    try:
        ledger.set_role_cap(role, cap)
    except ValueError as exc:
        raise MCPVerbError(400, str(exc))
    return f"cap for role '{role}' set to ${cap:.4f} (runtime override; reload reverts to roles.yaml)"


async def budget_pause_source(ctx: VerbContext) -> str:
    ledger = _ledger(ctx)
    source = str(ctx.params.get("source") or "").strip()
    if not source:
        raise MCPVerbError(400, "budget.pause_source requires 'source' (a role name or 'global')")
    ledger.pause_source(source)
    return f"spend source '{source}' paused (resume or restart to lift)"


__all__ = ["budget_status", "budget_set_cap", "budget_pause_source"]

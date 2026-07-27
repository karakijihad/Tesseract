"""Owner batch 1 follow-up — `_async_broadcast_cost` must not access
`state.role` (BudgetState has no such field).

Reading `state.role` on BudgetState raises AttributeError, which
propagates inside the fire-and-forget `loop.create_task(...)` task —
the cost_delta envelope is never sent. The HUD chips silently freeze at
the last persisted localStorage value while the cost ledger keeps
appending events to `cost-tracking.jsonl`. Owner caught this 2026-04-29
when the chat chip read $0.12 while the ledger had advanced to $0.28.

The role identifier lives on `event` (CostEvent.role), not on `state`.
"""

from __future__ import annotations

import dataclasses

from tesseract.brain.cost.ledger import BudgetState


def test_budget_state_has_no_role_field():
    fields = {f.name for f in dataclasses.fields(BudgetState)}
    assert "role" not in fields, (
        "BudgetState must NOT carry a `role` field — `_async_broadcast_cost` in "
        "mirror/server/app.py reads the role identifier from CostEvent. If you "
        "add `role` here, also fix that call site (the comment block there "
        "explains the regression history)."
    )
    assert "role_spent_usd" in fields
    assert "role_cap_usd" in fields


def test_async_broadcast_cost_role_check_no_state_role_access():
    """Stricter regex check on the actual reference."""
    import inspect
    import re

    from tesseract.mirror.server import app as app_mod

    src = inspect.getsource(app_mod._async_broadcast_cost)
    # Match `state.role` but NOT `state.role_spent_usd` / `state.role_cap_usd`.
    bad = re.findall(r"\bstate\.role\b(?!_)", src)
    assert not bad, f"_async_broadcast_cost still accesses bare `state.role`: {bad}"

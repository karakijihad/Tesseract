"""Strict parsing of operator approval decisions.

A permission decision at the operator boundary must be an explicit JSON
boolean. Accepting truthy non-booleans (the string ``"false"``, ``1``, ``[]``)
would let a malformed, stale, or direct local client invert a denial into an
approval — a max-security violation (audit C1). Every decision surface (parked
REST, ASK-over-MCP REST, live WebSocket ``tool_response``) routes its
``approved`` value through :func:`parse_approved`.
"""

from __future__ import annotations


class ApprovalDecisionError(ValueError):
    """The approval payload is missing ``approved`` or it is not a JSON bool."""


def parse_approved(body: object) -> bool:
    """Return the strict boolean ``approved`` from a decision payload.

    Raises :class:`ApprovalDecisionError` if ``body`` is not a mapping, lacks
    ``approved``, or carries a non-boolean value. ``bool`` is a subclass of
    ``int`` so ``isinstance(value, bool)`` accepts only ``True``/``False`` and
    rejects ``0``/``1`` and every other truthy type.
    """
    if not isinstance(body, dict) or "approved" not in body:
        raise ApprovalDecisionError("body requires 'approved' (bool)")
    value = body["approved"]
    if not isinstance(value, bool):
        raise ApprovalDecisionError("'approved' must be a JSON boolean")
    return value

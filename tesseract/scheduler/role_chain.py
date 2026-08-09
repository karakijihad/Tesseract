"""Shared adapter-chain builder for LLM-using scheduler jobs.

Resolves the role to use (operator override on `JobContext.model_role`,
falling back to the handler's `default_model_role`), reads the
primary+fallbacks from `roles.yaml`, and returns the buildable
`(adapter, options)` pairs. Skips refs we can't construct (missing API
key, etc.) so a partially-configured catalog still runs the job through
whatever's available — same posture the per-task helpers had before this
got lifted.
"""

from __future__ import annotations

import logging
from typing import Sequence

from tesseract.brain.boot import build_adapter, load_bundle
from tesseract.brain.cost.ledger import CostLedger
from tesseract.brain.cost.metered_adapter import meter_chain
from tesseract.kernel.adapters.base import AdapterOptions, ModelAdapter
from tesseract.scheduler.types import JobContext

log = logging.getLogger(__name__)

AdapterChain = list[tuple[ModelAdapter, AdapterOptions]]


def resolve_role_name(ctx: JobContext, default: str | None) -> str | None:
    """Return the role name a handler should use this run.

    Operator override on the job context wins; otherwise the handler's
    declared default. Returns `None` only when the handler has no default
    and no override — that's a misconfiguration the caller should treat
    as "skip the LLM call".
    """
    override = (ctx.model_role or "").strip()
    if override:
        return override
    return default or None


def build_chain_for_role(
    role_name: str,
    *,
    log_label: str = "scheduler",
    cost_ledger: CostLedger | None = None,
) -> AdapterChain:
    """Build the (adapter, options) chain for `role_name`.

    Empty list when the role is missing from `roles.yaml` or every
    catalog ref fails to build. Per-ref failures are logged at INFO so a
    missing API key on a fallback entry doesn't spam ERROR.

    When `cost_ledger` is provided, each entry is wrapped in a
    ``MeteredAdapter`` so the background ``generate()`` spend hits the ledger
    and paid calls preflight against the daily cap — parity with the chat path
    (2026-06-28 cost-ledger gap). Omitting it (tests/back-compat) yields bare
    adapters.
    """
    try:
        bundle = load_bundle()
    except Exception:
        log.exception("%s: load_bundle failed", log_label)
        return []
    if role_name not in bundle.roles:
        log.warning("%s: role %r missing from roles.yaml", log_label, role_name)
        return []
    role = bundle.role(role_name)
    chain: AdapterChain = []
    failures: list[str] = []
    for ref in (role.primary, *role.fallbacks):
        if ref is None:
            # `RoleConfig.primary` is None for `mode: inactive` and the
            # dataclass makes gating the consumer's job. Without this the
            # except below dereferences `ref.ref` on None and the
            # AttributeError escapes uncaught.
            failures.append("role is inactive (no primary wired)")
            continue
        try:
            adapter = build_adapter(ref)
        except Exception as exc:  # noqa: BLE001
            log.info("%s: cannot build %s — %s", log_label, ref.ref, exc)
            failures.append(f"{ref.ref}: {exc}")
            continue
        opts = AdapterOptions(
            provider=ref.connection.name,
            model=ref.model.model,
            role=role_name,  # billing key — the ledger records per role
            tier=ref.connection.tier,
            context_window=ref.model.fields.get("context_window"),
        )
        chain.append((adapter, opts))
    if not chain:
        # Per-ref misses stay at INFO — one dead fallback is normal. A chain
        # with nothing left is not: the job silently does no work, so the
        # reason each ref failed (a disabled switch names itself) has to be
        # readable without turning on INFO logging first.
        log.warning(
            "%s: no usable provider — %s",
            log_label,
            "; ".join(failures) or "role has no primary or fallbacks",
        )
    return meter_chain(chain, cost_ledger)


def build_chain_for_job(
    ctx: JobContext,
    *,
    default_role: str | None,
    log_label: str = "scheduler",
) -> AdapterChain:
    """Convenience: resolve the role and build the chain in one call.

    Returns an empty list when no role is resolvable — handlers should
    treat that as "skip this run", not raise.
    """
    role_name = resolve_role_name(ctx, default_role)
    if role_name is None:
        log.warning("%s: no role resolved (no override, no default)", log_label)
        return []
    return build_chain_for_role(
        role_name,
        log_label=f"{log_label} role={role_name}",
        cost_ledger=ctx.cost_ledger,
    )


__all__: Sequence[str] = (
    "AdapterChain",
    "build_chain_for_job",
    "build_chain_for_role",
    "resolve_role_name",
)

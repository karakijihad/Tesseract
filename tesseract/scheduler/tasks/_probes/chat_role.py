"""Chat probe — known-good prompt → non-empty assistant text.

Drives one ``adapter.generate`` call against the CATALOG REF it was handed,
built on its own rather than taken off the front of a role's chain. A fallback
asked through its role gets the primary, which is why three refs on this
machine had no health history until the row asked them directly.
``ok=True`` requires:

  * The call returns within the connection's ``timeout_seconds`` — read off
    the options the entry builder filled in, never a constant here.
  * The response is a non-empty string.

Failure modes map to ``DriftKind``:

  * Timeout → ``latency_spike``
  * Exception during generate → ``http_error``
  * Empty / whitespace-only response → ``empty_output``

Every failure is also ATTRIBUTED, through
:mod:`tesseract.orchestrator.provider_failure`: ``ours`` when the call never
reached the provider (the ref would not build, no timeout in config) and
``theirs`` when it did, with their own words where they gave any. That is what
makes this probe the same instrument for a provider the operator wires
tomorrow as for the ones that ship — the row a reader finds in
``runtime/logs/provider-health/`` has the same three fields whichever tier
wrote it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, ClassVar

from tesseract.brain.cost.ledger import BudgetExhausted
from tesseract.kernel.adapters.base import call_timeout
from tesseract.orchestrator import provider_failure
from tesseract.orchestrator.provider_failure import ProviderFault
from tesseract.scheduler.role_chain import build_entry_for_ref
from tesseract.scheduler.tasks._probes.base import ProbeResult

if TYPE_CHECKING:
    from tesseract.brain.cost.ledger import CostLedger

log = logging.getLogger(__name__)

_KNOWN_GOOD_PROMPT = (
    "Reply with the single word `pong` and no punctuation, no preamble."
)


class ChatRoleProbe:
    role_kind: ClassVar[str] = "chat"

    def __init__(
        self,
        *,
        entry_builder: Any = None,
        cost_ledger: "CostLedger | None" = None,
    ) -> None:
        # Injection seam: tests provide a fake that returns `(adapter, options)`
        # tuples without touching providers.yaml.
        self._entry_builder = entry_builder or build_entry_for_ref
        # When set, the probe's `generate()` call is metered so its spend lands
        # in cost-tracking.jsonl instead of vanishing (parity with the chat
        # path). None yields bare adapters (tests / back-compat).
        self._cost_ledger = cost_ledger

    async def probe(self, role_name: str, ref: str) -> ProbeResult:
        return await _run_chat_probe(
            self._entry_builder, role_name, ref, cost_ledger=self._cost_ledger,
        )


async def _run_chat_probe(
    entry_builder: Any,
    role_name: str,
    ref: str,
    *,
    cost_ledger: "CostLedger | None" = None,
) -> ProbeResult:
    t0 = time.monotonic()
    now = datetime.now(timezone.utc).isoformat()
    try:
        # `role_name` is the billing key, not the thing being asked: several
        # roles can name one ref, and the ledger needs one of their names.
        chain = entry_builder(
            ref,
            billing_key=role_name,
            log_label="provider_probe",
            cost_ledger=cost_ledger,
        ) or []
    except Exception as exc:  # noqa: BLE001
        return ProbeResult(
            role=role_name,
            ref=ref,
            ok=False,
            drift_kind="unavailable",
            evidence=provider_failure.evidence(
                provider_failure.ours(f"the adapter would not build ({exc})")
            ),
            probed_at=now,
            latency_ms=(time.monotonic() - t0) * 1000.0,
        )
    if not chain:
        return ProbeResult(
            role=role_name,
            ref=ref,
            ok=False,
            drift_kind="unavailable",
            evidence=provider_failure.evidence(
                provider_failure.ours("no adapter resolved for this ref")
            ),
            probed_at=now,
            latency_ms=(time.monotonic() - t0) * 1000.0,
        )
    adapter, options = chain[0]
    try:
        timeout = call_timeout(options)
    except KeyError as exc:
        return ProbeResult(
            role=role_name,
            ref=ref,
            ok=False,
            drift_kind="unavailable",
            evidence=provider_failure.evidence(
                provider_failure.ours(f"the catalog entry sets no timeout ({exc})")
            ),
            probed_at=now,
            latency_ms=(time.monotonic() - t0) * 1000.0,
        )
    try:
        text = await asyncio.wait_for(
            adapter.generate(_KNOWN_GOOD_PROMPT, options),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        return ProbeResult(
            role=role_name,
            ref=ref,
            ok=False,
            drift_kind="latency_spike",
            evidence=provider_failure.evidence(
                provider_failure.unanswered(timeout), timeout_seconds=timeout
            ),
            probed_at=now,
            latency_ms=(time.monotonic() - t0) * 1000.0,
        )
    except BudgetExhausted as exc:
        # Self-imposed daily cap, not provider drift. Since the probe is now
        # metered (cost-ledger threading), MeteredAdapter._preflight raises
        # this when the role/global budget is spent. Reporting it as a fault
        # would publish a false provider_health drift to the mapper — the
        # provider is fine, we just chose not to spend. Record it as healthy
        # (ok ⟺ drift_kind=="none" per the ProbeResult contract) with evidence
        # noting the cap so the JSONL still shows the skip.
        return ProbeResult(
            role=role_name,
            ref=ref,
            ok=True,
            drift_kind="none",
            evidence={"skipped": "budget_exhausted", "detail": str(exc)},
            probed_at=now,
            latency_ms=(time.monotonic() - t0) * 1000.0,
        )
    except Exception as exc:  # noqa: BLE001
        return ProbeResult(
            role=role_name,
            ref=ref,
            ok=False,
            drift_kind="http_error",
            evidence=provider_failure.evidence(provider_failure.from_exception(exc)),
            probed_at=now,
            latency_ms=(time.monotonic() - t0) * 1000.0,
        )
    latency_ms = (time.monotonic() - t0) * 1000.0
    if not isinstance(text, str) or not text.strip():
        return ProbeResult(
            role=role_name,
            ref=ref,
            ok=False,
            drift_kind="empty_output",
            evidence=provider_failure.evidence(
                ProviderFault(
                    origin="theirs", kind="empty_answer",
                    detail=f"answered with nothing ({type(text).__name__})",
                ),
                raw_type=type(text).__name__,
                len=len(text or ""),
            ),
            probed_at=now,
            latency_ms=latency_ms,
        )
    return ProbeResult(
        role=role_name,
        ref=ref,
        ok=True,
        drift_kind="none",
        evidence={"sample": text.strip()[:120], "char_count": len(text)},
        probed_at=now,
        latency_ms=latency_ms,
    )


__all__ = ["ChatRoleProbe"]

"""Chat-role probe — known-good prompt → non-empty assistant text.

Drives one ``adapter.generate`` call against the role's resolved primary.
``ok=True`` requires:

  * The call returns within the connection's ``timeout_seconds``.
  * The response is a non-empty string.

Failure modes map to ``DriftKind``:

  * Timeout → ``latency_spike``
  * Exception during generate → ``http_error``
  * Empty / whitespace-only response → ``empty_output``
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, ClassVar

from tesseract.brain.cost.ledger import BudgetExhausted
from tesseract.scheduler.role_chain import build_chain_for_role
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
        chain_builder: Any = None,
        cost_ledger: "CostLedger | None" = None,
    ) -> None:
        # Injection seam: tests provide a fake that returns `(adapter, options)`
        # tuples without touching providers.yaml.
        self._chain_builder = chain_builder or build_chain_for_role
        # When set, the probe's `generate()` call is metered so its spend lands
        # in cost-tracking.jsonl instead of vanishing (parity with the chat
        # path). None yields bare adapters (tests / back-compat).
        self._cost_ledger = cost_ledger

    async def probe(self, role_name: str, ref: str) -> ProbeResult:
        return await _run_chat_probe(
            self._chain_builder, role_name, ref, cost_ledger=self._cost_ledger,
        )


async def _run_chat_probe(
    chain_builder: Any,
    role_name: str,
    ref: str,
    *,
    cost_ledger: "CostLedger | None" = None,
) -> ProbeResult:
    t0 = time.monotonic()
    now = datetime.now(timezone.utc).isoformat()
    try:
        chain = chain_builder(
            role_name, log_label="provider_probe", cost_ledger=cost_ledger
        ) or []
    except Exception as exc:  # noqa: BLE001
        return ProbeResult(
            role=role_name,
            ref=ref,
            ok=False,
            drift_kind="unavailable",
            evidence={"exception": repr(exc)},
            probed_at=now,
            latency_ms=(time.monotonic() - t0) * 1000.0,
        )
    if not chain:
        return ProbeResult(
            role=role_name,
            ref=ref,
            ok=False,
            drift_kind="unavailable",
            evidence={"reason": "no adapter chain resolved"},
            probed_at=now,
            latency_ms=(time.monotonic() - t0) * 1000.0,
        )
    adapter, options = chain[0]
    timeout = float(getattr(options, "extra", {}).get("timeout_seconds", 30.0))
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
            evidence={"timeout_seconds": timeout},
            probed_at=now,
            latency_ms=(time.monotonic() - t0) * 1000.0,
        )
    except BudgetExhausted as exc:
        # Self-imposed daily cap, not provider drift. Since the probe is now
        # metered (cost-ledger threading), MeteredAdapter._preflight raises
        # this when the role/global budget is spent. Reporting it as a fault
        # would publish a false provider_health drift to the AU-5 mapper — the
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
            evidence={"exception": repr(exc)},
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
            evidence={"raw_type": type(text).__name__, "len": len(text or "")},
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

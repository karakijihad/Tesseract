from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class JobContext:
    job_name: str
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    fired_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    app: Any = field(default=None, compare=False, repr=False)
    config: dict[str, Any] = field(default_factory=dict)
    # Scheduler's own log directory (runs.jsonl lives here). Injected by the
    # engine so jobs that consume `runs.jsonl` (e.g. DailyWriterJob) read
    # from the engine's own log_dir rather than a hardcoded repo path.
    log_dir: Path | None = field(default=None, compare=False, repr=False)
    # Operator's per-job override of the cognitive role for this LLM call.
    # `None` means "use the handler's `default_model_role`". Only set on
    # handlers with `uses_llm = True`; ignored otherwise. Carrying it on
    # JobContext (rather than smuggling through `config`) keeps the inner
    # `config:` block strictly handler-private and makes the override
    # visible in tests via direct attribute access.
    model_role: str | None = field(default=None, compare=False)
    # Shared CostLedger singleton (``app["cost_ledger"]``), threaded so LLM-using
    # jobs bill spend and preflight the daily cap via ``MeteredAdapter`` (built
    # in ``role_chain.build_chain_for_role``). None for tests / ledger-disabled
    # boots — metering becomes a no-op. (2026-06-28 cost-ledger gap.)
    cost_ledger: Any = field(default=None, compare=False, repr=False)


@dataclass(frozen=True)
class JobResult:
    job_name: str
    run_id: str
    ok: bool
    detail: str = ""
    payload: dict = field(default_factory=dict)
    duration_ms: float = 0.0

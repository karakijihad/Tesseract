"""ChatDigestJob — nightly transcript summarizer for memory-retune M3.

Walks `tesseract/sessions/*.json` for files whose `ended_at` maps to
yesterday (UTC), filters `history` to user+assistant turns, asks the
chat_brain adapter for a 3-8 sentence digest, and appends a
`## [chat_digest] <YYYY-MM-DD>` section to
`memory-store/daily/<YYYY-MM-DD>.md`. Idempotent — the header itself is
the probe, so re-firing on the same day is a no-op.

"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path

from tesseract.paths import TESSERACT_HOME
from tesseract.kernel.adapters.base import AdapterOptions, ModelAdapter
from tesseract.memory.daily_notes import append_section, section_exists
from tesseract.mirror.server import chat_store
from tesseract.mirror.server.chat_store import ChatRecord
from tesseract.scheduler.base_job import BaseJob
from tesseract.brain.cost.metered_adapter import meter_chain
from tesseract.scheduler.role_chain import build_chain_for_role, resolve_role_name
from tesseract.scheduler.types import JobContext, JobResult

log = logging.getLogger(__name__)

DEFAULT_MAX_DIGEST_CHARS = 6000
DEFAULT_TIMEOUT_S = 60.0

_SUMMARY_PROMPT = (
    "You are summarizing one day of conversation between the operator and the assistant. "
    "Produce a 3-8 sentence digest covering: what was discussed, what was "
    "decided, what was learned, and anything explicitly deferred. Plain prose, "
    "no bullet list, no tool calls, no preamble. Output only the digest.\n\n"
    "--- TRANSCRIPT ---\n"
)


class ChatDigestJob(BaseJob):
    uses_llm = True
    default_model_role = "chat_brain"

    async def run(self, ctx: JobContext) -> JobResult:
        t0 = time.monotonic()
        try:
            target_date = (ctx.fired_at - timedelta(days=1)).date()
            # No directory to resolve: `chat_store` owns the records and
            # reads TESSERACT_HOME at call time, so a test scopes its writes
            # with the env var rather than with a config key nothing shipped
            # ever set.
            # Off the loop: reads + fully parses every active session file
            # (up to 10k) to find yesterday's.
            sessions = await asyncio.to_thread(_collect_sessions, target_date)
            if not sessions:
                return JobResult(
                    job_name=ctx.job_name,
                    run_id=ctx.run_id,
                    ok=True,
                    detail=f"no sessions for {target_date.isoformat()}",
                    payload={
                        "target_date": target_date.isoformat(),
                        "sessions": 0,
                        "wrote": False,
                    },
                    duration_ms=(time.monotonic() - t0) * 1000.0,
                )

            # Before anything expensive: a day already digested costs nothing.
            # The probe used to run inside `append_section`, i.e. AFTER the
            # model call, so re-firing on a written day still paid for a digest
            # and threw it away — 12.7s and `wrote=False` in AR-3's live pass,
            # multiplied once the pipeline began walking missed days.
            daily_dir = _resolve_daily_dir(ctx)
            header = f"## [chat_digest] {target_date.isoformat()}"
            if section_exists(
                probe=header,
                daily_dir=daily_dir,
                date=datetime.combine(target_date, dtime(0, 0), tzinfo=timezone.utc),
            ):
                return JobResult(
                    job_name=ctx.job_name,
                    run_id=ctx.run_id,
                    ok=True,
                    detail=f"{target_date.isoformat()} already digested",
                    payload={
                        "target_date": target_date.isoformat(),
                        "sessions": len(sessions),
                        "wrote": False,
                    },
                    duration_ms=(time.monotonic() - t0) * 1000.0,
                )

            max_chars = int(ctx.config.get("max_digest_chars", DEFAULT_MAX_DIGEST_CHARS))
            transcript = _build_transcript(sessions, max_chars, target_date)

            chain = meter_chain(_resolve_adapter_chain(ctx), ctx.cost_ledger)
            if not chain:
                return JobResult(
                    job_name=ctx.job_name,
                    run_id=ctx.run_id,
                    ok=True,
                    detail="adapter unavailable — skipped digest",
                    payload={
                        "target_date": target_date.isoformat(),
                        "sessions": len(sessions),
                        "wrote": False,
                    },
                    duration_ms=(time.monotonic() - t0) * 1000.0,
                )

            digest = await _summarize_with_fallback(transcript, chain, DEFAULT_TIMEOUT_S)
            if not digest.strip():
                # Chain was present but every member failed (or returned empty)
                # — surface as ok=False so the scheduler's retry_policy fires
                # a second attempt after its configured backoff. Distinct from
                # the chain-empty branch above, which is a structural misconfig
                # that retrying won't fix.
                return JobResult(
                    job_name=ctx.job_name,
                    run_id=ctx.run_id,
                    ok=False,
                    detail="chain exhausted — no digest produced",
                    payload={
                        "target_date": target_date.isoformat(),
                        "sessions": len(sessions),
                        "wrote": False,
                    },
                    duration_ms=(time.monotonic() - t0) * 1000.0,
                )

            # `daily_dir`/`header` were resolved before the model call, for the
            # early probe above. The write keeps its own probe as the guard
            # against a second writer between the two.
            wrote = append_section(
                header=header,
                body=digest.strip(),
                daily_dir=daily_dir,
                date=datetime.combine(target_date, dtime(0, 0), tzinfo=timezone.utc),
                idempotency_probe=header,
                pad_short=True,
            )
            return JobResult(
                job_name=ctx.job_name,
                run_id=ctx.run_id,
                ok=True,
                detail=f"sessions={len(sessions)} wrote={wrote}",
                payload={
                    "target_date": target_date.isoformat(),
                    "sessions": len(sessions),
                    "wrote": wrote,
                    "digest_chars": len(digest.strip()),
                },
                duration_ms=(time.monotonic() - t0) * 1000.0,
            )
        except Exception as exc:  # noqa: BLE001 — handler contract forbids raising
            log.exception("chat_digest crashed")
            return JobResult(
                job_name=ctx.job_name,
                run_id=ctx.run_id,
                ok=False,
                detail=f"unhandled: {exc!r}",
                duration_ms=(time.monotonic() - t0) * 1000.0,
            )


def _resolve_daily_dir(ctx: JobContext) -> Path:
    override = ctx.config.get("daily_dir")
    if override:
        return Path(override)
    app = ctx.app
    if app is not None and hasattr(app, "get"):
        tdir = app.get("tesseract_dir")
        if tdir is not None:
            return Path(tdir) / "memory-store" / "daily"
    return TESSERACT_HOME / "memory-store" / "daily"


def _resolve_adapter_chain(ctx: JobContext) -> list[tuple[ModelAdapter, AdapterOptions]]:
    """Return the ordered (adapter, options) fallback chain.

    Resolution order:
      1. `ctx.model_role` (operator override on this job in schedule.yaml)
         → build a fresh chain from that role's primary+fallbacks.
      2. `app["adapter_chain"]` (chat_brain hot-reloaded by Mirror startup
         and the config watcher) — used when the job's `model_role`
         matches the chat_brain default and the live chain is populated.
      3. Build a fresh chain from the handler's `default_model_role`.
      4. Singleton from the legacy `app["adapter"]` pair (test path).
    """
    role_name = resolve_role_name(ctx, ChatDigestJob.default_model_role)
    app = ctx.app
    app_chain: list[tuple[ModelAdapter, AdapterOptions]] = []
    if app is not None and hasattr(app, "get"):
        live = app.get("adapter_chain") or []
        if live:
            app_chain = [(a, o or AdapterOptions()) for a, o in live if a is not None]

    # Operator override always wins — they explicitly retargeted this
    # job, so route through the named role. If the chain can't build
    # (missing keys, every catalog ref unreachable) we return [] so the
    # job's `chain unavailable — skipped` branch fires. We do NOT silently
    # fall back to `app_chain` — that would run the digest on a
    # *different* role than the one the operator picked, contradicting
    # `engine._validate_model_role`'s "no silent fallback" contract.
    override_set = bool((ctx.model_role or "").strip())
    if override_set and role_name is not None:
        return build_chain_for_role(role_name, log_label="chat_digest")

    if app_chain:
        return app_chain

    if role_name is not None:
        built = build_chain_for_role(role_name, log_label="chat_digest")
        if built:
            return built

    if app is None or not hasattr(app, "get"):
        return []
    adapter = app.get("adapter")
    if adapter is None:
        return []
    return [(adapter, app.get("adapter_options") or AdapterOptions())]


def _collect_sessions(target: date) -> list[ChatRecord]:
    """Return every chat record whose wall-clock span covers `target`.

    Reads the chat records, which are the only session record there is.
    ARCHIVED ONES INCLUDED, deliberately: this job runs the morning after the
    day it summarises, and the first connection of a new day archives every
    chat left open on the previous one — so filtering them out would make the
    digest read an empty tree exactly when it has the most to say. Archiving
    is a shelf, not a retraction.

    Pre-fix, sessions were bucketed by a single `ended_at or started_at`
    stamp, so a session that crossed midnight (e.g. 23:30 D -> 00:10 D+1)
    landed entirely in D+1's digest, and D's digest missed the content.
    We now include a record if `target` falls within `[start.date(), end.date()]`.
    Cross-midnight conversations therefore appear in both D's and D+1's digest;
    the LLM is told the target date so each summary stays focused.
    """
    kept: list[ChatRecord] = []
    for record in chat_store.list_records(include_archived=True):
        start_dt = _parse_stamp(record.started_at)
        end_dt = _parse_stamp(record.ended_at) or start_dt
        if start_dt is None:
            continue
        start_d = start_dt.astimezone(timezone.utc).date()
        end_d = (end_dt or start_dt).astimezone(timezone.utc).date()
        if start_d <= target <= end_d:
            kept.append(record)
    return kept


def _parse_stamp(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None


def _message_date(msg: dict) -> date | None:
    stamp = msg.get("timestamp")
    if not isinstance(stamp, str) or not stamp.strip():
        return None
    dt = _parse_stamp(stamp)
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).date()


def _build_transcript(
    sessions: list[ChatRecord],
    max_chars: int,
    target: date,
) -> str:
    parts: list[str] = []
    total = 0
    for s in sessions:
        has_message_timestamps = any(
            _message_date(msg) is not None
            for msg in s.history
            if msg.get("role") in ("user", "assistant")
        )
        for msg in s.history:
            if msg.get("_reasoning"):
                continue
            role = msg.get("role")
            if role not in ("user", "assistant"):
                continue
            msg_day = _message_date(msg)
            if msg_day is not None and msg_day != target:
                continue
            if msg_day is None and has_message_timestamps:
                continue
            content = msg.get("content") or ""
            if not isinstance(content, str) or not content.strip():
                continue
            line = f"{role.upper()}: {content.strip()}\n"
            if total + len(line) > max_chars:
                return "".join(parts)
            parts.append(line)
            total += len(line)
    return "".join(parts)


async def _summarize_with_fallback(
    transcript: str,
    chain: list[tuple[ModelAdapter, AdapterOptions]],
    timeout_s: float,
) -> str:
    """Walk the adapter chain; return the first non-empty digest.

    Each member gets an independent `timeout_s` budget. Timeouts and
    RuntimeErrors (adapter-level failures) are logged and fall through to
    the next entry. An empty string from a successful call is also treated
    as a failure — we want a real digest, not a no-op.
    """
    prompt = f"{_SUMMARY_PROMPT}{transcript}\n--- END ---\n"
    for index, (adapter, options) in enumerate(chain):
        label = f"{options.provider or 'unknown'}/{options.model or '?'}"
        try:
            digest = await asyncio.wait_for(
                adapter.generate(prompt, options or AdapterOptions()),
                timeout=timeout_s,
            )
        except asyncio.TimeoutError:
            log.warning("chat_digest: %s timed out after %.1fs (chain idx=%d)", label, timeout_s, index)
            continue
        except Exception as exc:  # noqa: BLE001
            log.warning("chat_digest: %s call failed (%s) (chain idx=%d)", label, exc, index)
            continue
        if digest and digest.strip():
            if index > 0:
                log.info("chat_digest: fell back to chain idx=%d (%s)", index, label)
            return digest
        log.warning("chat_digest: %s returned empty digest (chain idx=%d)", label, index)
    return ""

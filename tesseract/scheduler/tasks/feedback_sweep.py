"""FeedbackSweepJob — Layer C of the feedback durability plan.

Daily sweep over yesterday's session transcripts + chat digest, looking for
directive-shaped operator statements ("always", "never", "from now on",
"don't", "stop X-ing", "remember to") that didn't get reflected on. Returns
*proposals* to the operator via the Workspace Inbox — never auto-writes a
memory. The operator confirms; `memory_save` then runs through the normal
ASK-tier path.

Disabled by default in `schedule.yaml`. Operator flips it on once Layer A
has been observed dry. Mirrors `chat_digest`'s shape: read-only inputs,
adapter-chain fallback, JSONL output, WS broadcast.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from tesseract.brain.boot import SESSIONS_DIR
from tesseract.brain.session_store import SessionState, list_sessions
from tesseract.kernel.adapters.base import AdapterOptions, ModelAdapter
from tesseract.memory.store import MemoryStore
from tesseract.paths import TESSERACT_HOME, log_dir
from tesseract.scheduler.base_job import BaseJob
from tesseract.scheduler.role_chain import build_chain_for_job
from tesseract.scheduler.types import JobContext, JobResult

log = logging.getLogger(__name__)

DEFAULT_TRANSCRIPT_CHARS = 8_000
DEFAULT_TIMEOUT_S = 60.0
ENVELOPE_KIND = "feedback_proposals"

_DIRECTIVE_PROMPT = (
    "You are scanning yesterday's transcripts between the operator and the assistant for "
    "directive-shaped operator statements that should be preserved as feedback "
    "memories across sessions. Look for patterns like 'always', 'never', 'from "
    "now on', 'don't', 'stop X-ing', 'remember to', 'when X happens, do Y'.\n\n"
    "For each candidate, decide whether it duplicates an existing feedback "
    "memory (titles + summaries listed below). Skip duplicates. For the rest, "
    "propose a feedback memory.\n\n"
    "Return ONLY a JSON object with this shape, no preamble:\n"
    '{"proposals": [{"title": "<short>", "summary": "<2-3 sentences capturing the rule '
    'and its why>", "importance": <int 1-10>, "source_quote": "<the operator '
    'sentence verbatim>"}]}\n\n'
    "Importance 8-10: load-bearing identity / always-or-never rules. "
    "Importance 5-7: workflow corrections worth preserving. "
    "Importance 1-4: skip — don't propose at all.\n\n"
    "If no candidates, return {\"proposals\": []}. Don't invent rules — silence is "
    "fine.\n\n"
)


class FeedbackSweepJob(BaseJob):
    uses_llm = True
    # A chain, not a role: the role this named existed only to hold its budget
    # line, and the line moved onto the `consolidate` manifest entry.
    default_model_chain = "chain_2"

    async def run(self, ctx: JobContext) -> JobResult:
        t0 = time.monotonic()
        try:
            target_date = (ctx.fired_at - timedelta(days=1)).date()
            session_dir = _resolve_session_dir(ctx)
            # Off the loop: reads + fully parses every active session file
            # (up to 10k) to find yesterday's.
            sessions = await asyncio.to_thread(_collect_sessions, target_date, session_dir)
            if not sessions:
                return _ok(ctx, t0, target_date, 0, 0, "no sessions to sweep")

            store_dir = _resolve_store_dir(ctx)
            existing = _existing_feedback_summary(store_dir)

            transcript = _build_transcript(
                sessions,
                int(ctx.config.get("max_transcript_chars", DEFAULT_TRANSCRIPT_CHARS)),
                target_date,
            )
            if not transcript.strip():
                return _ok(ctx, t0, target_date, len(sessions), 0, "empty transcript")

            chain = build_chain_for_job(
                ctx,
                default_role=None,
                default_chain=FeedbackSweepJob.default_model_chain,
                log_label="feedback_sweep",
            )
            if not chain:
                return _ok(ctx, t0, target_date, len(sessions), 0,
                           "no model reachable — skipped")

            prompt = _build_prompt(transcript, existing, target_date)
            raw = await _call_with_fallback(prompt, chain, DEFAULT_TIMEOUT_S)
            proposals = _parse_proposals(raw)

            log_dir = _resolve_log_dir(ctx)
            log_path = _write_jsonl(log_dir, target_date, proposals, sessions)
            _emit_inbox_events(ctx, target_date, proposals, log_path)

            await _broadcast(ctx, target_date, proposals, log_path)

            return _ok(
                ctx, t0, target_date, len(sessions), len(proposals),
                f"sessions={len(sessions)} proposals={len(proposals)}",
                payload_extra={"log_path": str(log_path)},
            )
        except Exception as exc:  # noqa: BLE001 — handler contract forbids raising
            log.exception("feedback_sweep crashed")
            return JobResult(
                job_name=ctx.job_name,
                run_id=ctx.run_id,
                ok=False,
                detail=f"unhandled: {exc!r}",
                duration_ms=(time.monotonic() - t0) * 1000.0,
            )


def _ok(
    ctx: JobContext,
    t0: float,
    target_date: Any,
    sessions: int,
    proposals: int,
    detail: str,
    *,
    payload_extra: dict[str, Any] | None = None,
) -> JobResult:
    payload = {
        "target_date": target_date.isoformat(),
        "sessions": sessions,
        "proposals": proposals,
    }
    if payload_extra:
        payload.update(payload_extra)
    return JobResult(
        job_name=ctx.job_name,
        run_id=ctx.run_id,
        ok=True,
        detail=detail,
        payload=payload,
        duration_ms=(time.monotonic() - t0) * 1000.0,
    )


def _resolve_session_dir(ctx: JobContext) -> Path:
    override = ctx.config.get("session_dir")
    if override:
        return Path(override)
    app = ctx.app
    if app is not None and hasattr(app, "get"):
        tdir = app.get("tesseract_dir")
        if tdir is not None:
            return Path(tdir) / "sessions"
    return SESSIONS_DIR


def _resolve_store_dir(ctx: JobContext) -> Path:
    override = ctx.config.get("store_dir")
    if override:
        return Path(override)
    app = ctx.app
    if app is not None and hasattr(app, "get"):
        tdir = app.get("tesseract_dir")
        if tdir is not None:
            return Path(tdir) / "memory-store"
    return TESSERACT_HOME / "memory-store"


def _resolve_log_dir(ctx: JobContext) -> Path:
    override = ctx.config.get("log_dir")
    if override:
        return Path(override)
    app = ctx.app
    if app is not None and hasattr(app, "get"):
        tdir = app.get("tesseract_dir")
        if tdir is not None:
            return Path(tdir) / "logs" / "feedback-sweep"
    return log_dir("feedback-sweep")


def _existing_feedback_summary(store_dir: Path) -> list[dict[str, Any]]:
    """Existing operator-directive titles + summaries for dedup hinting.

    Pulls both ``feedback`` and ``user`` records (active only) — the
    operator sometimes saves a durable rule under ``user`` (identity /
    preference) rather than ``feedback`` (correction). The sweep still
    *proposes* new memories under ``feedback`` (that's the conversational
    shape coming out of transcripts), but it must dedup against both
    subdirs to avoid re-proposing rules already saved as user.
    """
    if not store_dir.exists():
        return []
    try:
        store = MemoryStore(store_dir)
        records = store.list_active_directives(importance_floor=1)
    except Exception:
        log.exception("feedback_sweep: failed to load existing directives")
        return []
    return [{"title": fm.title, "summary": fm.summary} for fm in records]


def _build_prompt(
    transcript: str,
    existing: list[dict[str, Any]],
    target_date: Any,
) -> str:
    existing_block = (
        "\n".join(f"- {e['title']}: {e['summary']}" for e in existing)
        if existing else "(none)"
    )
    return (
        f"{_DIRECTIVE_PROMPT}"
        f"--- DATE ---\n{target_date.isoformat()}\n\n"
        f"--- EXISTING FEEDBACK MEMORIES ---\n{existing_block}\n\n"
        f"--- TRANSCRIPT ---\n{transcript}\n--- END ---\n"
    )


def _collect_sessions(target_date: Any, session_dir: Path) -> list[SessionState]:
    if not session_dir.exists():
        return []
    kept: list[SessionState] = []
    for _path, state in list_sessions(session_dir, limit=10_000):
        start = _parse_stamp(state.started_at)
        end = _parse_stamp(state.ended_at) or start
        if start is None:
            continue
        start_d = start.astimezone(timezone.utc).date()
        end_d = (end or start).astimezone(timezone.utc).date()
        if start_d <= target_date <= end_d:
            kept.append(state)
    return kept


def _parse_stamp(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None


def _build_transcript(
    sessions: list[SessionState],
    max_chars: int,
    target_date: Any,
) -> str:
    parts: list[str] = []
    total = 0
    for s in sessions:
        for msg in s.history:
            if msg.get("_reasoning"):
                continue
            role = msg.get("role")
            if role not in ("user", "assistant"):
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


async def _call_with_fallback(
    prompt: str,
    chain: list[tuple[ModelAdapter, AdapterOptions]],
    timeout_s: float,
) -> str:
    for index, (adapter, options) in enumerate(chain):
        label = f"{options.provider or '?'}/{options.model or '?'}"
        try:
            out = await asyncio.wait_for(adapter.generate(prompt, options), timeout=timeout_s)
        except asyncio.TimeoutError:
            log.warning("feedback_sweep: %s timed out after %.1fs", label, timeout_s)
            continue
        except Exception as exc:  # noqa: BLE001
            log.warning("feedback_sweep: %s call failed (%s)", label, exc)
            continue
        if out and out.strip():
            return out
        log.warning("feedback_sweep: %s returned empty", label)
    return ""


def _iter_top_level_json_objects(text: str) -> list[str]:
    """Yield every balanced top-level `{...}` substring, in order.

    Honours quoted strings — braces inside quotes don't shift the balance.
    Used by `_parse_proposals` to skip past prose-template braces (e.g.
    `{key}: value`) that aren't JSON, and try the next candidate. Greedy
    regex would have anchored on the first stray brace and dropped the
    real proposals object.
    """
    out: list[str] = []
    depth = 0
    start = -1
    in_string = False
    escape = False
    for idx, ch in enumerate(text):
        if escape:
            escape = False
            continue
        if in_string:
            if ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            continue
        if ch == "{":
            if depth == 0:
                start = idx
            depth += 1
        elif ch == "}":
            if depth == 0:
                continue
            depth -= 1
            if depth == 0 and start >= 0:
                out.append(text[start:idx + 1])
                start = -1
    return out


def _extract_first_json_object(text: str) -> str | None:
    """Return the first balanced top-level `{...}` that parses as JSON, or
    None. Skips template-style stray braces (e.g. `{key}` in prose) that
    don't form valid JSON."""
    for candidate in _iter_top_level_json_objects(text):
        try:
            json.loads(candidate)
        except json.JSONDecodeError:
            continue
        return candidate
    return None


def _parse_proposals(raw: str) -> list[dict[str, Any]]:
    """Extract `proposals` from the model's JSON output. Tolerant of fenced
    code blocks and trailing prose — finds the first balanced top-level
    JSON object."""
    if not raw or not raw.strip():
        return []
    blob = _extract_first_json_object(raw)
    if blob is None:
        log.warning("feedback_sweep: no JSON object in adapter output")
        return []
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        log.warning("feedback_sweep: JSON parse failed on adapter output")
        return []
    raw_props = data.get("proposals") or []
    if not isinstance(raw_props, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw_props:
        if not isinstance(item, dict):
            continue
        title = (item.get("title") or "").strip()
        summary = (item.get("summary") or "").strip()
        if not title or not summary:
            continue
        try:
            importance = int(item.get("importance") or 0)
        except (TypeError, ValueError):
            importance = 0
        if importance < 5:
            continue
        importance = min(10, importance)
        out.append({
            "title": title,
            "summary": summary,
            "importance": importance,
            "source_quote": (item.get("source_quote") or "").strip(),
        })
    return out


def _write_jsonl(
    log_dir: Path,
    target_date: Any,
    proposals: list[dict[str, Any]],
    sessions: list[SessionState],
) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"{target_date.isoformat()}.jsonl"
    session_ids = [getattr(s, "session_id", "") or "" for s in sessions]
    written_at = datetime.now(timezone.utc).isoformat()
    with path.open("w", encoding="utf-8") as fh:
        for prop in proposals:
            entry = {
                "written_at": written_at,
                "target_date": target_date.isoformat(),
                "source_session_ids": session_ids,
                **prop,
            }
            fh.write(json.dumps(entry) + "\n")
    return path


def _emit_inbox_events(
    ctx: JobContext,
    target_date: Any,
    proposals: list[dict[str, Any]],
    log_path: Path,
) -> None:
    """Emit one Workspace Inbox event per sweep proposal.

    Best-effort: a missing EventStore must not fail the job — the JSONL
    on disk + the WS envelope are still authoritative."""
    if not proposals:
        return
    try:
        from tesseract.workspace_events import EventStore, WorkspaceEvent
    except ImportError:
        return
    try:
        log_dir = _resolve_log_dir(ctx)
        store = EventStore(log_dir.parent)
    except Exception:
        log.exception("feedback_sweep: EventStore init failed")
        return
    for prop in proposals:
        title = (prop.get("title") or "Operator directive").strip()[:200]
        summary = (prop.get("summary") or "").strip()[:1200]
        try:
            store.append_event(WorkspaceEvent.new(
                kind="feedback_sweep",
                source="feedback_sweep",
                title=title,
                summary=summary,
                payload={
                    "action": "memory_save",
                    "proposed": {
                        "title": title,
                        "summary": summary,
                        "importance": prop.get("importance", 6),
                        "source_quote": prop.get("source_quote", ""),
                    },
                    "target_date": target_date.isoformat(),
                    "log_path": str(log_path),
                },
                priority=int(prop.get("importance", 6)),
            ))
        except Exception:
            log.exception("feedback_sweep: append event failed")


async def _broadcast(
    ctx: JobContext,
    target_date: Any,
    proposals: list[dict[str, Any]],
    log_path: Path,
) -> None:
    app = ctx.app
    if app is None or not hasattr(app, "get"):
        return
    sessions = app.get("server_sessions") or {}
    if not sessions:
        return
    try:
        from tesseract.mirror.server.envelope import make_envelope
        from tesseract.mirror.server.session import send_envelope
    except ImportError:
        return
    payload = {
        "source": "feedback_sweep",
        "target_date": target_date.isoformat(),
        "proposals": proposals,
        "log_path": str(log_path),
    }
    for sess in sessions.values():
        env = make_envelope(
            ENVELOPE_KIND, "background",
            getattr(sess, "session_id", ""), payload,
        )
        try:
            await send_envelope(sess, env)
        except Exception:
            log.exception(
                "feedback_sweep: send_envelope failed for %s",
                getattr(sess, "session_id", "?"),
            )

"""Observer — peripheral awareness over the chat session.

Two entry points: `observe(history, mode)` stateless one-shot, and
`observe_incremental(new_turns, mode)` stateful (rolling transcript,
3-strike circuit breaker, prompts sourced from the `observer` agent
definition).
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from tesseract.agents.loader import AgentDefinition
from tesseract.brain.cost import BudgetExhausted, CostLedger, CostUsage
from tesseract.brain.memory_suggestion import (
    SCHEMA_FOR_PROMPT,
    MemorySuggestion,
    next_observation_id,
    parse_suggestion,
)
from tesseract.brain.observation_transcript import ObservationTranscript, PtyLine
from tesseract.brain.observer_budget import CircuitBreaker
from tesseract.kernel.adapters.base import AdapterOptions, ChunkType, ModelAdapter
from tesseract.paths import home_dir, log_dir

logger = logging.getLogger(__name__)

ObserverMode = Literal["meta", "maintenance"]

_OBSERVATION_PROMPT = "Observation Prompt"
_SUGGESTION_PROMPT = "Suggestion Prompt"

DEFAULT_CONTEXT_TURNS = 12

# Mirrors `agents/observer.md` § "Hard banlist". The model is told never to
# emit these phrases, but it leaks them anyway when there's no real signal.
# Treating any output that matches as `NONE` server-side keeps the right
# panel + chat-stream surfaces clean instead of showing low-signal filler.
_BANLIST_PHRASES: tuple[str, ...] = (
    "something worth noting",
    "nothing significant",
    "a point of interest",
    "something to consider",
    "noteworthy moment",
    "interesting exchange",
)


def _is_banned_observation(text: str) -> bool:
    lowered = text.lower().strip().rstrip(".!?,;:")
    if not lowered:
        return False
    return any(phrase in lowered for phrase in _BANLIST_PHRASES)

def _observer_log_dir() -> Path:
    """Resolve the observer log dir at call time under `TESSERACT_HOME`.

    Pure — no I/O, no migration. An app update replaces the code tree
    (`Path(__file__)`-anchored paths get wiped); anchoring here off
    `home_dir()` instead means the log dir survives an update.
    """
    return log_dir("observer")


@dataclass
class ObserverConfig:
    model: str
    provider: str
    temperature: float
    max_output_tokens: int
    context_window: int
    timeout_seconds: int
    max_retries: int
    reasoning_effort: str = ""
    use_responses_api: bool = False
    stream: bool = True

    @classmethod
    def from_role_entry(cls, entry: dict[str, Any], provider_cfg: dict[str, Any]) -> "ObserverConfig":
        # No hardcoded defaults for infrastructure
        # values. Missing keys raise loudly rather than silently falling
        # back to invented numbers. `reasoning_effort` + `use_responses_api`
        # are feature flags whose absence legitimately means "off".
        return cls(
            model=entry["model"],
            provider=entry["provider"],
            temperature=entry["temperature"],
            max_output_tokens=entry["max_output_tokens"],
            context_window=entry["context_window"],
            timeout_seconds=provider_cfg["timeout_seconds"],
            max_retries=provider_cfg["max_retries"],
            reasoning_effort=entry.get("reasoning_effort", ""),
            use_responses_api=bool(entry.get("use_responses_api", False)),
            stream=bool(entry.get("stream", True)),
        )


class Observer:
    """Peripheral observer over the parent chat history.

    Holds its own adapter so observations don't block the chat stream and
    don't share token budget with the main brain's reply path.
    """

    def __init__(
        self,
        adapter: ModelAdapter,
        config: ObserverConfig,
        agent_def: AgentDefinition,
        cost_ledger: CostLedger | None = None,
    ) -> None:
        self._adapter = adapter
        self._config = config
        self._agent_def = agent_def
        self._cost_ledger = cost_ledger
        self._transcript = ObservationTranscript()
        self._circuit_breaker = CircuitBreaker()
        self._fires_total = 0
        self._tokens_used_total = 0
        self._last_fired_at: str | None = None
        self._last_suggestion_observation_id: str | None = None
        # Serializes observe_incremental across concurrent producers
        # (subscriber loop_end + PTY push tasks) so transcript/breaker/
        # counter mutations don't interleave.
        self._lock = asyncio.Lock()

    def reset(self) -> None:
        """Clear transcript + PTY buffer + memory deltas + breaker + pending
        suggestion marker. Used by disarm — disarm/rearm must restore a
        clean observer. Counters (fires_total, tokens_used_total,
        last_fired_at) persist across arm/disarm by design."""
        self._transcript.reset()
        self._circuit_breaker.reset()
        self._last_suggestion_observation_id = None

    def get_stats(self) -> dict[str, Any]:
        """Counters persist across arm/disarm; only the transcript resets on `reset()`."""
        return {
            "fires_total": self._fires_total,
            "tokens_used_total": self._tokens_used_total,
            "last_fired_at": self._last_fired_at,
            "circuit_breaker_state": self._circuit_breaker.state(),
            "pending_suggestion_count": 1 if self._last_suggestion_observation_id else 0,
            "transcript_turns": len(self._transcript.chat_turns),
        }

    def drop_pty_for_pane(self, pane_id: str) -> int:
        """Revoke buffered PTY content for a pane (consent revoke / pane close)."""
        return self._transcript.drop_pty_lines_for_pane(pane_id)

    @property
    def options(self) -> AdapterOptions:
        return AdapterOptions(
            model=self._config.model,
            provider=self._config.provider,
            temperature=self._config.temperature,
            max_output_tokens=self._config.max_output_tokens,
            context_window=self._config.context_window,
            reasoning_effort=self._config.reasoning_effort,
            use_responses_api=self._config.use_responses_api,
            stream=self._config.stream,
        )

    async def observe(
        self,
        history: list[dict[str, Any]],
        mode: ObserverMode = "meta",
        context_turns: int = DEFAULT_CONTEXT_TURNS,
        *,
        session_id: str = "",
    ) -> str:
        """Stateless one-shot; does not update `self._transcript`.

        `mode` is accepted for caller back-compat (REPL `/observe meta|maintenance`
        + Mirror WS + REST). Only `meta` is implemented today; `maintenance`
        is a deferred feature — both modes currently compose the same
        `Observation Prompt` section. When a dedicated maintenance prompt
        lands in `agents/observer.md`, wire it here via section selection.

        Every non-empty observation is appended to
        `tesseract/logs/observer/YYYY-MM-DD.jsonl` (fail-open — log
        write errors never propagate) so operators can inspect the
        observer's output outside the Mirror + the conscience heartbeat
        can eventually derive an `observer_silence` signal from it.
        """
        if mode != "meta":
            logger.info(
                "observer.observe: mode=%r currently uses the meta prompt "
                "(maintenance prompt not yet implemented — fix-pass 2026-04-20)",
                mode,
            )
        trimmed = _trim_history_for_observer(history, context_turns)
        if not trimmed:
            return ""
        if self._circuit_breaker.is_open():
            # Gated as well as counted. Counting a hang without gating on it
            # protects the incremental path but still spends the full timeout
            # on every hand-run call, which is the cost this decision was
            # taken to stop. Logged rather than silent: a `/observe` that
            # returns nothing has to say why.
            logger.warning(
                "observer breaker open (provider %s/%s) — skipping stateless observation",
                self._config.provider,
                self._config.model,
            )
            return ""
        try:
            text, _tokens = await asyncio.wait_for(
                self._run_stream(self._compose_messages(trimmed)),
                timeout=self._config.timeout_seconds,
            )
        except BudgetExhausted as exc:
            logger.info("observer skipped — %s", exc)
            return ""
        except asyncio.TimeoutError:
            # Counted, like the incremental path. These two handlers used to
            # disagree: a hang here only dropped the observation, so a
            # provider that accepts the connection and never streams could
            # cost the full timeout on every operator-triggered `/observe`
            # forever with the breaker still green. The breaker is per
            # provider-entry, not per call path, so both paths feed it.
            logger.warning(
                "observer call exceeded %ss (provider %s/%s hung) — counting as failure",
                self._config.timeout_seconds,
                self._config.provider,
                self._config.model,
            )
            self._circuit_breaker.record_failure()
            return ""
        # Stateless and incremental paths share the same counter so the
        # ObserverStatsChip "N obs" / "N tok" / "last fired" reading reflects
        # *every* model invocation, not just stateful ones (fix-pass 2026-05-01).
        # Lock matches `observe_incremental`'s mutation site so concurrent
        # stateless callers don't race the counters.
        async with self._lock:
            self._fires_total += 1
            self._tokens_used_total += _tokens
            self._last_fired_at = datetime.now(timezone.utc).isoformat()
            # Paired with the `record_failure` above. The breaker counts
            # CONSECUTIVE failures, so a path that only ever reports failures
            # would let three hangs spread across an otherwise healthy session
            # open it — the success has to reset the run for the count to mean
            # what its name says.
            self._circuit_breaker.record_success()
        out = text or ""
        if out:
            _append_observation_log(mode=mode, session_id=session_id, text=out)
        return out

    async def observe_incremental(
        self,
        new_turns: list[dict[str, Any] | PtyLine],
        mode: ObserverMode = "meta",
    ) -> MemorySuggestion | None:
        """Stateful — accumulates turns and PTY lines; returns a typed suggestion
        or `None` (breaker open / nothing new / parse failure / PTY-only feed).

        Items with ``role == "pty"`` go to the PTY buffer; all others are chat
        turns. PTY-only feeds enrich context but do not trigger an LLM call.
        """
        async with self._lock:
            if self._circuit_breaker.is_open():
                return None

            chat_items = [t for t in new_turns if t.get("role") != "pty"]
            pty_items = [t for t in new_turns if t.get("role") == "pty"]

            if pty_items:
                self._transcript.append_pty_lines(pty_items)

            added = self._transcript.append_chat_turns(chat_items) if chat_items else 0
            if added == 0:
                return None

            start = max(0, len(self._transcript.chat_turns) - DEFAULT_CONTEXT_TURNS)
            window = list(self._transcript.chat_turns)[start:]

            observation_id = next_observation_id()
            messages = self._compose_messages(
                window,
                section=_SUGGESTION_PROMPT,
                extra_placeholders={
                    "{schema}": SCHEMA_FOR_PROMPT,
                    "{observation_id}": observation_id,
                },
                user_nudge="Emit your one JSON suggestion now, or NONE.",
            )
            try:
                # Hard ceiling around the whole stream: a provider that
                # accepts the connection but never streams (NIM, found live
                # 2026-07-30) otherwise holds `self._lock` forever — every
                # later turn queues behind it and the observer zombifies
                # with zero fires, zero warnings, breaker green.
                text, tokens = await asyncio.wait_for(
                    self._run_stream(messages),
                    timeout=self._config.timeout_seconds,
                )
            except BudgetExhausted as exc:
                # Budget skip is not an adapter failure — neither the circuit
                # breaker nor the fires counter should move. Observer will
                # wake up again once the ledger crosses midnight (local-tz)
                # or the operator raises the cap in roles.yaml.
                logger.info("observer skipped — %s", exc)
                return None
            except asyncio.TimeoutError:
                logger.warning(
                    "observer call exceeded %ss (provider %s/%s hung) — counting as failure",
                    self._config.timeout_seconds,
                    self._config.provider,
                    self._config.model,
                )
                self._circuit_breaker.record_failure()
                return None
            self._fires_total += 1
            self._tokens_used_total += tokens
            self._last_fired_at = datetime.now(timezone.utc).isoformat()

            if text is None:
                self._circuit_breaker.record_failure()
                return None

            self._circuit_breaker.record_success()
            suggestion = parse_suggestion(text, fallback_observation_id=observation_id)
            self._last_suggestion_observation_id = (
                suggestion.observation_id if suggestion else None
            )
            return suggestion

    def _compose_messages(
        self,
        transcript_turns: list[dict[str, Any]],
        section: str = _OBSERVATION_PROMPT,
        extra_placeholders: dict[str, str] | None = None,
        user_nudge: str = "Emit your one observation now, or NONE.",
    ) -> list[dict[str, Any]]:
        return [
            {
                "role": "system",
                "content": self._compose_system_prompt(
                    transcript_turns, section, extra_placeholders or {}
                ),
            },
            {"role": "user", "content": user_nudge},
        ]

    def _compose_system_prompt(
        self,
        transcript_turns: list[dict[str, Any]],
        section: str,
        extra_placeholders: dict[str, str],
    ) -> str:
        template = self._agent_def.get_section(section)
        if not template:
            logger.warning(
                "observer agent %r missing %r section; using empty system prompt",
                self._agent_def.name, section,
            )
            return ""

        transcript_text = "\n".join(
            f"{t['role']}: {t['content']}" for t in transcript_turns
        ) or "(empty)"
        pty_context_text = _render_pty_lines(self._transcript.pty_buffer)

        filled = (
            template
            .replace("{transcript}", transcript_text)
            .replace("{pty_context}", pty_context_text)
        )
        for placeholder, value in extra_placeholders.items():
            filled = filled.replace(placeholder, value)
        return filled

    async def _run_stream(
        self, messages: list[dict[str, Any]]
    ) -> tuple[str | None, int]:
        """Returns (text, tokens). `None` text signals adapter error so callers
        can trip the circuit breaker; `""` means NONE/empty (not an error).

        Preflights the cost ledger before the stream; `BudgetExhausted`
        propagates to the caller so budget-skipped observations bypass both
        the fires/tokens counters and the circuit breaker. After successful
        completion, records full input/output/cached counts against
        `observer_agent` in the shared ledger.
        """
        if self._cost_ledger is not None:
            self._cost_ledger.check_preflight("observer_agent")

        collected: list[str] = []
        input_tokens = 0
        output_tokens = 0
        cached_tokens = 0
        cache_creation_tokens = 0
        try:
            async for chunk in self._adapter.stream(
                messages=messages, tools=None, options=self.options
            ):
                if chunk.type == ChunkType.TEXT:
                    collected.append(chunk.text)
                elif chunk.type == ChunkType.STOP:
                    usage = chunk.raw.get("usage") if chunk.raw else None
                    if isinstance(usage, dict):
                        input_tokens = int(usage.get("input_tokens") or 0)
                        output_tokens = int(usage.get("output_tokens") or 0)
                        cached_tokens = int(usage.get("cached_tokens") or 0)
                        cache_creation_tokens = int(usage.get("cache_creation_tokens") or 0)
                elif chunk.type == ChunkType.ERROR:
                    logger.warning("observer stream error: %s", chunk.error)
                    return None, output_tokens
        except Exception as e:
            logger.warning("observer call failed: %s", e)
            return None, output_tokens

        joined = "".join(collected)
        if output_tokens == 0 and joined:
            output_tokens = max(1, len(joined) // 4)

        if self._cost_ledger is not None and (input_tokens or output_tokens):
            try:
                self._cost_ledger.record(
                    "observer_agent",
                    self._config.model,
                    CostUsage(
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        cached_tokens=cached_tokens,
                        cache_creation_tokens=cache_creation_tokens,
                    ),
                )
            except RuntimeError:
                logger.exception("observer cost record failed")

        text = joined.strip()
        if not text or text.upper().rstrip(".!") == "NONE":
            return "", output_tokens
        if _is_banned_observation(text):
            logger.info("observer banlist hit — dropping %r", text[:80])
            return "", output_tokens
        return text, output_tokens


def build_observer_from_config(
    adapter: ModelAdapter,
    role_entry: dict[str, Any],
    provider_cfg: dict[str, Any],
    agent_def: AgentDefinition,
    cost_ledger: CostLedger | None = None,
) -> Observer:
    """Build an Observer from an already-constructed adapter and a resolved
    role entry.

    Adapter dispatch lives in `brain/boot.py::build_adapter` (keyed on
    `providers.yaml::<tier>.<provider>.adapter`) — observer is provider-
    agnostic and works with any adapter the catalog exposes (openai,
    gemini, anthropic, …). Caller is responsible for handling adapter
    construction failures (e.g. missing API keys) and continuing to the
    next fallback ref. `cost_ledger` flows through to the Observer so
    observer spend debits the shared daily total.
    """
    config = ObserverConfig.from_role_entry(role_entry, provider_cfg)
    return Observer(adapter=adapter, config=config, agent_def=agent_def, cost_ledger=cost_ledger)


def _trim_history_for_observer(
    history: list[dict[str, Any]],
    context_turns: int,
) -> list[dict[str, Any]]:
    """Keep only user + assistant text turns, tail-trimmed to context_turns."""
    plain: list[dict[str, Any]] = []
    for msg in history:
        role = msg.get("role")
        if role not in ("user", "assistant"):
            continue
        content = msg.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        plain.append({"role": role, "content": content})
    return plain[-context_turns:]


def _append_observation_log(*, mode: str, session_id: str, text: str) -> None:
    """Append one observation record to `tesseract/logs/observer/YYYY-MM-DD.jsonl`.

    Fail-open: disk errors are logged at WARNING and swallowed — the
    observer must never refuse to return an observation just because
    the log path is unwritable.
    """
    try:
        log_dir = _observer_log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc)
        target = log_dir / f"{now.date().isoformat()}.jsonl"
        record = {
            "timestamp": now.isoformat(),
            "mode": mode,
            "session_id": session_id,
            "text": text,
        }
        with target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001 — fail-open
        logger.warning("observer log write failed: %s", exc)


def _render_pty_lines(lines) -> str:
    if not lines:
        return "(none)"
    return "\n".join(
        f"- [{line.get('timestamp', '?')}] {line.get('pane_id', '?')}: "
        f"{(line.get('text') or '').rstrip()}"
        for line in lines
    )

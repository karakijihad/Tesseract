"""Observer — peripheral awareness over any conversation the runtime holds.

Two entry points: `observe(history, mode)` stateless one-shot, and
`observe_incremental(new_turns, transcript, mode)` stateful (3-strike
circuit breaker). Every string either sends to the model, or checks against
what the model said, is read off the `observer` agent card
(`agents/observer.md`) at construction time — see `ObserverPolicy` and
`ObserverCardError` below. This module holds no prompt text and no banned
phrase: a string the model reads belongs in the card, never here.

The rolling transcript is the CALLER's — one per conversation, held by
its `ChatSession` — so the cockpit and a channel are observed by the same
instance without interleaving. What the observer owns is what belongs to
the machine rather than to a conversation: the PTY buffer, the breaker
and the counters.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from tesseract.agents.loader import AgentDefinition
from tesseract.brain.context_signal import Fullness
from tesseract.brain.cost import BudgetExhausted, CostLedger, CostUsage
from tesseract.brain.memory_suggestion import next_observation_id
from tesseract.brain.observation_transcript import ObservationTranscript, PtyBuffer, PtyLine
from tesseract.brain.observer_reading import (
    EMPTY_READING,
    ObserverReading,
    parse_reading,
)
from tesseract.brain.observer_budget import CircuitBreaker
from tesseract.kernel.adapters.base import AdapterOptions, ChunkType, ModelAdapter
from tesseract.paths import home_dir, log_dir

logger = logging.getLogger(__name__)

# Only one mode is implemented. The parameter stays because it is part of the
# wire contract the REPL, the Mirror WS `/observe` command and the REST
# `POST /api/observer/observe` route already speak (the envelope they emit
# carries `mode` back to the caller); removing it would be a frontend change,
# not an observer one.
ObserverMode = Literal["meta"]

# The two job names the card's `jobs:` frontmatter mapping must declare. Not
# prompt text — a fixed pair of call-site identifiers, the same way `observe`
# and `observe_incremental` are a fixed pair of methods. Which SECTION and
# CUE each job uses is entirely the card's to say.
_OBSERVATION_JOB = "observation"
_READING_JOB = "reading"

#: The section carrying the JSON shape `{schema}` is filled from. A dedicated
#: section rather than a frontmatter string because it is a fenced code block,
#: not a short value, and sections merge whole under a shadow the same way
#: `Suggestion Prompt` does.
_READING_SCHEMA_SECTION = "Reading Schema"

#: Which `{placeholder}` tokens each job's section body must contain, checked
#: once at construction so a card missing one fails loudly instead of shipping
#: a prompt with a literal unfilled `{schema}` in it.
_JOB_PLACEHOLDERS: dict[str, tuple[str, ...]] = {
    _OBSERVATION_JOB: ("{transcript}", "{pty_context}", "{banlist}"),
    _READING_JOB: ("{transcript}", "{pty_context}", "{schema}", "{room_left}", "{observation_id}"),
}


class ObserverCardError(RuntimeError):
    """The observer card is missing a section, a frontmatter key, or a
    placeholder its own `jobs:` declaration says it needs.

    Raised once, at construction (`ObserverPolicy.from_agent_def`, called
    from `Observer.__init__` / `build_observer_from_config`). Callers that
    build the observer as part of boot catch this and degrade to "no
    observer this run" rather than crash — see `brain/boot.py::build_observer`.
    """


@dataclass(frozen=True)
class ObserverJob:
    section: str
    cue: str


@dataclass(frozen=True)
class ObserverPolicy:
    """Everything the observer's prompts and server-side checks need, read
    off the card's frontmatter and validated once.

    A plain, easily-hand-built dataclass on purpose: a test standing up a bare
    `Observer` for something unrelated to the card (concurrency, timeouts,
    counters) can construct a minimal one directly rather than writing a
    throwaway card to disk.
    """

    preamble: str
    jobs: dict[str, ObserverJob]
    context_turns: int
    silence: str
    banlist: tuple[str, ...]
    transcript_line: str
    transcript_empty: str
    pty_line: str
    pty_empty: str
    roles_observed: tuple[str, ...]
    room_unmeasured: str
    room_measured: str

    @classmethod
    def from_agent_def(cls, agent_def: AgentDefinition) -> "ObserverPolicy":
        """Validate `agent_def`'s card in full and build the policy it
        describes, or raise `ObserverCardError` naming what is missing.

        Every property this class needs is checked here, once, rather than
        discovered piecemeal on whichever call first touches it — a card
        that loads but cannot run a turn is exactly the "fails open, sends
        an empty system prompt" defect this phase exists to close.
        """
        name = agent_def.name or "observer"
        fm = agent_def.raw_frontmatter

        def _require(key: str) -> Any:
            if key not in fm:
                raise ObserverCardError(
                    f"agent {name!r}: frontmatter is missing required key {key!r}"
                )
            return fm[key]

        def _require_str(key: str) -> str:
            value = _require(key)
            if not isinstance(value, str) or not value.strip():
                raise ObserverCardError(
                    f"agent {name!r}: frontmatter {key!r} must be a non-empty string"
                )
            return value.strip()

        preamble = _require_str("preamble")
        if not agent_def.get_section(preamble):
            raise ObserverCardError(
                f"agent {name!r}: preamble section {preamble!r} is missing or empty"
            )

        raw_jobs = _require("jobs")
        if not isinstance(raw_jobs, dict) or not raw_jobs:
            raise ObserverCardError(
                f"agent {name!r}: frontmatter 'jobs' must be a non-empty mapping"
            )
        jobs: dict[str, ObserverJob] = {}
        for job_name, required_placeholders in _JOB_PLACEHOLDERS.items():
            raw_job = raw_jobs.get(job_name)
            if not isinstance(raw_job, dict):
                raise ObserverCardError(
                    f"agent {name!r}: frontmatter 'jobs.{job_name}' is missing or not a mapping"
                )
            section = raw_job.get("section")
            cue = raw_job.get("cue")
            if not isinstance(section, str) or not section.strip():
                raise ObserverCardError(
                    f"agent {name!r}: frontmatter 'jobs.{job_name}.section' is missing"
                )
            if not isinstance(cue, str) or not cue.strip():
                raise ObserverCardError(
                    f"agent {name!r}: frontmatter 'jobs.{job_name}.cue' is missing"
                )
            section = section.strip()
            body = agent_def.get_section(section)
            if not body:
                raise ObserverCardError(
                    f"agent {name!r}: section {section!r} (jobs.{job_name}) is missing or empty"
                )
            for placeholder in required_placeholders:
                if placeholder not in body:
                    raise ObserverCardError(
                        f"agent {name!r}: section {section!r} (jobs.{job_name}) is missing "
                        f"placeholder {placeholder!r}"
                    )
            jobs[job_name] = ObserverJob(section=section, cue=cue.strip())

        context_turns = _require("context_turns")
        if isinstance(context_turns, bool) or not isinstance(context_turns, int) or context_turns <= 0:
            raise ObserverCardError(
                f"agent {name!r}: frontmatter 'context_turns' must be a positive integer"
            )

        silence = _require_str("silence")

        raw_banlist = _require("banlist")
        if not isinstance(raw_banlist, list):
            raise ObserverCardError(f"agent {name!r}: frontmatter 'banlist' must be a list")
        banlist = tuple(str(p).strip().lower() for p in raw_banlist if str(p).strip())

        transcript_line = _require_str("transcript_line")
        for placeholder in ("{role}", "{content}"):
            if placeholder not in transcript_line:
                raise ObserverCardError(
                    f"agent {name!r}: frontmatter 'transcript_line' is missing placeholder {placeholder!r}"
                )
        transcript_empty = _require_str("transcript_empty")

        pty_line = _require_str("pty_line")
        for placeholder in ("{timestamp}", "{pane_id}", "{text}"):
            if placeholder not in pty_line:
                raise ObserverCardError(
                    f"agent {name!r}: frontmatter 'pty_line' is missing placeholder {placeholder!r}"
                )
        pty_empty = _require_str("pty_empty")

        raw_roles = _require("roles_observed")
        if not isinstance(raw_roles, list) or not raw_roles:
            raise ObserverCardError(
                f"agent {name!r}: frontmatter 'roles_observed' must be a non-empty list"
            )
        roles_observed = tuple(str(r).strip() for r in raw_roles if str(r).strip())

        room_unmeasured = _require_str("room_unmeasured")
        room_measured = _require_str("room_measured")
        for placeholder in ("{percent}", "{conversation_tokens}", "{trigger_tokens}"):
            if placeholder not in room_measured:
                raise ObserverCardError(
                    f"agent {name!r}: frontmatter 'room_measured' is missing placeholder {placeholder!r}"
                )

        if not agent_def.get_section(_READING_SCHEMA_SECTION):
            raise ObserverCardError(
                f"agent {name!r}: section {_READING_SCHEMA_SECTION!r} is missing or empty"
            )

        return cls(
            preamble=preamble,
            jobs=jobs,
            context_turns=context_turns,
            silence=silence,
            banlist=banlist,
            transcript_line=transcript_line,
            transcript_empty=transcript_empty,
            pty_line=pty_line,
            pty_empty=pty_empty,
            roles_observed=roles_observed,
            room_unmeasured=room_unmeasured,
            room_measured=room_measured,
        )


def _is_banned_observation(text: str, banlist: tuple[str, ...]) -> bool:
    lowered = text.lower().strip().rstrip(".!?,;:")
    if not lowered:
        return False
    return any(phrase in lowered for phrase in banlist)

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
    # Which catalog tier the role rides, so its cost rows say so themselves.
    tier: str = ""
    use_responses_api: bool = False
    prompt_cache_explicit: bool = False
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
            tier=entry.get("tier", ""),
            use_responses_api=bool(entry.get("use_responses_api", False)),
            prompt_cache_explicit=bool(entry.get("prompt_cache_explicit", False)),
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
        # Raises `ObserverCardError` on a missing section, key or placeholder.
        # Validated once, here, rather than per call: a card that cannot run a
        # turn should fail at construction, not send an empty system prompt on
        # the first call that discovers it. `build_observer_from_config`'s
        # caller (`brain/boot.py::build_observer`) catches this per ref and
        # degrades to no observer this run.
        self._policy = ObserverPolicy.from_agent_def(agent_def)
        self._adapter = adapter
        self._config = config
        self._agent_def = agent_def
        self._cost_ledger = cost_ledger
        self._pty = PtyBuffer()
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
        """Clear PTY buffer + breaker + pending suggestion marker. Used by
        disarm — disarm/rearm must restore a clean observer. Counters
        (fires_total, tokens_used_total, last_fired_at) persist across
        arm/disarm by design, and a conversation's transcript is its own:
        it dies with the conversation rather than with an arm cycle."""
        self._pty.reset()
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
        }

    def _panes_in_the_prompt(self) -> list[str]:
        """Which panes the next prompt will quote, so the row can name them."""
        return sorted({
            str(line.get("pane_id") or "")
            for line in self._pty.lines
            if line.get("pane_id")
        })

    def drop_pty_for_pane(self, pane_id: str) -> int:
        """Stop holding a pane's terminal output. The buffer, and nothing else.

        This is the pane going away: closed, or its observer switch turned
        off. Nothing about it is a statement that what the assistant already
        learned should be unlearned, so the records stay. Closing a terminal
        is finishing work, not withdrawing permission.
        """
        return self._pty.drop_pane(pane_id)

    async def forget_pane(self, pane_id: str) -> int:
        """The operator withdrawing consent: the buffer AND the records.

        A revoke that reaches memory and not disk is a revoke in name only.
        The buffer is the terminal content itself and goes at once. The log
        holds the observer's PROSE about that content, which can quote it and
        cannot be scrubbed sentence by sentence without guessing which half
        was the secret, so the whole row goes.

        Awaited rather than launched. The disk half runs on a thread because
        rewriting a fortnight of files on the loop stops the health probe the
        supervisor kills the backend for missing; it is awaited because a
        detached task holds no reference anybody keeps, can be collected
        mid-flight, and is not joined at shutdown. A revoke that silently did
        not finish is the failure this whole function exists to prevent.
        """
        dropped = self._pty.drop_pane(pane_id)
        await asyncio.to_thread(_purge_pane_from_log, pane_id)
        return dropped

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
            prompt_cache_explicit=self._config.prompt_cache_explicit,
            stream=self._config.stream,
        )

    async def observe(
        self,
        history: list[dict[str, Any]],
        mode: ObserverMode = "meta",
        context_turns: int | None = None,
        *,
        session_id: str = "",
    ) -> str:
        """Stateless one-shot; does not update `self._transcript`.

        `mode` is accepted for wire back-compat (REPL `/observe`, Mirror WS,
        REST `POST /api/observer/observe`) — see `ObserverMode`'s docstring.
        `context_turns=None` (the default) uses the card's own
        `context_turns`; a caller may still override it.

        Every non-empty observation is appended to
        `tesseract/logs/observer/YYYY-MM-DD.jsonl` (fail-open — log
        write errors never propagate) so operators can inspect the
        observer's output outside the Mirror + the conscience heartbeat
        can eventually derive an `observer_silence` signal from it.
        """
        turns = context_turns if context_turns is not None else self._policy.context_turns
        trimmed = _trim_history_for_observer(history, turns, self._policy.roles_observed)
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
        # Read before the call, because the buffer keeps filling while it
        # runs and what this row has to name is what went INTO the prompt.
        panes_read = self._panes_in_the_prompt()
        try:
            text, _tokens = await asyncio.wait_for(
                self._run_stream(self._compose_messages(trimmed, job=_OBSERVATION_JOB)),
                timeout=self._config.timeout_seconds,
            )
        except BudgetExhausted as exc:
            logger.info("observer skipped — %s", exc)
            return ""
        except asyncio.TimeoutError:
            # Counted, like the incremental path. Dropping the observation
            # without counting would let a provider that accepts the connection
            # and never streams cost the full timeout on every
            # operator-triggered `/observe` with the breaker still green. The
            # breaker is per provider-entry, not per call path.
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
        # *every* model invocation, not just stateful ones.
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
            _append_observation_log(
                mode=mode, session_id=session_id, text=out, panes=panes_read,
            )
        return out

    async def feed_pty(self, lines: list[PtyLine]) -> None:
        """Buffer terminal output as context for the next observation.

        A pane belongs to the machine rather than to a conversation, so
        this names none and never fires the model on its own — it enriches
        whatever is observed next, wherever that happens.
        """
        if not lines:
            return
        async with self._lock:
            self._pty.append_lines(lines)

    async def observe_incremental(
        self,
        new_turns: list[dict[str, Any]],
        transcript: ObservationTranscript,
        mode: ObserverMode = "meta",
        room: Fullness | None = None,
    ) -> ObserverReading:
        """Stateful over the CALLER's transcript; returns what one call read.

        Both halves are empty when the breaker is open, when nothing is new,
        or when the reply could not be decoded, and either half may be empty
        on its own. The reading is never `None`: a caller that has to ask
        whether it got an object before asking what is in it gets the emptiness
        check wrong eventually, and this one runs on every turn.

        `transcript` is the conversation's own rolling window, handed in by
        whoever owns the conversation, so a cockpit chat and a Telegram
        chat observed in the same second cannot bleed into each other.
        PTY context is the machine's and arrives via `feed_pty`.
        """
        async with self._lock:
            if self._circuit_breaker.is_open():
                return EMPTY_READING

            added = transcript.append_chat_turns(new_turns)
            if added == 0:
                return EMPTY_READING

            start = max(0, len(transcript.chat_turns) - self._policy.context_turns)
            window = list(transcript.chat_turns)[start:]

            observation_id = next_observation_id()
            messages = self._compose_messages(
                window,
                job=_READING_JOB,
                extra_placeholders={
                    "{schema}": self._agent_def.get_section(_READING_SCHEMA_SECTION),
                    "{observation_id}": observation_id,
                    "{room_left}": _describe_room(room, self._policy),
                },
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
                return EMPTY_READING
            except asyncio.TimeoutError:
                logger.warning(
                    "observer call exceeded %ss (provider %s/%s hung) — counting as failure",
                    self._config.timeout_seconds,
                    self._config.provider,
                    self._config.model,
                )
                self._circuit_breaker.record_failure()
                return EMPTY_READING
            self._fires_total += 1
            self._tokens_used_total += tokens
            self._last_fired_at = datetime.now(timezone.utc).isoformat()

            if text is None:
                self._circuit_breaker.record_failure()
                return EMPTY_READING

            self._circuit_breaker.record_success()
            reading = parse_reading(
                text, fallback_observation_id=observation_id, silence=self._policy.silence,
            )
            # Written on after the parse: what the model says about the room
            # is the one thing in its reply it was told rather than saw.
            reading = _with_room(reading, _room_percent(room))
            self._last_suggestion_observation_id = (
                reading.suggestion.observation_id if reading.suggestion else None
            )
            return reading

    def _compose_messages(
        self,
        transcript_turns: list[dict[str, Any]],
        job: str,
        extra_placeholders: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        # One funnel for both `observe` and `observe_incremental`, which is
        # why the invocation is recorded here: it is the point where the card
        # becomes a model call, and counting it at boot (where the observer is
        # built) would say it ran on a machine that never observed anything.
        from tesseract.agents.invocations import record as _record_invocation

        _record_invocation(self._agent_def.name or "observer", via="observer")

        job_spec = self._policy.jobs[job]
        return [
            {
                "role": "system",
                "content": self._compose_system_prompt(
                    transcript_turns, job_spec.section, extra_placeholders or {}
                ),
            },
            {"role": "user", "content": job_spec.cue},
        ]

    def _compose_system_prompt(
        self,
        transcript_turns: list[dict[str, Any]],
        section: str,
        extra_placeholders: dict[str, str],
    ) -> str:
        # No missing-section fallback here: `ObserverPolicy.from_agent_def`
        # already refused construction if the preamble or any job section was
        # absent or empty, so by the time a call reaches this method both are
        # guaranteed present. That is what closes the old defect (a missing
        # section silently sending an EMPTY system prompt) rather than
        # papering over it per call.
        preamble_text = self._agent_def.get_section(self._policy.preamble)
        job_text = self._agent_def.get_section(section)
        template = f"{preamble_text}\n\n{job_text}"

        policy = self._policy
        transcript_text = "\n".join(
            policy.transcript_line.format(role=t["role"], content=t["content"])
            for t in transcript_turns
        ) or policy.transcript_empty
        pty_context_text = _render_pty_lines(self._pty.lines, policy)
        banlist_text = ", ".join(f'"{p}"' for p in policy.banlist)

        # One pass over the card's own text. Chained replaces would run each
        # later placeholder over the conversation already spliced in, so a
        # turn that quoted `{schema}` would reach the model rewritten.
        values = {
            "{transcript}": transcript_text,
            "{pty_context}": pty_context_text,
            "{banlist}": banlist_text,
            **extra_placeholders,
        }
        pattern = re.compile("|".join(re.escape(key) for key in values))
        return pattern.sub(lambda match: values[match.group(0)], template)

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
        # The provider's own usage dict, kept whole rather than unpacked into
        # locals: `CostUsage.from_raw` is the one place that decides what a
        # missing key means, and three copies of that decision is what this
        # function used to be one of.
        usage_raw: dict[str, Any] = {}
        input_tokens = 0
        output_tokens = 0
        try:
            async for chunk in self._adapter.stream(
                messages=messages, tools=None, options=self.options
            ):
                if chunk.type == ChunkType.TEXT:
                    collected.append(chunk.text)
                elif chunk.type == ChunkType.STOP:
                    usage = chunk.raw.get("usage") if chunk.raw else None
                    if isinstance(usage, dict):
                        usage_raw = dict(usage)
                        input_tokens = int(usage.get("input_tokens") or 0)
                        output_tokens = int(usage.get("output_tokens") or 0)
                elif chunk.type == ChunkType.ERROR:
                    logger.warning("observer stream error: %s", chunk.error)
                    return None, output_tokens
        except Exception as e:
            logger.warning("observer call failed: %s", e)
            return None, output_tokens

        joined = "".join(collected)
        if output_tokens == 0 and joined:
            output_tokens = max(1, len(joined) // 4)
            # The estimate replaces the provider's figure on the row too, so
            # what is billed and what is recorded stay the same number.
            usage_raw["output_tokens"] = output_tokens

        if self._cost_ledger is not None and (input_tokens or output_tokens):
            try:
                self._cost_ledger.record(
                    "observer_agent",
                    self._config.model,
                    CostUsage.from_raw(usage_raw),
                    tier=self._config.tier or "",
                )
            except RuntimeError:
                logger.exception("observer cost record failed")

        text = joined.strip()
        if not text or text.upper().rstrip(".!") == self._policy.silence.strip().upper():
            return "", output_tokens
        if _is_banned_observation(text, self._policy.banlist):
            logger.info("observer banlist hit — dropping %r", text[:80])
            return "", output_tokens
        return text, output_tokens


def _with_room(reading: ObserverReading, percent: int | None) -> ObserverReading:
    """`reading` with the runtime's own fullness on its nudge.

    Untouched where there is no nudge or nothing measured the room: an absent
    field claims nothing, a zero would claim the conversation was empty.
    """
    if reading.nudge is None or percent is None:
        return reading
    return replace(reading, nudge=replace(reading.nudge, context_percent=percent))


def _room_percent(room: Fullness | None) -> int | None:
    """The one rounding of a `Fullness`, so the sentence the observer is told
    and the figure on its nudge cannot differ by a point."""
    if room is None or room.trigger_tokens <= 0:
        return None
    return round(room.ratio * 100)


def _describe_room(room: Fullness | None, policy: ObserverPolicy) -> str:
    """How full the conversation was, for the observer's prompt.

    The observer is asked whether a boundary looks due and used to be told
    nothing about the room, which made it the one reader judging that question
    blind. It gets the same reading the agent gets, from the same publisher,
    so the two can never disagree about the number.

    One turn behind, and said so — `policy.room_measured` names it. The card
    owns the wording; this function only owns which of the card's two
    sentences applies and which figures fill it.
    """
    if room is None or room.trigger_tokens <= 0:
        return policy.room_unmeasured
    return policy.room_measured.format(
        percent=_room_percent(room),
        conversation_tokens=room.conversation_tokens,
        trigger_tokens=room.trigger_tokens,
    )


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
    roles_observed: tuple[str, ...],
) -> list[dict[str, Any]]:
    """Keep only text turns from `roles_observed`, tail-trimmed to context_turns."""
    plain: list[dict[str, Any]] = []
    for msg in history:
        role = msg.get("role")
        if role not in roles_observed:
            continue
        content = msg.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        plain.append({"role": role, "content": content})
    return plain[-context_turns:]


#: Held by whoever is changing the observation log, and by nothing else. The
#: purge runs on a worker thread while the writer runs on the loop, so a row
#: appended between the purge's read and its replace would be in neither file.
#: The approvals sweep answers the same race the same way, one layer up.
_LOG_LOCK = threading.Lock()


def _append_observation_log(
    *, mode: str, session_id: str, text: str, panes: list[str] | None = None,
) -> None:
    """Append one observation record to `tesseract/logs/observer/YYYY-MM-DD.jsonl`.

    `panes` names the consented terminal panes whose output was in the prompt
    this observation came out of. It is written so that revoking consent for
    one of them can find this row again: the text is the model's own prose and
    may quote the pane, and prose cannot be redacted after the fact without
    guessing which half mattered.

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
            "panes": list(panes or ()),
        }
        with _LOG_LOCK:
            with target.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001 — fail-open
        logger.warning("observer log write failed: %s", exc)


def _purge_pane_from_log(pane_id: str) -> int:
    """Rewrite the observation log without the rows this pane fed. Blocking.

    A row that names no panes at all is left where it is. It was written
    before the field existed and there is no way to tell whether it read this
    pane, another one, or none; deleting every one of them on any revoke would
    destroy unrelated evidence to answer a question nobody can answer. They
    age out on the tree's own window like everything else here.

    Bounded by that same window: the directory holds one file per day and the
    retention sweep keeps a fortnight of them, so this walks a handful of
    files however long the machine has been running.
    """
    removed = 0
    root = _observer_log_dir()
    if not root.is_dir():
        return 0
    with _LOG_LOCK:
        for path in sorted(root.glob("*.jsonl")):
            tmp = path.with_name(path.name + ".rewriting")
            try:
                kept: list[str] = []
                dropped_here = 0
                for raw in path.read_text(encoding="utf-8").splitlines():
                    if not raw.strip():
                        continue
                    try:
                        row = json.loads(raw)
                    except Exception:  # noqa: BLE001 — a torn line is not a record
                        kept.append(raw)
                        continue
                    if pane_id in (row.get("panes") or ()):
                        dropped_here += 1
                        continue
                    kept.append(raw)
                if not dropped_here:
                    continue
                # Written beside the file and moved onto it, so a reader never
                # sees a half-rewritten day. The suffix is deliberately not
                # `.jsonl`: the retention sweep ages this directory by globbing
                # that extension and reading the stem as a date, so a stray
                # `<day>.jsonl.tmp` would be a file nothing could ever remove.
                tmp.write_text(
                    "".join(f"{line}\n" for line in kept), encoding="utf-8",
                )
                tmp.replace(path)
                removed += dropped_here
            except Exception as exc:  # noqa: BLE001 — one unreadable day is not the others
                logger.warning("observer log purge failed for %s: %s", path.name, exc)
            finally:
                # Only reachable when the move did not happen, because a
                # successful `replace` leaves nothing at this name.
                tmp.unlink(missing_ok=True)
    if removed:
        logger.info(
            "observer: forgot %d observation(s) that read pane %s", removed, pane_id,
        )
    return removed


def _render_pty_lines(lines, policy: ObserverPolicy) -> str:
    if not lines:
        return policy.pty_empty
    return "\n".join(
        policy.pty_line.format(
            timestamp=line.get("timestamp", "?"),
            pane_id=line.get("pane_id", "?"),
            text=(line.get("text") or "").rstrip(),
        )
        for line in lines
    )

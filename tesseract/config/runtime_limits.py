"""Runtime infrastructure limit loaders — concurrency caps + watchdog bounds.

Relocated from ``tesseract/orchestrator/lifeline/time_context.py`` (prune wave
1, Batch 3): these are shared infra, not lifeline/identity data, so they read
``tesseract/config/runtime.yaml``. Raise-loudly semantics preserved verbatim —
no hardcoded infrastructure defaults per project rule.
"""

from __future__ import annotations

from pathlib import Path

import yaml


def default_runtime_config_path() -> Path:
    """Canonical path to ``tesseract/config/runtime.yaml``.

    Resolved via `config_dir()` (call-time) rather than the frozen
    `CONFIG_DIR` constant, so callers re-invoking this mid-process (most
    call sites do, per request/tool-call) honor a `TESSERACT_HOME` change
    without needing a fresh import.
    """
    from tesseract.paths import config_dir

    return config_dir() / "runtime.yaml"


def load_runtime_config(path: Path) -> dict:
    """Load runtime.yaml. Raises FileNotFoundError loudly on missing file
    (no default fallback — config is source of truth per project rule).
    """
    if not path.exists():
        raise FileNotFoundError(f"runtime config not found: {path}")
    with path.open("r", encoding="utf-8") as fh:
        loaded = yaml.safe_load(fh) or {}
    if not isinstance(loaded, dict):
        raise ValueError(f"runtime config must be a YAML mapping, got {type(loaded).__name__}")
    return loaded


def load_max_concurrent_synthetic_turns(path: Path) -> int:
    """Return `max_concurrent_synthetic_turns` from runtime.yaml.

    Raises loudly when the file or key is missing: no hardcoded defaults
    for infrastructure values. Values <=0 are rejected. A value
    of 1 reproduces the pre-WP serial behavior (useful kill-switch).
    """
    cfg = load_runtime_config(path)
    raw = cfg.get("max_concurrent_synthetic_turns")
    if raw is None:
        raise ValueError(
            f"runtime.yaml missing 'max_concurrent_synthetic_turns' at {path}",
        )
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"max_concurrent_synthetic_turns must be int, got {raw!r}",
        ) from exc
    if value < 1:
        raise ValueError(
            f"max_concurrent_synthetic_turns must be >=1, got {value}",
        )
    return value


def _load_positive_float(
    path: Path, key: str, *, allow_zero: bool = False
) -> float:
    """Shared reader for the seconds-and-timeouts knobs.

    Five of them read the same key the same way and raised the same three
    sentences, retyped each time. Raises loudly on a missing file or key: no
    hardcoded infrastructure defaults, per project rule. `allow_zero` is for a
    knob where zero means off rather than nonsense.
    """
    cfg = load_runtime_config(path)
    raw = cfg.get(key)
    if raw is None:
        raise ValueError(f"runtime.yaml missing {key!r} at {path}")
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be a number, got {raw!r}") from exc
    if allow_zero:
        if value < 0:
            raise ValueError(f"{key} must be >= 0, got {value}")
    elif value <= 0:
        raise ValueError(f"{key} must be > 0, got {value}")
    return value


def load_spawn_stall_seconds(path: Path) -> float:
    """Return `spawn_stall_seconds` from runtime.yaml — the halt-watchdog bound.

    Spawn push Stage 2B. A background spawn still `running` past this many
    seconds is flagged stalled and surfaced to the assistant via a one-shot
    `[spawn_stalled]` floor note. Generous on purpose: a hung *subprocess* is
    already killed by `cli_stream.race_communicate`'s own timeout, so this only
    catches the rarer task/record staleness.
    """
    return _load_positive_float(path, "spawn_stall_seconds")


def load_spawn_heartbeat_seconds(path: Path) -> float:
    """Return `spawn_heartbeat_seconds` from runtime.yaml.

    How long a background spawn may run before the heartbeat starts a turn to
    report it, and the cadence the report then backs off from. `0` disables the
    heartbeat, which is the rollback: nothing on the completion path depends on
    this key.
    """
    return _load_positive_float(path, "spawn_heartbeat_seconds", allow_zero=True)


def load_shutdown_drain_seconds(path: Path) -> float:
    """Return `shutdown_drain_seconds` from runtime.yaml.

    How long a shutdown waits for turns that are still running before it
    cancels them. Read at shutdown rather than at boot, like every other
    runtime limit, so an operator edit applies without a restart, which for
    this one is the only way it could ever apply at all.
    """
    return _load_positive_float(path, "shutdown_drain_seconds")


def load_turn_step_expected_within_s(path: Path) -> float:
    """Return `turn_step_expected_within_s` from runtime.yaml.

    How long an open turn may go without starting a new step before the panel
    draws it as working below what it promised rather than working. Read where
    the panel builds its band, not at boot, so an operator who finds the dial
    wrong while watching a slow turn sees the next reading change.
    """
    return _load_positive_float(path, "turn_step_expected_within_s")


def load_tool_result_window_share(path: Path) -> float:
    """Return `tool_result_window_share` from runtime.yaml.

    The largest share of a model's context window one tool result may take.
    Raises loudly on a missing key: this is the backstop that keeps a single
    oversized result from making every model in a chain unreachable, and a
    silent default would let it go missing without anyone noticing.
    """
    cfg = load_runtime_config(path)
    raw = cfg.get("tool_result_window_share")
    if raw is None:
        raise ValueError(
            f"runtime.yaml missing 'tool_result_window_share' at {path}"
        )
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"tool_result_window_share must be a number, got {raw!r}"
        ) from exc
    if not 0 < value <= 1:
        raise ValueError(
            f"tool_result_window_share must be above 0 and at most 1, got {value}"
        )
    return value


def load_spawn_heartbeat_interval_s(path: Path) -> float:
    """Return `spawn_heartbeat_interval_s` from runtime.yaml — how often the
    heartbeat looks. Separate from the threshold because a loop that slept for
    the threshold would report a spawn anywhere between one and two thresholds
    into its run.
    """
    return _load_positive_float(path, "spawn_heartbeat_interval_s")


def load_max_concurrent_spawns_per_session(path: Path) -> int:
    """Return `max_concurrent_spawns_per_session` from runtime.yaml.

    per-session cap on simultaneously-running background
    spawns (SpawnRegistry). Register attempts past the cap raise
    `SpawnCapExceeded`, which tools map to a "drain first" error result.
    Raises loudly when the file or key is missing (no hardcoded
    infrastructure defaults).
    """
    cfg = load_runtime_config(path)
    raw = cfg.get("max_concurrent_spawns_per_session")
    if raw is None:
        raise ValueError(
            f"runtime.yaml missing 'max_concurrent_spawns_per_session' at {path}",
        )
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"max_concurrent_spawns_per_session must be int, got {raw!r}",
        ) from exc
    if value < 1:
        raise ValueError(
            f"max_concurrent_spawns_per_session must be >=1, got {value}",
        )
    return value


def load_ask_park_timeout_s(path: Path) -> float:
    """Return `ask_park_timeout_s` from runtime.yaml — how long an unattended
    approval request stays parked in the queue before it is denied."""
    return _load_positive_float(path, "ask_park_timeout_s")


def load_max_foreground_delegate_timeout_s(path: Path) -> float:
    """Return `max_foreground_delegate_timeout_s` from runtime.yaml — the hard
    cap on how long a foreground delegation may block the chat turn. A request
    asking for longer is auto-flipped to a background spawn instead."""
    return _load_positive_float(path, "max_foreground_delegate_timeout_s")


def load_max_spawn_depth(path: Path) -> int:
    """Return `max_spawn_depth` from runtime.yaml.

    trio W3 — structural cap on spawn NESTING (root chat session = depth 0;
    each invoke_agent sub-session +1). A session at or past the cap may not
    register background spawns (`SpawnDepthExceeded` → "don't nest deeper"
    error). Raises loudly when the file or key is missing (no
    hardcoded infrastructure defaults).
    """
    cfg = load_runtime_config(path)
    raw = cfg.get("max_spawn_depth")
    if raw is None:
        raise ValueError(
            f"runtime.yaml missing 'max_spawn_depth' at {path}",
        )
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"max_spawn_depth must be int, got {raw!r}",
        ) from exc
    if value < 1:
        raise ValueError(
            f"max_spawn_depth must be >=1, got {value}",
        )
    return value


CONCURRENCY_KEY = "max_concurrent_chat_turns_per_provider"


def load_max_concurrent_chat_turns_per_provider(path: Path) -> int:
    """Return `max_concurrent_chat_turns_per_provider` from runtime.yaml.

    mirror-multi-chat P2 inc.C2 — bounds how many chat turns may stream
    concurrently against a single provider so parallel background chats can't
    collide on its rate limit. Raises loudly when the file or key is missing
    (no hardcoded infrastructure defaults). A value of 1 reproduces
    the pre-inc.C2 fully-serial behavior (useful kill-switch).
    """
    cfg = load_runtime_config(path)
    raw = cfg.get(CONCURRENCY_KEY)
    if raw is None:
        raise ValueError(
            f"runtime.yaml missing 'max_concurrent_chat_turns_per_provider' at {path}",
        )
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"max_concurrent_chat_turns_per_provider must be int, got {raw!r}",
        ) from exc
    if value < 1:
        raise ValueError(
            f"max_concurrent_chat_turns_per_provider must be >=1, got {value}",
        )
    return value


def load_agent_pending_cap(path: Path) -> int:
    """Return `agent_pending_cap` from runtime.yaml.

    Stage 10 — maximum number of agents that may sit in ``agents/pending/``
    before HEADLESS ``agent_create`` calls are refused (attended creates are
    ASK-gated and uncapped). Flood guard for unattended proposal loops.
    Raises loudly when the file or key is missing (no hardcoded
    infrastructure defaults).
    """
    cfg = load_runtime_config(path)
    raw = cfg.get("agent_pending_cap")
    if raw is None:
        raise ValueError(
            f"runtime.yaml missing 'agent_pending_cap' at {path}",
        )
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"agent_pending_cap must be int, got {raw!r}",
        ) from exc
    if value < 1:
        raise ValueError(
            f"agent_pending_cap must be >=1, got {value}",
        )
    return value


def load_skill_pending_cap(path: Path) -> int:
    """Return `skill_pending_cap` from runtime.yaml.

    Maximum number of skills that may sit in
    ``workspace/skills/pending/`` before HEADLESS ``skill_create`` calls are
    refused (attended drafts are ASK-gated and uncapped). Flood guard for
    unattended proposal loops, mirroring ``agent_pending_cap``. Raises loudly
    when the file or key is missing (no hardcoded infra defaults).
    """
    cfg = load_runtime_config(path)
    raw = cfg.get("skill_pending_cap")
    if raw is None:
        raise ValueError(
            f"runtime.yaml missing 'skill_pending_cap' at {path}",
        )
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"skill_pending_cap must be int, got {raw!r}",
        ) from exc
    if value < 1:
        raise ValueError(
            f"skill_pending_cap must be >=1, got {value}",
        )
    return value


def load_chat_queue_max(path: Path) -> int:
    """Return `chat_queue_max` from runtime.yaml.

    conversation-layer Task 4.1 — maximum number of chat turns that may sit
    queued (not yet dispatched) for one session before the queue is
    considered full. Raises loudly when the file or key is missing
    (no hardcoded infrastructure defaults).
    """
    cfg = load_runtime_config(path)
    raw = cfg.get("chat_queue_max")
    if raw is None:
        raise ValueError(
            f"runtime.yaml missing 'chat_queue_max' at {path}",
        )
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"chat_queue_max must be int, got {raw!r}",
        ) from exc
    if value < 1:
        raise ValueError(
            f"chat_queue_max must be >=1, got {value}",
        )
    return value


def load_agent_question_timeout_s(path: Path) -> float:
    """Return `agent_question_timeout_s` from runtime.yaml.

    Bounds how long a sub-agent parked on `agent_ask` waits for an answer.
    Raises loudly when the file or key is missing (no hardcoded
    infrastructure defaults).
    """
    cfg = load_runtime_config(path)
    raw = cfg.get("agent_question_timeout_s")
    if raw is None:
        raise ValueError(
            "runtime.yaml is missing `agent_question_timeout_s` — a parked "
            "sub-agent question needs a bound, and config is authoritative"
        )
    value = float(raw)
    if value <= 0:
        raise ValueError(
            f"runtime.yaml::agent_question_timeout_s must be positive, got {raw!r}"
        )
    return value


def _load_positive_int(path: Path, key: str, minimum: int) -> int:
    """Shared reader for the plain positive-int knobs. Raises loudly on a
    missing file or key — no hardcoded infrastructure defaults, per project
    rule."""
    cfg = load_runtime_config(path)
    raw = cfg.get(key)
    if raw is None:
        raise ValueError(f"runtime.yaml missing {key!r} at {path}")
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be int, got {raw!r}") from exc
    if value < minimum:
        raise ValueError(f"{key} must be >={minimum}, got {value}")
    return value


def _read_ceiling(path: Path, key: str) -> int:
    """A ceiling on how much is read off a wire or a pipe, in bytes.

    Refuses a value that is not ABOVE the longest a stored credential may be
    (`credentials/models.py::MAX_VALUE_CHARS`). Both consumers read up to this
    much and check the WHOLE of what they read for stored values, and the
    check is an exact match: a value longer than the ceiling would be cut in
    half before anything could find it, and the leading half would be
    reported. The cap on a value and the ceiling on a read are one guarantee
    held between two files, and the relationship is enforced here for the same
    reason the report ceiling's is, one function below.
    """
    from tesseract.credentials.models import MAX_VALUE_CHARS

    value = _load_positive_int(path, key, 1024)
    if value <= MAX_VALUE_CHARS:
        raise ValueError(
            f"{key} ({value}) must be above the {MAX_VALUE_CHARS} characters a "
            f"stored credential value may hold: what is read is checked for "
            f"stored values whole, and a value longer than the ceiling is cut "
            f"in half before an exact match can find it"
        )
    return value


def load_api_request_timeout_s(path: Path) -> float:
    """`api_request` — how long to wait for a service to answer, in seconds."""
    return _load_positive_float(path, "api_request_timeout_s")


def load_api_request_max_body_bytes(path: Path) -> int:
    """`api_request` — how much of a reply is read off the wire, in bytes."""
    return _read_ceiling(path, "api_request_max_body_bytes")


def load_api_request_report_chars(path: Path) -> int:
    """`api_request` — how much of a checked reply is reported, in characters.

    Refuses a value that is not below `api_request_max_body_bytes`. The report
    being the shorter of the two is what keeps a value split by the read
    ceiling out of the report, so a configuration that inverted them would
    quietly reopen the leak both numbers exist to close.
    """
    value = _load_positive_int(path, "api_request_report_chars", 200)
    ceiling = load_api_request_max_body_bytes(path)
    if value >= ceiling:
        raise ValueError(
            f"api_request_report_chars ({value}) must be below "
            f"api_request_max_body_bytes ({ceiling}): the reply is checked for "
            f"stored values before it is cut, and reporting as much as was "
            f"read would put the cut made at the read ceiling inside the "
            f"report, where an exact match can no longer find it"
        )
    return value


def load_command_run_timeout_s(path: Path) -> float:
    """`command_run` — how long a command holding an account may run."""
    return _load_positive_float(path, "command_run_timeout_s")


def load_command_run_max_output_bytes(path: Path) -> int:
    """`command_run` — how much of what a command prints is read, in bytes."""
    return _read_ceiling(path, "command_run_max_output_bytes")


def load_command_run_report_chars(path: Path) -> int:
    """`command_run` — how much of that output is reported, in characters.

    Refuses a value that is not below `command_run_max_output_bytes`, for the
    reason `load_api_request_report_chars` gives: the read ceiling is itself a
    cut, and only a shorter report keeps a value straddling it out of what is
    reported.
    """
    value = _load_positive_int(path, "command_run_report_chars", 200)
    ceiling = load_command_run_max_output_bytes(path)
    if value >= ceiling:
        raise ValueError(
            f"command_run_report_chars ({value}) must be below "
            f"command_run_max_output_bytes ({ceiling}): what a command printed "
            f"is checked for stored values before it is cut, and reporting as "
            f"much as was read would put the cut made at the read ceiling "
            f"inside the report, where an exact match can no longer find it"
        )
    return value


def load_screen_look_answer_chars(path: Path) -> int:
    """Return `screen_look_answer_chars` from runtime.yaml — how much of the
    vision model's answer comes back to the caller."""
    return _load_positive_int(path, "screen_look_answer_chars", 80)

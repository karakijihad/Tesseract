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


def load_spawn_stall_seconds(path: Path) -> float:
    """Return `spawn_stall_seconds` from runtime.yaml — the halt-watchdog bound.

    Spawn push Stage 2B. A background spawn still `running` past this many
    seconds is flagged stalled and surfaced to the assistant via a one-shot
    `[spawn_stalled]` floor note. Generous on purpose: a hung *subprocess* is
    already killed by `cli_stream.race_communicate`'s own timeout, so this only
    catches the rarer task/record staleness. Raises loudly when the file or key
    is missing (no hardcoded infrastructure defaults).
    """
    cfg = load_runtime_config(path)
    raw = cfg.get("spawn_stall_seconds")
    if raw is None:
        raise ValueError(
            f"runtime.yaml missing 'spawn_stall_seconds' at {path}",
        )
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"spawn_stall_seconds must be a number, got {raw!r}",
        ) from exc
    if value <= 0:
        raise ValueError(
            f"spawn_stall_seconds must be > 0, got {value}",
        )
    return value


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
    """Return `ask_park_timeout_s` from runtime.yaml.

    trio W4 — bound on how long a background spawn's unattended ASK stays
    parked (input_required) awaiting the operator before it finally denies.
    Raises loudly when the file or key is missing (no hardcoded
    infrastructure defaults).
    """
    cfg = load_runtime_config(path)
    raw = cfg.get("ask_park_timeout_s")
    if raw is None:
        raise ValueError(
            f"runtime.yaml missing 'ask_park_timeout_s' at {path}",
        )
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"ask_park_timeout_s must be a number, got {raw!r}",
        ) from exc
    if value <= 0:
        raise ValueError(
            f"ask_park_timeout_s must be > 0, got {value}",
        )
    return value


def load_max_foreground_delegate_timeout_s(path: Path) -> float:
    """Return `max_foreground_delegate_timeout_s` from runtime.yaml.

    Delegate visibility fix-pass (2026-07-10) — hard cap on how long a
    ``background: false`` delegate_* call may block the chat turn. Foreground
    requests with a larger ``timeout`` are auto-flipped to background spawns
    by ``_delegate_runner.run_delegate``. Raises loudly when the file or key
    is missing (no hardcoded infrastructure defaults).
    """
    cfg = load_runtime_config(path)
    raw = cfg.get("max_foreground_delegate_timeout_s")
    if raw is None:
        raise ValueError(
            f"runtime.yaml missing 'max_foreground_delegate_timeout_s' at {path}",
        )
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"max_foreground_delegate_timeout_s must be a number, got {raw!r}",
        ) from exc
    if value <= 0:
        raise ValueError(
            f"max_foreground_delegate_timeout_s must be > 0, got {value}",
        )
    return value


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


def load_max_concurrent_chat_turns_per_provider(path: Path) -> int:
    """Return `max_concurrent_chat_turns_per_provider` from runtime.yaml.

    mirror-multi-chat P2 inc.C2 — bounds how many chat turns may stream
    concurrently against a single provider so parallel background chats can't
    collide on its rate limit. Raises loudly when the file or key is missing
    (no hardcoded infrastructure defaults). A value of 1 reproduces
    the pre-inc.C2 fully-serial behavior (useful kill-switch).
    """
    cfg = load_runtime_config(path)
    raw = cfg.get("max_concurrent_chat_turns_per_provider")
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

    Phase 4 (capability-growth) — maximum number of skills that may sit in
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


def load_screen_look_answer_chars(path: Path) -> int:
    """Return `screen_look_answer_chars` from runtime.yaml — how much of the
    vision model's answer comes back to the caller."""
    return _load_positive_int(path, "screen_look_answer_chars", 80)

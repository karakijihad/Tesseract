"""Temporal helpers for TARS's system prompt — time-of-day bucketing, age
computation, identity-config loading, and the conscience drift snippet.

Split out of `tesseract/brain/prompt.py` (module-size cleanup, Task 7.5).
Pure/stateless helpers live here; `prompt.py` keeps `_build_now_section`
and its monkeypatch-sensitive globals (`_now_local`, `_IDENTITY_CONFIG_PATH`,
`_TEMPORAL_FALLBACK_WARNED`) because several tests patch those attributes
directly on `tesseract.brain.prompt` (see `tests/brain/test_prompt_temporal.py`,
`tests/fix_pass_survivability_SU_3_5/test_audit_subagents.py`) — moving them
here would silently break those patches (they'd patch a copy, not the name
`_build_now_section` actually reads).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Mapping

import yaml

# Logger name pinned to the historical "tesseract.brain.prompt" identity —
# `tests/fix_pass_2026_05_05/test_directives_section.py` and
# `tests/brain/test_prompt_temporal.py` assert on that exact logger name via
# `caplog.at_level(..., logger="tesseract.brain.prompt")`. Hardcoding (not
# `__name__`) keeps every split-out prompt module logging under the one name
# operators and tests already key on.
logger = logging.getLogger("tesseract.brain.prompt")

_DEFAULT_CONSCIENCE_DIR = Path(__file__).resolve().parent.parent / "logs" / "conscience"

_DEFAULT_TOD_BUCKETS: Mapping[str, tuple[str, str]] = {
    "morning":   ("05:00", "12:00"),
    "afternoon": ("12:00", "17:00"),
    "evening":   ("17:00", "21:00"),
    "night":     ("21:00", "05:00"),
}


def _parse_hhmm(value: str) -> time:
    try:
        hh, mm = value.split(":")
        return time(int(hh), int(mm))
    except (ValueError, TypeError) as e:
        raise ValueError(f"invalid HH:MM value: {value!r}") from e


def _time_of_day_bucket(now_local: datetime, *, buckets: Mapping[str, tuple[str, str]]) -> str:
    """Return the time-of-day label for `now_local`.

    `buckets` maps a label to (start_hhmm, end_hhmm). The first label whose
    range contains `now_local.time()` wins. Ranges where end <= start wrap
    midnight (e.g. ("21:00", "05:00")).
    """
    current = now_local.time()
    for label, (start_raw, end_raw) in buckets.items():
        start = _parse_hhmm(start_raw)
        end = _parse_hhmm(end_raw)
        if start <= end:
            if start <= current < end:
                return label
        else:
            if current >= start or current < end:
                return label
    raise ValueError(f"no bucket matched local time {current}")


def _compute_age_days(born_at_iso: str, *, now: datetime) -> int:
    """Days between `born_at_iso` and `now`. Both tz-aware. Floor division."""
    born = datetime.fromisoformat(born_at_iso)
    if born.tzinfo is None:
        raise ValueError(f"born_at must be timezone-aware: {born_at_iso}")
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return (now - born).days


def _load_identity_config(path: Path) -> dict:
    """Load identity.yaml. Raises loudly on missing file/bad shape (no
    default fallback — config is source of truth per project rule)."""
    if not path.exists():
        raise FileNotFoundError(f"identity config not found: {path}")
    with path.open("r", encoding="utf-8") as fh:
        loaded = yaml.safe_load(fh) or {}
    if not isinstance(loaded, dict):
        raise ValueError(f"identity config must be a YAML mapping, got {type(loaded).__name__}")
    return loaded


def _drift_snippet(conscience_dir: Path | None = None) -> str:
    """Return a one-line drift summary when non-ok, else "".

    Reads the latest `drift-*.jsonl` line from `tesseract/logs/conscience/`.
    Returns empty string when healthy, when no report exists, or on any
    parse error — this is *informational* for the prompt, never critical.
    """
    base = conscience_dir or _DEFAULT_CONSCIENCE_DIR
    if not base.exists():
        return ""
    files = sorted(base.glob("drift-*.jsonl"))
    if not files:
        return ""
    report = _load_last_line(files[-1])
    if report is None:
        return ""
    summary = report.get("summary") or {}
    warn = int(summary.get("warn", 0))
    bad = int(summary.get("bad", 0))
    if warn == 0 and bad == 0:
        return ""  # Healthy — no prompt bloat.
    worst = "bad" if bad else "warn"
    flagged = [
        s.get("name", "?") for s in (report.get("signals") or [])
        if s.get("status") in ("warn", "bad")
    ]
    age = _age_from_iso(report.get("timestamp") or "")
    names = ", ".join(flagged) if flagged else "unknown"
    return (
        f"- Drift: {worst} — {summary.get('ok', 0)} ok / {warn} warn / {bad} bad. "
        f"Flagged: {names}. (scraped {age}; call conscience_status for detail)"
    )


def _load_last_line(path: Path) -> dict | None:
    try:
        last: dict | None = None
        with path.open("r", encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    last = json.loads(raw)
                except json.JSONDecodeError:
                    continue
        return last
    except OSError:
        return None


def _age_from_iso(iso: str) -> str:
    if not iso:
        return "unknown"
    try:
        t = datetime.fromisoformat(iso).astimezone(timezone.utc)
    except ValueError:
        return "unknown"
    delta = (datetime.now(timezone.utc) - t).total_seconds()
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"

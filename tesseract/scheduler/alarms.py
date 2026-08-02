from __future__ import annotations

import dataclasses
import importlib
import logging
import secrets
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Literal

import yaml

from tesseract.lib.yaml_io import atomic_write_text
from tesseract.paths import home_dir, runtime_dir
from tesseract.scheduler.base_job import BaseJob
from tesseract.scheduler.log import append_run_log
from tesseract.scheduler.types import JobContext, JobResult

log = logging.getLogger(__name__)

RecurrenceKind = Literal["daily", "weekdays", "weekly", "every"]

RECENT_FIRED_MAX = 16
SNOOZE_OPTIONS = ["5m", "10m", "30m", "1h"]


def _legacy_state_file() -> Path:
    """Legacy alarms state, anchored at the state root — not at `__file__`.

    The old anchor pointed inside the code tree, so a packaged install grew a
    `scheduler/` directory full of Python source under its state root and the
    data-sync repo started tracking `engine.py`. Resolving under
    `TESSERACT_HOME` at call time also means test isolation follows from the
    isolated home rather than from a per-directory monkeypatch.
    """
    return home_dir() / "scheduler" / "alarms.yaml"


def _legacy_state_candidates() -> tuple[Path, ...]:
    """Every place a pre-relocation alarms file can still be sitting.

    The historic anchor was `Path(__file__)/state/alarms.yaml`, inside the code
    tree. Re-anchoring alone would orphan anything stranded there — a file
    written by a pre-relocation build would become permanently invisible to the
    migration — so that path stays in the list as a read-only probe. Safe to
    probe from any process: migration copies and leaves the original in place,
    and never writes to a code-tree path.
    """
    return (
        _legacy_state_file(),
        Path(__file__).resolve().parent / "state" / "alarms.yaml",
        # Last: the migration quarantines the leaked `scheduler/` tree under
        # `runtime/`, so a pre-relocation alarms file can be sitting there too.
        # Probed after the historic anchors, which are the older writes.
        runtime_dir() / "scheduler" / "alarms.yaml",
    )


def alarms_state_path() -> Path:
    """Resolve the alarm-state file under `<TESSERACT_HOME>/runtime`,
    call-time (never captured at import), matching the idiom in
    `outbound.py::outbound_rates_path` / `workspace_changes.py::
    workspace_events_dir` — an app update that replaces the code tree
    must not touch it.

    Pure path resolution, no I/O side effects: this is called from
    `brain/boot.py::build_tool_registry`, which the unit test suite
    exercises constantly, so it must be safe to call unconditionally.
    See `ensure_alarms_state_migrated` for the (deliberately separate)
    legacy-data migration.
    """
    return runtime_dir() / "alarms.yaml"


def ensure_alarms_state_migrated() -> None:
    """One-time relocation of any pre-Phase-1 alarm state found at one of
    `_legacy_state_candidates()` into `alarms_state_path()`, so upgrading
    operators don't silently lose queued alarms.

    Deliberately NOT folded into `alarms_state_path()`: that resolver is
    reached via `build_tool_registry()`, which ordinary unit tests call
    constantly, and it must stay pure path resolution with no file I/O.
    Call this only from the real entry points (`mirror/server/
    __main__.py`, `supervisor/__main__.py`, `scripts/tars_controller.py`),
    the same call site as `config_seed.py`'s `ensure_*_seeded()` —
    explicit call only, matching that module's "no import-time or
    incidental file I/O" contract.

    Idempotent: a no-op the instant `alarms_state_path()` exists, so this
    never overwrites live post-migration data with stale legacy data, no
    matter how many times it runs. Copies then reads the copy back to
    verify a byte-for-byte match before declaring success.

    The legacy file is deliberately LEFT IN PLACE rather than deleted:
    an earlier version of this function deleted it after a verified copy,
    reasoning that a stale file risked resurrecting old alarms — but nothing
    ever reads the legacy path again once the new path has data (every
    future call short-circuits at the `exists()` check below), so there is
    nothing to resurrect, and deleting is irreversible. Every migration is
    logged — a silent migration would be nearly as bad as a silent loss,
    since nobody could confirm it worked.
    """
    new_path = alarms_state_path()
    if new_path.exists():
        return
    legacy = next((path for path in _legacy_state_candidates() if path.exists()), None)
    if legacy is None:
        return
    try:
        legacy_text = legacy.read_text(encoding="utf-8")
        atomic_write_text(new_path, legacy_text, prefix=".alarms-migrate-")
        if new_path.read_text(encoding="utf-8") != legacy_text:
            raise ValueError("post-copy verification mismatch")
    except Exception:
        log.exception("alarm migration: failed to relocate legacy state %s -> %s", legacy, new_path)
        return
    log.warning(
        "alarm migration: relocated queued alarms %s -> %s (legacy file left in place)",
        legacy, new_path,
    )


@dataclass(frozen=True)
class RecurrenceRule:
    kind: RecurrenceKind
    weekday: int | None = None          # 0=mon … 6=sun, for kind="weekly"
    interval_seconds: int | None = None  # for kind="every"

    def next_occurrence(self, after: datetime) -> datetime:
        if self.kind == "daily":
            return after + timedelta(days=1)
        if self.kind == "weekdays":
            step = 1
            while True:
                candidate = after + timedelta(days=step)
                if candidate.weekday() < 5:
                    return candidate
                step += 1
        if self.kind == "weekly":
            if self.weekday is None:
                raise ValueError("weekly recurrence requires weekday")
            days_ahead = (self.weekday - after.weekday()) % 7
            if days_ahead == 0:
                days_ahead = 7
            return after + timedelta(days=days_ahead)
        if self.kind == "every":
            if not self.interval_seconds:
                raise ValueError("every recurrence requires interval_seconds")
            return after + timedelta(seconds=self.interval_seconds)
        raise ValueError(f"unknown recurrence kind: {self.kind!r}")

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": self.kind}
        if self.weekday is not None:
            out["weekday"] = self.weekday
        if self.interval_seconds is not None:
            out["interval_seconds"] = self.interval_seconds
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RecurrenceRule":
        return cls(
            kind=data["kind"],
            weekday=data.get("weekday"),
            interval_seconds=data.get("interval_seconds"),
        )


@dataclass
class PendingAlarm:
    id: str
    label: str
    run_at: datetime
    handler_dotpath: str
    message: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    recurrence: RecurrenceRule | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    fired: bool = False

    @property
    def name(self) -> str:
        """Back-compat alias — the S4 shape used `name` where v2 uses `label`."""
        return self.label

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "run_at": self.run_at.isoformat(),
            "handler_dotpath": self.handler_dotpath,
            "message": self.message,
            "payload": dict(self.payload),
            "recurrence": self.recurrence.to_dict() if self.recurrence else None,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PendingAlarm":
        rec = data.get("recurrence")
        return cls(
            id=data["id"],
            label=data["label"],
            run_at=_parse_iso(data["run_at"]),
            handler_dotpath=data["handler_dotpath"],
            message=data.get("message", ""),
            payload=dict(data.get("payload") or {}),
            recurrence=RecurrenceRule.from_dict(rec) if rec else None,
            created_at=_parse_iso(data.get("created_at")) if data.get("created_at") else datetime.now(timezone.utc),
        )


@dataclass
class FiredAlarm:
    """Lightweight record of a just-fired alarm for 'snooze the last one' flows."""
    id: str
    label: str
    message: str
    fired_at: datetime
    was_recurring: bool


class AlarmRegistry:
    def __init__(
        self,
        log_dir: Path | None = None,
        state_file: Path | None = None,
    ) -> None:
        """`state_file` is opt-in persistence; None = pure in-memory (tests,
        REPL without Mirror). `brain/boot.py::build_tool_registry` passes
        `alarms_state_path()` so queued alarms survive restart."""
        self._alarms: list[PendingAlarm] = []
        self._log_dir = log_dir
        self._state_file = state_file
        self.recently_fired: deque[FiredAlarm] = deque(maxlen=RECENT_FIRED_MAX)
        self._load()

    # --- Mutation API --------------------------------------------------------

    def add(
        self,
        label: str,
        run_at: datetime,
        handler_dotpath: str,
        payload: dict[str, Any] | None = None,
        *,
        message: str = "",
        recurrence: RecurrenceRule | None = None,
    ) -> PendingAlarm:
        for existing in self._alarms:
            if not existing.fired and existing.label == label:
                raise ValueError(f"alarm label already pending: {label!r}")
        alarm = PendingAlarm(
            id=_new_id(),
            label=label,
            run_at=run_at,
            handler_dotpath=handler_dotpath,
            message=message,
            payload=dict(payload or {}),
            recurrence=recurrence,
        )
        self._alarms.append(alarm)
        self._persist()
        return alarm

    def cancel(self, handle: str) -> PendingAlarm | None:
        """Remove an alarm entirely (one-shot or recurring). Returns the alarm
        that was removed, or None if no match."""
        alarm = self.resolve(handle)
        if alarm is None:
            return None
        alarm.fired = True
        self._alarms = [a for a in self._alarms if a is not alarm]
        self._persist()
        return alarm

    def snooze(self, handle: str, new_run_at: datetime) -> PendingAlarm | None:
        """Reschedule an alarm's next fire. For recurring alarms, only the
        upcoming fire shifts — the cycle continues from the rule, not the
        snooze time."""
        alarm = self.resolve(handle)
        if alarm is None:
            return None
        alarm.run_at = new_run_at
        alarm.fired = False
        self._persist()
        return alarm

    def resolve(self, handle: str) -> PendingAlarm | None:
        """Resolve a handle (label or id-prefix) to a live alarm. Returns None
        if there is no match or the match is ambiguous."""
        pending = [a for a in self._alarms if not a.fired]
        by_label = [a for a in pending if a.label == handle]
        if len(by_label) == 1:
            return by_label[0]
        if len(by_label) > 1:
            return None
        by_id = [a for a in pending if a.id.startswith(handle)]
        if len(by_id) == 1:
            return by_id[0]
        return None

    def suggestions(self, handle: str, limit: int = 5) -> list[str]:
        """Return a small list of labels/ids that could match `handle` for
        user-facing error messages."""
        pending = [a for a in self._alarms if not a.fired]
        hits: list[str] = []
        for a in pending:
            if handle in a.label or a.id.startswith(handle):
                hits.append(f"{a.label}({a.id[:8]})")
        return hits[:limit]

    def list_pending(self) -> list[PendingAlarm]:
        return [a for a in self._alarms if not a.fired]

    # --- Persistence ---------------------------------------------------------

    def _persist(self) -> None:
        if self._state_file is None:
            return
        try:
            data = {"alarms": [a.to_dict() for a in self._alarms if not a.fired]}
            atomic_write_text(
                self._state_file,
                yaml.safe_dump(data, sort_keys=False),
                prefix=".alarms-",
            )
        except Exception:
            log.exception("alarm persist failed (%s)", self._state_file)

    def _load(self) -> None:
        if self._state_file is None or not self._state_file.exists():
            return
        try:
            raw = yaml.safe_load(self._state_file.read_text(encoding="utf-8")) or {}
        except Exception:
            log.exception("alarm load failed (%s) — starting empty", self._state_file)
            return
        entries = raw.get("alarms") or []
        for entry in entries:
            try:
                self._alarms.append(PendingAlarm.from_dict(entry))
            except Exception:
                log.exception("alarm load: skipping malformed entry %r", entry)

    # --- Fire path -----------------------------------------------------------

    async def tick(self, app: Any, now: datetime) -> None:
        for alarm in list(self._alarms):
            if alarm.fired or alarm.run_at > now:
                continue
            was_recurring = alarm.recurrence is not None
            if was_recurring:
                next_run = alarm.recurrence.next_occurrence(alarm.run_at)
                # Skip any recurrence slots we've already passed (process was
                # offline longer than one cycle): fast-forward to the next
                # future occurrence to avoid firing a burst.
                while next_run <= now:
                    next_run = alarm.recurrence.next_occurrence(next_run)
                previous_run = alarm.run_at
                alarm.run_at = next_run
                try:
                    await self._fire(app, alarm, previous_run)
                except Exception:
                    log.exception("alarm tick: handler %s raised", alarm.label)
                self._record_fired(alarm, previous_run, was_recurring=True)
                self._persist()
            else:
                alarm.fired = True  # mark before dispatch so a crashing handler can't retry
                try:
                    await self._fire(app, alarm, now)
                except Exception:
                    log.exception("alarm tick: handler %s raised", alarm.label)
                self._record_fired(alarm, now, was_recurring=False)
                self._alarms = [a for a in self._alarms if a is not alarm]
                self._persist()

    def _record_fired(self, alarm: PendingAlarm, fired_at: datetime, *, was_recurring: bool) -> None:
        self.recently_fired.append(FiredAlarm(
            id=alarm.id,
            label=alarm.label,
            message=alarm.message,
            fired_at=fired_at,
            was_recurring=was_recurring,
        ))

    async def _fire(self, app: Any, alarm: PendingAlarm, now: datetime) -> None:
        handler_cls = self._resolve_handler(alarm.handler_dotpath)
        ctx_payload = {
            **alarm.payload,
            "alarm_id": alarm.id,
            "alarm_name": alarm.label,  # back-compat key
            "alarm_label": alarm.label,
            "message": alarm.message,
            "recurring": alarm.recurrence is not None,
            "snooze_options": list(SNOOZE_OPTIONS),
        }
        ctx = JobContext(
            job_name=f"alarm:{alarm.label}",
            fired_at=now,
            app=app,
            config=ctx_payload,
        )
        t0 = time.perf_counter()
        try:
            result = await handler_cls().run(ctx)
        except Exception as exc:
            result = JobResult(
                job_name=ctx.job_name,
                run_id=ctx.run_id,
                ok=False,
                detail=f"unhandled exception: {exc!r}",
                duration_ms=(time.perf_counter() - t0) * 1000.0,
            )
            log.exception("alarm %s raised", alarm.label)
        if result.duration_ms == 0.0:
            result = dataclasses.replace(result, duration_ms=(time.perf_counter() - t0) * 1000.0)
        append_run_log(ctx, result, log_dir=self._log_dir)

    @staticmethod
    def _resolve_handler(dotted: str) -> type[BaseJob]:
        module_path, _, cls_name = dotted.rpartition(".")
        if not module_path:
            raise ValueError(f"handler path must be dotted: {dotted!r}")
        module = importlib.import_module(module_path)
        cls = getattr(module, cls_name)
        if not isinstance(cls, type) or not issubclass(cls, BaseJob):
            raise TypeError(f"{dotted} must subclass BaseJob")
        return cls


# --- Helpers -----------------------------------------------------------------


def _new_id() -> str:
    return secrets.token_hex(4)  # 8 hex chars


def _parse_iso(text: str) -> datetime:
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00") if text.endswith("Z") else text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)

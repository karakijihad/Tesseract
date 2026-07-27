"""X-5 Session A — persistent named lanes (tmux Agent Teams pattern).

A `NamedLane` is a stable label (`coder/claude`, `auditor/codex`) that
resolves to a current `lane_id`. The binding is persisted at
``<TESSERACT_HOME>/controller/named-lanes/<sanitized>.json`` so a fresh
`NamedLaneManager` (post brain restart) re-discovers the bindings from
disk and re-attaches the underlying lane via `LaneManager.attach`.

Two-layer model:

* `LaneManager` owns the lane's process + on-disk record (X-4 ground).
* `NamedLaneManager` owns the *name → lane_id* binding (this module).

Decoupling means a stale binding (lane was closed externally) doesn't
crash `ensure`; the manager detects the dead lane and opens a fresh
one under the same name.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from tesseract.orchestrator.activity.hooks import register_lane

from ._common import home_root, utc_now_iso
from .models import LaneKind, LaneMode

if TYPE_CHECKING:
    from .manager import LaneManager


log = logging.getLogger(__name__)

_NAME_PATTERN = re.compile(r"^[a-z0-9_\-]+(/[a-z0-9_\-]+)?$")


class NamedLaneError(Exception):
    """Base for named-lane errors so callers can catch with one type."""


class InvalidNamedLaneNameError(NamedLaneError):
    """Raised when a name fails the `_NAME_PATTERN` check."""


class NamedLaneRecord(BaseModel):
    """The persisted `<name>.json` shape.

    ``extra="ignore"`` upholds the X-4 substrate guarantee — newer
    Session-B writers may add fields (e.g. routing hints) without
    breaking a Session-A reader."""

    model_config = ConfigDict(extra="ignore")

    name: str
    lane_id: str
    kind: LaneKind
    mode: LaneMode = "headless"
    model: str
    working_dir: str
    created_at_utc: str = Field(default_factory=utc_now_iso)
    last_bound_at_utc: str = Field(default_factory=utc_now_iso)


def named_lanes_root() -> Path:
    return home_root() / "controller" / "named-lanes"


def _validate_name(name: str) -> None:
    if not _NAME_PATTERN.match(name):
        raise InvalidNamedLaneNameError(
            f"invalid named-lane name {name!r}: must match "
            "[a-z0-9_-]+(/[a-z0-9_-]+)? (e.g. 'coder/claude')"
        )


def _name_to_filename(name: str) -> str:
    _validate_name(name)
    return name.replace("/", "__") + ".json"


def _record_path(name: str) -> Path:
    return named_lanes_root() / _name_to_filename(name)


def write_named_lane(record: NamedLaneRecord) -> Path:
    """Atomic write via tmp + os.replace, mirroring `store.write_lane`."""
    _validate_name(record.name)
    root = named_lanes_root()
    root.mkdir(parents=True, exist_ok=True)
    final = _record_path(record.name)
    tmp = final.with_suffix(".json.tmp")
    tmp.write_text(record.model_dump_json(indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, final)
    return final


def read_named_lane(name: str) -> NamedLaneRecord | None:
    path = _record_path(name)
    if not path.exists():
        return None
    return NamedLaneRecord.model_validate_json(path.read_text(encoding="utf-8"))


def list_named_lanes() -> list[NamedLaneRecord]:
    """Every `*.json` directly under `named_lanes_root()`. Orphans (file
    parses but underlying lane is gone) are still listed — the binding
    exists; `ensure` is the place to reconcile."""
    root = named_lanes_root()
    if not root.exists():
        return []
    out: list[NamedLaneRecord] = []
    for entry in sorted(root.iterdir()):
        if entry.suffix != ".json" or entry.name.endswith(".json.tmp"):
            continue
        try:
            out.append(
                NamedLaneRecord.model_validate_json(
                    entry.read_text(encoding="utf-8")
                )
            )
        except Exception:  # noqa: BLE001 — corrupt record skipped, not fatal
            continue
    return out


def delete_named_lane(name: str) -> bool:
    """Drop the binding only. The underlying lane (LaneManager-owned) is
    untouched. Returns True if a record existed, False if absent."""
    path = _record_path(name)
    if not path.exists():
        return False
    path.unlink()
    return True


class NamedLaneManager:
    """Thin owner of name → lane_id bindings on top of a `LaneManager`.

    The two managers compose: this one persists the binding,
    `LaneManager` owns the lane lifecycle. `ensure` is the load-bearing
    method — idempotent open-or-reuse with stale-binding repair."""

    def __init__(self, *, lane_manager: "LaneManager") -> None:
        self._lanes = lane_manager
        # Per-name lock so two concurrent `ensure` calls for the same
        # name don't both spawn a fresh lane (which would leave the
        # first lane orphaned — alive in the LaneManager but with no
        # binding pointing at it).
        self._name_locks: dict[str, asyncio.Lock] = {}

    @property
    def lane_manager(self) -> "LaneManager":
        return self._lanes

    def get(self, name: str) -> NamedLaneRecord | None:
        """Read the current binding from disk. Returns ``None`` when no
        binding exists — caller decides whether to `ensure`."""
        _validate_name(name)
        return read_named_lane(name)

    def list(self) -> list[NamedLaneRecord]:
        return list_named_lanes()

    def release(self, name: str) -> bool:
        """Drop the binding without closing the lane. Returns whether a
        binding existed prior."""
        return delete_named_lane(name)

    async def ensure(
        self,
        name: str,
        *,
        kind: LaneKind,
        model: str,
        working_dir: str,
        mode: LaneMode = "headless",
        env: dict[str, str] | None = None,
    ) -> NamedLaneRecord:
        """Return a live binding for `name`. Reuses an existing alive
        lane; opens a new one when no record exists OR the recorded
        lane is dead (lifecycle `closed`/`error` or its `lane.json`
        missing from disk).

        On a `kind` mismatch the call raises rather than silently
        re-opening — operators must `release` first to swap kinds; the
        guard avoids accidentally pointing `coder/claude` at a Codex
        lane mid-flight."""
        _validate_name(name)
        lock = self._name_locks.setdefault(name, asyncio.Lock())
        async with lock:
            existing = read_named_lane(name)
            if existing is not None:
                if existing.kind != kind:
                    raise NamedLaneError(
                        f"named lane {name!r} is bound to kind={existing.kind}; "
                        f"requested kind={kind}. Release the binding first."
                    )
                if self._is_lane_alive(existing.lane_id):
                    # Root fix (Deferred 2026-07-12): a reused disk-alive
                    # binding was never attached into this process's runtime
                    # cache after a restart, so the first send/turn failed
                    # "not attached" until a tool-side self-heal kicked in.
                    # Attach best-effort, only when the cache lacks the lane
                    # (attach replays events.jsonl from 0 — skip when warm).
                    runtimes = getattr(self._lanes, "_runtimes", None)
                    if runtimes is not None and existing.lane_id not in runtimes:
                        try:
                            await self._lanes.attach(existing.lane_id)
                        except Exception:  # noqa: BLE001
                            log.warning(
                                "named ensure %s: attach of reused lane %s failed",
                                name,
                                existing.lane_id,
                                exc_info=True,
                            )
                    # Refresh the timestamp so operators can see recent activity
                    # against the binding even when the underlying lane id is
                    # unchanged.
                    bumped = existing.model_copy(
                        update={"last_bound_at_utc": utc_now_iso()}
                    )
                    write_named_lane(bumped)
                    # AS-1 — upsert the activity record under the human name so
                    # a reused binding is reflected even when no open() ran
                    # this process (e.g. after a controller restart).
                    register_lane(
                        bumped.lane_id, label=bumped.name, provider=bumped.kind
                    )
                    return bumped

            if existing is not None:
                # The binding is moving to a fresh lane. Close + archive the
                # replaced lane: an un-archived headless lane dir stays
                # sendable forever (spawn-per-turn) and the boot rebuild
                # resurrects it into the activity map — ghost coder/claude
                # rows + dead-lane turns observed 2026-07-03. Best-effort:
                # a failed close must not block the replacement lane.
                try:
                    await self._lanes.close(
                        existing.lane_id, "replaced by named ensure"
                    )
                except Exception:  # noqa: BLE001
                    log.warning(
                        "named ensure %s: closing replaced lane %s failed",
                        name,
                        existing.lane_id,
                        exc_info=True,
                    )
            lane_id = await self._lanes.open(
                kind=kind,
                mode=mode,
                model=model,
                working_dir=working_dir,
                env=env,
            )
            record = NamedLaneRecord(
                name=name,
                lane_id=lane_id,
                kind=kind,
                mode=mode,
                model=model,
                working_dir=working_dir,
            )
            write_named_lane(record)
            # AS-1 — upsert with the human name over the bare label that
            # LaneManager.open just registered for this fresh lane.
            register_lane(record.lane_id, label=record.name, provider=record.kind)
            return record

    def _is_lane_alive(self, lane_id: str) -> bool:
        """Read the on-disk `lane.json` lifecycle — file-canonical truth.

        `LaneManager.status` would report `alive=False` for any lane
        not in its in-memory `_runtimes` cache, but after a brain
        restart that cache is empty even though the lane's underlying
        process (controller-owned) is still healthy. The persisted
        lifecycle (`ready`/`busy`/`idle` vs `closed`/`error`) is the
        load-bearing signal per P-3."""
        from .store import read_lane

        try:
            lane = read_lane(lane_id)
        except FileNotFoundError:
            return False
        except Exception:  # noqa: BLE001 — corrupt record treated as dead
            return False
        return lane.lifecycle not in ("closed", "error")


__all__ = [
    "InvalidNamedLaneNameError",
    "NamedLaneError",
    "NamedLaneManager",
    "NamedLaneRecord",
    "delete_named_lane",
    "list_named_lanes",
    "named_lanes_root",
    "read_named_lane",
    "write_named_lane",
]

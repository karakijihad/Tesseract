"""Where each kind of message the runtime sends on its own is delivered.

One table, ``kind -> [channel, ...]``. The shipped table is
``config/routing.yaml`` in the app tree; the operator's own file, if they have
one, names only the kinds they changed and is laid over the top.

Six properties, and every change to this module has to hold all of them at
once:

1. **One table.** A kind's destinations are read here and nowhere else. The
   split this replaces was `channels.yaml::telegram.brief_push` deciding the
   brief while a default argument decided everything else, which is how a
   destination came to be chosen in two places that could not disagree out
   loud.
2. **The shipped table is read, never copied over theirs.** Their file states
   only what they changed, so a kind added in a release reaches an install
   that already has a file, and a default corrected in a release reaches every
   kind they never touched.
3. **Two channels, one, or none.** An empty list means it does not leave this
   machine. It still appears in the app: every kind reaches the panel through
   the thing that produced it, never through this table, so nothing here can
   take a message off the screen.
4. **An unknown kind is refused, loudly.** A misspelt row that silently routes
   nothing is the exact failure this table exists to end. The shipped file must
   also name every kind, so a lookup can never miss.
5. **An unknown CHANNEL is not refused.** Whether a name has an adapter behind
   it is answered when something is sent, so writing a channel into the table
   before its adapter exists is a logged warning rather than a boot failure.
6. **This decides WHERE, never WHETHER.** Muting a kind and capping how often a
   channel will take one stay in `channels.yaml`, and a safety-critical kind
   still bypasses both inside a channel the operator did pick.

A broken operator file degrades to the shipped table rather than silencing the
runtime. A broken SHIPPED file raises: that is an install that cannot deliver
anything and should say so at once.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import yaml

from tesseract import paths
from tesseract.orchestrator.autonomy.outbound import CATEGORIES

log = logging.getLogger(__name__)

ROUTING_FILENAME = "routing.yaml"

#: Every kind the runtime sends on its own. The notification categories plus
#: the daily brief, which had its own switch until this table took it in.
ROUTABLE_KINDS: tuple[str, ...] = (*CATEGORIES, "daily_brief")


@dataclass(frozen=True)
class OutboundRouting:
    """The merged table. Every kind in :data:`ROUTABLE_KINDS` has a row."""

    routes: Mapping[str, tuple[str, ...]]

    def destinations(self, kind: str) -> tuple[str, ...]:
        return tuple(self.routes.get(kind, ()))

    def goes_to(self, kind: str, channel: str) -> bool:
        return channel in self.destinations(kind)


def system_routing_path() -> Path:
    return paths.system_config_dir() / ROUTING_FILENAME


def user_routing_path() -> Path:
    return paths.config_dir() / ROUTING_FILENAME


def _read_routes(path: Path) -> dict[str, tuple[str, ...]]:
    """The ``routes:`` block of one file, validated.

    Raises ``ValueError`` with the file named, so both callers can decide for
    themselves whether that is fatal.
    """
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"{path}: could not be read ({exc})") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: the file must be a mapping")
    block = raw.get("routes") or {}
    if not isinstance(block, dict):
        raise ValueError(f"{path}: `routes` must be a mapping of kind to channels")

    out: dict[str, tuple[str, ...]] = {}
    for kind, value in block.items():
        if kind not in ROUTABLE_KINDS:
            raise ValueError(
                f"{path}: `{kind}` is not something the runtime sends. "
                f"What it sends is: {', '.join(sorted(ROUTABLE_KINDS))}"
            )
        if value is None:
            value = []
        if not isinstance(value, (list, tuple)):
            raise ValueError(
                f"{path}: `{kind}` must be a list of channel names, or an "
                "empty list to keep it off every channel"
            )
        names: list[str] = []
        for entry in value:
            name = str(entry).strip()
            if not name:
                continue
            if name not in names:
                names.append(name)
        out[kind] = tuple(names)
    return out


def load_outbound_routing() -> OutboundRouting:
    """The shipped table with the operator's rows laid over it.

    In a dev checkout the two paths are the same file and it is read once.
    """
    shipped_path = system_routing_path()
    if not shipped_path.exists():
        raise RuntimeError(
            f"{ROUTING_FILENAME} is missing from the app tree ({shipped_path}). "
            "Nothing the runtime sends has anywhere to go."
        )
    try:
        merged = _read_routes(shipped_path)
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc

    missing = [kind for kind in ROUTABLE_KINDS if kind not in merged]
    if missing:
        raise RuntimeError(
            f"{shipped_path}: no row for {', '.join(sorted(missing))}. Every "
            "kind the runtime sends has to say where it goes, even if the "
            "answer is nowhere."
        )

    user_path = user_routing_path()
    try:
        same_file = user_path.resolve() == shipped_path.resolve()
    except OSError:
        same_file = user_path == shipped_path
    if same_file or not user_path.exists():
        return OutboundRouting(routes=merged)

    try:
        merged.update(_read_routes(user_path))
    except ValueError:
        # Theirs is unreadable. Falling back to the shipped table keeps the
        # runtime able to reach them, which is the whole point of the file
        # they just broke; the config watcher is what tells them about it.
        log.exception("routing: %s could not be used; the shipped table stands", user_path)
    return OutboundRouting(routes=merged)


def destinations_for(kind: str) -> tuple[str, ...]:
    """Convenience read for a caller that wants one kind and holds no table."""
    return load_outbound_routing().destinations(kind)


def routing_summary(routing: OutboundRouting) -> list[str]:
    """``kind -> channels`` lines, for the reload toast and the log."""
    lines: list[str] = []
    for kind in ROUTABLE_KINDS:
        targets = routing.destinations(kind)
        lines.append(f"{kind}: {', '.join(targets) if targets else 'nowhere'}")
    return lines


__all__ = [
    "OutboundRouting",
    "ROUTABLE_KINDS",
    "ROUTING_FILENAME",
    "destinations_for",
    "load_outbound_routing",
    "routing_summary",
    "system_routing_path",
    "user_routing_path",
]

"""The two numbers the conversations rail reads and writes.

`chats_at_once` bounds how many turns stream against one provider
(`runtime.yaml`); `archive_after_days` is how long a quiet conversation stays
in history (`retention.yaml`). Both write the file the runtime already reads,
so there is no second copy to keep in step.
"""

from __future__ import annotations

import logging
from typing import Any

from aiohttp import web

from tesseract.config.runtime_limits import (
    CONCURRENCY_KEY,
    load_max_concurrent_chat_turns_per_provider,
)
from tesseract.lib.yaml_io import round_trip_yaml
from tesseract.paths import config_dir
from tesseract.retention.policy import load_live, set_window

log = logging.getLogger(__name__)

# Bounds on what the rail may set. The loaders refuse <1 already; these are the
# rail's own ceilings, so a stepper cannot walk somewhere the operator would
# have to edit yaml by hand to escape.
CHATS_AT_ONCE_MIN, CHATS_AT_ONCE_MAX = 1, 10
ARCHIVE_DAYS_MIN, ARCHIVE_DAYS_MAX = 1, 365


def _runtime_path():
    return config_dir() / "runtime.yaml"


def _archive_after_days() -> int:
    for policy in load_live():
        if policy.tree.key == "sessions":
            return policy.keep_days
    raise ValueError("retention.yaml has no 'sessions' policy")


async def get_chat_settings(request: web.Request) -> web.Response:
    try:
        return web.json_response({
            "chats_at_once": load_max_concurrent_chat_turns_per_provider(
                _runtime_path()
            ),
            "archive_after_days": _archive_after_days(),
            "chats_at_once_range": [CHATS_AT_ONCE_MIN, CHATS_AT_ONCE_MAX],
            "archive_after_days_range": [ARCHIVE_DAYS_MIN, ARCHIVE_DAYS_MAX],
        })
    except (ValueError, OSError) as exc:
        return web.json_response({"error": str(exc)}, status=500)


def _read_int(body: dict[str, Any], key: str, lo: int, hi: int) -> int | None:
    raw = body.get(key)
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ValueError(f"{key} must be a whole number, got {raw!r}")
    if not lo <= raw <= hi:
        raise ValueError(f"{key} must be between {lo} and {hi}, got {raw}")
    return raw


def _apply_chats_at_once(app: web.Application, value: int) -> None:
    """Write the cap, then make it true of the running system.

    Semaphores are built lazily per provider at the cap current when the
    provider is first seen, so the live registry is dropped: the next turn
    builds one at the new cap. Without this the number would only ever take
    effect on a provider nobody had used yet.

    **This overshoots while turns are in flight, and by how much is knowable.**
    A turn holds the semaphore OBJECT it acquired, not a lookup through the
    dict, so it keeps running under the old cap and releases into an object
    nothing reads any more. Until those drain, a provider can carry its
    in-flight turns PLUS the new cap. Lowering 3 to 1 with three turns running
    permits four for as long as the slowest of them takes. The alternative is a
    limiter that can be resized under its own waiters, which is a real
    primitive and not this function; the bound is small, it is downward-only
    transient, and the operator lowering a cap is asking about the next turn
    rather than the ones already streaming.
    """
    round_trip_yaml(
        _runtime_path(),
        lambda doc: doc.__setitem__(CONCURRENCY_KEY, value),
    )
    app[CONCURRENCY_KEY] = value
    if app.get("chat_turn_semaphores") is not None:
        app["chat_turn_semaphores"] = {}


def _apply_archive_after_days(value: int) -> None:
    """The nightly sweep calls `load_live()` when it runs, so it reads this
    without a restart.

    Through `set_window` rather than a round trip of its own, because a round
    trip rewrites the WHOLE document from what it parsed. Two of them
    overlapping lose one silently: the second was parsed before the first
    landed and carries every other row at its old value. `retention.yaml` has
    three writers now — this rail, the Thrown away room's field, and
    `retention_set_window` from a phone — and `policy._writing` is the lock
    all of them have to be inside for that to be one file rather than three
    racing copies of it. The rail's own 1-to-365 range is checked before this
    is reached and is narrower than what `set_window` accepts, so nothing the
    rail could set starts being refused.
    """
    set_window(config_dir(), "sessions", value)


async def patch_chat_settings(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "body must be an object"}, status=400)
    try:
        chats_at_once = _read_int(body, "chats_at_once", CHATS_AT_ONCE_MIN, CHATS_AT_ONCE_MAX)
        archive_days = _read_int(
            body, "archive_after_days", ARCHIVE_DAYS_MIN, ARCHIVE_DAYS_MAX
        )
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=400)

    if chats_at_once is None and archive_days is None:
        return web.json_response(
            {"error": "give chats_at_once or archive_after_days"}, status=400
        )

    # Two independent files with no shared transaction between them. A failure
    # on the second cannot un-write the first, so the reply names what landed
    # rather than returning a bare 500 over a half-applied change.
    written: list[str] = []
    try:
        if chats_at_once is not None:
            _apply_chats_at_once(request.app, chats_at_once)
            written.append("chats_at_once")
        if archive_days is not None:
            _apply_archive_after_days(archive_days)
            written.append("archive_after_days")
    except (KeyError, ValueError, OSError) as exc:
        log.exception("chat settings write failed")
        return web.json_response(
            {"error": str(exc), "written": written}, status=500
        )

    return await get_chat_settings(request)

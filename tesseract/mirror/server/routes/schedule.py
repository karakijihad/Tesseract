from __future__ import annotations

from typing import Any

from aiohttp import web

from tesseract.scheduler.config_loader import RetryPolicy
from tesseract.scheduler.manifest.entry import MIN_SUMMARY_CHARS


async def list_jobs(request: web.Request) -> web.Response:
    """GET /api/schedule — every job + live runtime state.

    Response shape:
    `{"jobs": [{**JobConfig, "origin", "locked_fields", "runtime": {...} | null}]}`.
    `runtime` is `null` when the scheduler is not running.

    `origin` is read from which file the row came from, never from a naming
    convention, and `locked_fields` is what the app sets on a system job —
    together they are what lets the editor say a job is set by the app and
    follows it, at the point of edit, without the surface having to know the
    rule. An edit to anything else becomes an override row rather than a copy.

    `locked_fields` is asked PER ROW rather than being one list for all of
    them, because the answer differs by row: a row the app fires at a set time
    of day leaves that time to the operator, and the rest carry a rate that is
    part of what the work is. Asking the loader is what keeps this page and the
    write agreeing on which is which.
    """
    from tesseract.scheduler.config_loader import locked_fields_of, system_rows

    scheduler = request.app.get("scheduler")
    if scheduler is None:
        return web.json_response({"jobs": []})
    shipped = system_rows(scheduler.config_dir)
    jobs = []
    for cfg in scheduler.configs:
        is_system = cfg.name in shipped
        try:
            runtime = scheduler.runtime_state(cfg.name)
        except KeyError:
            runtime = None
        jobs.append({
            **cfg.model_dump(),
            "origin": "system" if is_system else "user",
            "locked_fields": sorted(locked_fields_of(shipped.get(cfg.name))),
            "removable": not is_system,
            "runtime": runtime,
        })
    return web.json_response({"jobs": jobs})


async def read_cadence(request: web.Request) -> web.Response:
    """GET /api/schedule/cadence?value=... - what a cadence means.

    The one reader. A form that decided for itself whether a cadence was valid,
    what it said and when it next fired was a second implementation of the
    scheduler's grammar, and the two disagreed four separate times: named
    weekdays, named months, Sunday's second number, day-of-month required WITH
    day-of-week rather than either, and `?`, `L` and `#`. Every one of those
    was a cadence the operator could not create and the runtime would have run.

    Anonymous-readable: it reads a string the caller already has and touches
    nothing.
    """
    from datetime import datetime, timezone

    from tesseract.mirror.server.routes._isotime import iso as _iso
    from tesseract.scheduler.cadence import explain

    reading = explain(
        request.query.get("value", ""), datetime.now(timezone.utc)
    )
    return web.json_response(
        {
            "ok": reading.ok,
            "problem": reading.problem,
            "words": reading.words,
            "nextFireAt": _iso(reading.next_fire_at),
        }
    )


async def list_roles(request: web.Request) -> web.Response:
    """GET /api/schedule/roles — role names available for the model_role
    dropdown in the Schedule view.

    Sourced from `roles.yaml::roles.*` keys via `load_bundle()`. Voice
    lanes (`stt`, `tts`) live under `roles.yaml::voice.*` and are
    intentionally excluded — they're not interchangeable cognition roles.
    """
    try:
        from tesseract.brain.boot import load_bundle
        bundle = load_bundle()
        names = sorted(bundle.roles.keys())
    except Exception as exc:  # noqa: BLE001
        return web.json_response(
            {"roles": [], "error": str(exc)}, status=503,
        )
    return web.json_response({"roles": names})


async def list_handlers(request: web.Request) -> web.Response:
    """GET /api/schedule/handlers — whitelist of registered handler classes.

    Used by the Mirror "Add job" modal to populate the handler dropdown
    without leaking arbitrary import paths to operator UI.

    It carries the summary floor too. The form has to disable its button
    before the round trip, and the number it needs is the manifest's, so
    typing it into the form was a second copy of a rule the engine enforces.
    """
    handlers = [
        {
            "dotpath": "tesseract.scheduler.tasks.daily_writer.DailyWriterJob",
            "label": "Daily writer (rollup)",
        },
        {
            "dotpath": "tesseract.scheduler.tasks.vault_lint.VaultLintJob",
            "label": "Vault lint",
        },
        {
            "dotpath": "tesseract.scheduler.tasks.chat_digest.ChatDigestJob",
            "label": "Chat digest",
        },
        {
            "dotpath": "tesseract.scheduler.tasks.conscience_heartbeat.ConscienceHeartbeatJob",
            "label": "Conscience heartbeat",
        },
        {
            "dotpath": "tesseract.scheduler.tasks.librarian_heartbeat.LibrarianHeartbeatJob",
            "label": "Librarian heartbeat",
        },
        {
            "dotpath": "tesseract.scheduler.tasks.index_rebuild.IndexRebuildJob",
            "label": "Index rebuild",
        },
        {
            "dotpath": "tesseract.scheduler.tasks.telegram_notify.TelegramNotifyJob",
            "label": "Telegram notify",
        },
        {
            "dotpath": "tesseract.scheduler.tasks.provider_watch.ProviderWatchJob",
            "label": "Provider watch (daily)",
        },
    ]
    return web.json_response(
        {"handlers": handlers, "minSummaryChars": MIN_SUMMARY_CHARS}
    )


class _BadField(ValueError):
    """A field the caller sent in a shape this route will not guess at."""


def _a_real_bool(value: Any, default: bool) -> bool:
    """A boolean the caller actually sent, or the default when they sent none.

    `bool()` is the wrong tool for a JSON body: `bool("false")` is True, so a
    client sending the string rather than the literal armed a job it was asking
    to leave off. Nor is silently taking the default an answer, which is the
    other half of the same mistake: the caller said something and the route
    decided otherwise without telling them. Absent means the default; present
    and wrong-typed is refused.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    raise _BadField("`enabled` has to be true or false, not a string or a number")


def _a_real_list(value: Any) -> list[str] | None:
    """The channels the caller named, or None for "use the routing".

    `list()` is the wrong tool for the same reason: `list("telegram")` is eight
    one-character destinations, silently, with no error. A bare string is a
    caller who plainly meant one channel and is read as one; anything that is
    not a string or a list is refused rather than dropped.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    raise _BadField("`delivery` has to be a list of channel names, or one name")


async def create_job(request: web.Request) -> web.Response:
    """POST /api/schedule/create — operator-direct job creation.

    Bypasses the tool/ASK flow because the Mirror UI gesture itself is
    the operator's approval. Body shape mirrors `ScheduleCreateInput`, and
    that claim is load-bearing: it is the whole contract for anything that is
    not the Add job form. So the defaults are that schema's defaults. `enabled`
    defaulted True here while the tool defaults False, which meant a body
    omitting the field described a plan through one door and armed live work
    through the other; the form always states it, so only a caller trusting the
    documented shape was caught. `delivery` was not forwarded at all, so a
    caller naming where a row reports was silently routed by `routing.yaml`.
    """
    scheduler = request.app.get("scheduler")
    if scheduler is None:
        return web.json_response({"error": "scheduler not running"}, status=503)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body"}, status=400)
    try:
        retry = RetryPolicy(
            max_retries=int(body.get("max_retries", 0)),
            backoff_seconds=int(body.get("backoff_seconds", 0)),
        )
        cfg = scheduler.add_job_runtime(
            name=str(body["name"]),
            cadence=str(body["cadence"]),
            handler=str(body["handler"]),
            # Required, and the engine says why when it is missing — the same
            # rule and the same sentence the `schedule_create` tool gets, since
            # both doors land on that one call.
            summary=str(body.get("summary") or ""),
            enabled=_a_real_bool(body.get("enabled"), False),
            on_failure=str(body.get("on_failure", "log")),
            retry_policy=retry,
            config=dict(body.get("config") or {}),
            delivery=_a_real_list(body.get("delivery")),
        )
    except KeyError as exc:
        return web.json_response({"error": f"missing field: {exc.args[0]}"}, status=400)
    except (ValueError, TypeError) as exc:
        return web.json_response({"error": str(exc)}, status=400)
    return web.json_response({
        "name": cfg.name,
        "cadence": cfg.cadence,
        "handler": cfg.handler,
        "summary": cfg.summary,
        "enabled": cfg.enabled,
        "on_failure": cfg.on_failure,
    })


async def remove_job(request: web.Request) -> web.Response:
    """DELETE /api/schedule/{name} — operator-direct job removal."""
    scheduler = request.app.get("scheduler")
    if scheduler is None:
        return web.json_response({"error": "scheduler not running"}, status=503)
    name = request.match_info["name"]
    try:
        cfg = scheduler.remove_job_runtime(name)
    except KeyError:
        return web.json_response({"error": f"job {name!r} not registered"}, status=404)
    return web.json_response({"removed": cfg.name})

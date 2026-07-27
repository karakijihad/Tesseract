from __future__ import annotations

from aiohttp import web

from tesseract.scheduler.config_loader import RetryPolicy


async def list_jobs(request: web.Request) -> web.Response:
    """GET /api/schedule — seeded jobs + live runtime state.

    Response shape: `{"jobs": [{**JobConfig, "runtime": {...} | null}]}`.
    `runtime` is `null` when the scheduler is not running.
    Contract: `Docs/Plan/scheduler/_shared/mirror-schedule-tab.md`.
    """
    scheduler = request.app.get("scheduler")
    if scheduler is None:
        return web.json_response({"jobs": []})
    jobs = []
    for cfg in scheduler.configs:
        try:
            runtime = scheduler.runtime_state(cfg.name)
        except KeyError:
            runtime = None
        jobs.append({**cfg.model_dump(), "runtime": runtime})
    return web.json_response({"jobs": jobs})


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
            "dotpath": "tesseract.scheduler.tasks.observer_idle.ObserverIdleJob",
            "label": "Observer idle nudge",
        },
        {
            "dotpath": "tesseract.scheduler.tasks.telegram_notify.TelegramNotifyJob",
            "label": "Telegram notify",
        },
        {
            "dotpath": "tesseract.scheduler.tasks.provider_watch.ProviderWatchJob",
            "label": "Provider watch (daily)",
        },
        {
            "dotpath": "tesseract.scheduler.tasks.daily_brief.DailyBriefJob",
            "label": "Daily brief (morning)",
        },
        {
            "dotpath": "tesseract.scheduler.tasks.interests_decay.InterestsDecayJob",
            "label": "Interests decay (nightly)",
        },
    ]
    return web.json_response({"handlers": handlers})


async def create_job(request: web.Request) -> web.Response:
    """POST /api/schedule/create — operator-direct job creation.

    Bypasses the tool/ASK flow because the Mirror UI gesture itself is
    the operator's approval. Body shape mirrors `ScheduleCreateInput`.
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
            enabled=bool(body.get("enabled", True)),
            on_failure=str(body.get("on_failure", "log")),
            retry_policy=retry,
            config=dict(body.get("config") or {}),
        )
    except KeyError as exc:
        return web.json_response({"error": f"missing field: {exc.args[0]}"}, status=400)
    except (ValueError, TypeError) as exc:
        return web.json_response({"error": str(exc)}, status=400)
    return web.json_response({
        "name": cfg.name,
        "cadence": cfg.cadence,
        "handler": cfg.handler,
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

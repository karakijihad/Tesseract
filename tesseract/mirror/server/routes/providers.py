"""Read-only provider catalog endpoints.

Surfaces catalog refs (``<tier>.<provider>.<model_id>``) so the Mirror
UI can populate dropdowns (e.g. the per-mission planner override) from
the live ``providers.yaml`` without hardcoding model names. Refs are
filtered to ``chat``-kind models so non-chat entries (embeddings, TTS,
STT, audio) don't show up in dropdowns that drive prompt-based work.
"""

from __future__ import annotations

import logging
from typing import Any

from aiohttp import web

logger = logging.getLogger(__name__)


async def list_chat_models(request: web.Request) -> web.Response:
    """GET /api/providers/catalog — list of chat-kind catalog refs.

    Response shape::

        {
          "models": [
            {"ref": "api.openai.gpt54_mini", "tier": "api",
             "provider": "openai", "model_id": "gpt54_mini",
             "model": "gpt-5.4-mini"},
            ...
          ]
        }

    Only ``ProviderModel.kind == "chat"`` (catalog default) is returned —
    embedding / TTS / STT / audio_stt entries are filtered out so a
    dropdown driving a prompt-based call (planner override, etc.) can't
    misroute to a non-chat model.
    """
    try:
        from tesseract.brain.boot import load_bundle
    except Exception:
        logger.exception("providers/catalog: boot import failed")
        return web.json_response({"error": "providers unavailable"}, status=503)

    try:
        bundle = load_bundle()
    except Exception as exc:  # noqa: BLE001 — config errors must surface
        logger.exception("providers/catalog: load_bundle failed")
        return web.json_response({"error": f"providers config: {exc}"}, status=503)

    out: list[dict[str, Any]] = []
    for ref, conn, model in bundle.all_models():
        if model.kind != "chat":
            continue
        out.append(
            {
                "ref": ref,
                "tier": conn.tier,
                "provider": conn.name,
                "model_id": model.id,
                "model": model.model,
            }
        )
    out.sort(key=lambda row: row["ref"])
    return web.json_response({"models": out})


__all__ = ["list_chat_models"]

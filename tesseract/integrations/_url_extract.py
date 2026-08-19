"""Auto-extract URLs from inbound channel messages (Session 3 2026-05-16).

When the operator shares a link on Telegram (article, doc, YouTube,
GitHub PR…), the assistant used to see only the URL string. The operator had to
manually ask "read this link". This module detects URLs and pulls their
content via Tavily extract so the page content rides into the chat turn
as part of the recall context.

Best-effort throughout: no API key, network failure, or extraction
error degrades silently to "URLs were detected but not extracted"
— never blocks the chat turn.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Any, Iterable

import httpx

from tesseract import http_client

log = logging.getLogger(__name__)

# Permissive URL regex — matches typical http(s):// links operators
# paste into chat. We deliberately reject mailto:/ftp:/ etc. (Tavily
# can't extract them anyway).
_URL_RE = re.compile(r"https?://[^\s<>\"'\)]+")

_TAVILY_ENDPOINT = "https://api.tavily.com/extract"
_TAVILY_TIMEOUT = 10.0
_MAX_URLS_PER_MESSAGE = 3
_MAX_CHARS_PER_URL = 4_000


def find_urls(text: str | None) -> list[str]:
    """Extract up to :data:`_MAX_URLS_PER_MESSAGE` distinct URLs from ``text``.

    De-duplicates while preserving order so the first link a user
    pastes wins when they paste 4+ links. Strips trailing punctuation
    that's almost always typing residue rather than part of the URL
    (``.``, ``,``, ``)``, etc.).
    """
    if not text:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for match in _URL_RE.findall(text):
        cleaned = match.rstrip(".,;:!?)\"'")
        if cleaned in seen:
            continue
        seen.add(cleaned)
        out.append(cleaned)
        if len(out) >= _MAX_URLS_PER_MESSAGE:
            break
    return out


async def _gate_allows(url_list: list[str], context: Any, ask_fn: Any, policy: Any) -> bool:
    """Put this fetch through the same decision `tavily_extract` gets.

    The extraction below is not a tool call, so nothing about it reached
    `permissions/decide.py::evaluate` — the posture in `permissions.yaml`, the
    audit row, the operator's ASK channel, all skipped. It resolves to `auto`
    today for every caller, so this changes no outcome; what it changes is that
    a future posture on `tavily_extract` applies here as well, instead of one
    call site quietly keeping the old answer.

    The decision is taken over the tool's real input, so a policy that reads
    the urls (`path_overrides`) sees what will actually be fetched.
    """
    from tesseract.kernel.tools.tavily_extract import (
        TavilyExtractInput,
        TavilyExtractTool,
    )
    from tesseract.permissions.decide import evaluate

    if context is None:
        # No context is no decision, and an outbound call nobody could have
        # refused is exactly what this function exists to prevent.
        log.warning("url-extract: no tool context to decide against; skipping")
        return False

    raw = {"urls": url_list, "extract_depth": "basic"}
    denial = await evaluate(
        tool=TavilyExtractTool(),
        validated=TavilyExtractInput(**raw),
        raw_input=raw,
        context=context,
        ask_fn=ask_fn,
        policy=policy,
    )
    if denial is not None:
        log.debug("url-extract: gate refused (%s)", denial.output)
        return False
    return True


async def extract_urls_to_context(
    urls: Iterable[str],
    *,
    context: Any,
    ask_fn: Any = None,
    policy: Any = None,
) -> str:
    """Call Tavily extract for ``urls`` and format the results as a
    ``<url_content>`` context block ready to inject above the user body.

    Returns an empty string when the permission gate refuses, when
    ``TAVILY_API_KEY`` is unset, when the call fails, or when all extractions
    came back empty. Per-URL content is capped at :data:`_MAX_CHARS_PER_URL` so
    a long article doesn't swamp the chat turn's token budget.

    ``context`` is required rather than optional: a caller with nothing to
    decide against would skip the gate, which is the hole this parameter
    exists to close.
    """
    url_list = list(urls)
    if not url_list:
        return ""
    if not await _gate_allows(url_list, context, ask_fn, policy):
        return ""
    # This path reaches Tavily without going through the `tavily_extract`
    # tool, so the catalog switch has to be honoured here too — otherwise
    # `services.tavily.enabled: false` silently keeps paying for extractions
    # on every inbound message carrying a link.
    from tesseract.kernel.tools.web_providers import (
        service_disabled_reason,
        service_key_env,
    )

    disabled = service_disabled_reason("tavily")
    if disabled is not None:
        log.debug("url-extract: %s; skipping", disabled)
        return ""
    key_env = service_key_env("tavily", "TAVILY_API_KEY")
    api_key = (os.environ.get(key_env) or "").strip()
    if not api_key:
        log.debug("url-extract: %s not set; skipping", key_env)
        return ""

    payload = {"urls": url_list, "extract_depth": "basic"}
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    try:
        async with http_client.async_client(timeout=_TAVILY_TIMEOUT) as client:
            response = await client.post(
                _TAVILY_ENDPOINT, headers=headers, json=payload,
            )
    except (httpx.HTTPError, asyncio.TimeoutError) as exc:
        log.debug("url-extract: tavily request failed (%s)", exc)
        return ""
    if response.status_code >= 400:
        log.debug("url-extract: tavily HTTP %s", response.status_code)
        return ""
    try:
        data = response.json()
    except ValueError:
        log.debug("url-extract: tavily returned non-JSON")
        return ""

    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list) or not results:
        return ""

    blocks: list[str] = []
    for entry in results:
        if not isinstance(entry, dict):
            continue
        url = str(entry.get("url") or "").strip()
        raw_content = entry.get("raw_content") or entry.get("content") or ""
        text = str(raw_content).strip()
        if not url or not text:
            continue
        if len(text) > _MAX_CHARS_PER_URL:
            text = text[: _MAX_CHARS_PER_URL - 1] + "…"
        blocks.append(f"### {url}\n{text}")

    if not blocks:
        return ""
    return "--- URL CONTENT (auto-extracted) ---\n" + "\n\n".join(blocks)


__all__ = ["find_urls", "extract_urls_to_context"]

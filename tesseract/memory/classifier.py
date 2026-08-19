"""Memory-section classifier — M2 fallback path.

Called by the librarian only when a daily section has no recognized
`[type]` prefix. Delegates to the configured `chat_brain` adapter with a
single-turn prompt defined by `<TESSERACT_HOME>/agents/memory-classifier.md`
(resolved at call time via `tesseract.paths.agents_dir()`).
Never raises — adapter / JSON / timeout failures all return
`(None, 0.0)` so the caller can log one `unclassifiable` event and skip.

"""

from __future__ import annotations

import asyncio
import json
import logging
import re

from tesseract.kernel.adapters.base import AdapterOptions, ModelAdapter
from tesseract.memory.types import MemoryType
from tesseract.agents.loader import resolve_agent_path

logger = logging.getLogger(__name__)

_BODY_TRUNCATE_CHARS = 400
_CONFIDENCE_FLOOR = 0.6
_TYPE_MAP: dict[str, MemoryType] = {t.value: t for t in MemoryType}


def _parse_llm_json(raw: str) -> dict:
    """Extract the first `{...}` block from raw text and parse it as JSON."""
    match = re.search(r"\{.*\}", raw or "", re.DOTALL)
    if not match:
        return {}
    try:
        return json.loads(match.group())
    except json.JSONDecodeError:
        return {}


def _load_agent_prompt() -> str:
    """Strip YAML frontmatter from the agent md and return the body.

    Re-read on every call — no cache. `classify_section` only calls this
    for unprefixed daily sections during a librarian pass (rare, and the
    file is a few hundred bytes), so the read cost is negligible; caching
    would mean an operator's edit to the card is silently ignored for the
    rest of the process's life, the same failure mode this call-time
    rewrite exists to close.

    Resolved through the loader rather than joined onto a directory: the card
    is a shipped one, so it lives in the app tree, and an operator shadow of
    the same slug has to win here as it does everywhere else.
    """
    path = resolve_agent_path("memory-classifier")
    text = path.read_text(encoding="utf-8")
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            text = text[end + 4:]
    return text.strip()


async def classify_section(
    title: str,
    body: str,
    adapter: ModelAdapter,
    *,
    options: AdapterOptions | None = None,
    timeout_s: float = 8.0,
) -> tuple[MemoryType | None, float]:
    """Classify one daily section into a `MemoryType`.

    Returns `(type, confidence)` on success, `(None, 0.0)` on timeout,
    malformed JSON, unknown type string, or confidence < 0.6.
    """
    truncated = (body or "")[:_BODY_TRUNCATE_CHARS]
    prompt_body = _load_agent_prompt()
    prompt = f"{prompt_body}\n\nTITLE: {title}\nBODY: {truncated}\n"

    try:
        raw = await asyncio.wait_for(
            adapter.generate(prompt, options or AdapterOptions()),
            timeout=timeout_s,
        )
    except asyncio.TimeoutError:
        logger.warning("memory-classifier: adapter timed out after %.1fs", timeout_s)
        return (None, 0.0)
    except Exception as exc:
        logger.warning("memory-classifier: adapter call failed (%s)", exc)
        return (None, 0.0)

    parsed = _parse_llm_json(raw)
    type_raw = parsed.get("type")
    try:
        confidence = float(parsed.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0

    mem_type = _TYPE_MAP.get(str(type_raw).lower()) if type_raw else None
    if mem_type is None or confidence < _CONFIDENCE_FLOOR:
        return (None, 0.0)
    return (mem_type, confidence)

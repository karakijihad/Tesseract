"""Image attachment handler — vision captioning via the chat_brain chain (CR-2A)."""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass

from tesseract.brain.boot import (
    ChatBrainConfig,
    adapter_options_from_chat_brain,
    build_chat_brain_adapter,
    load_chat_brain_chain,
)
from tesseract.brain.cost.ledger import CostLedger, CostUsage
from tesseract.kernel.adapters.base import AdapterOptions, ChunkType, ModelAdapter

log = logging.getLogger(__name__)


_PROMPT_NO_CAPTION = (
    "Describe this image in 2-4 sentences. Focus on the salient subject, "
    "any visible text, and the overall scene. Output the description only — "
    "no preamble, no markdown, no labels."
)

_PROMPT_WITH_CAPTION = (
    "Describe this image in 2-4 sentences with the user's caption in mind. "
    "Focus on the salient subject, any visible text, and how the caption "
    "relates to what's shown. Output the description only — no preamble, "
    "no markdown, no labels.\n\nUser caption: "
)

_VISION_ROLE = "channel_vision"


class ImageHandlerError(RuntimeError):
    """Raised when captioning fails; bridge maps to ``status="extract_failed"``."""


@dataclass(frozen=True)
class _VisionEntry:
    cfg: ChatBrainConfig
    adapter: ModelAdapter
    options: AdapterOptions


def _entry_supports_vision(cfg: ChatBrainConfig) -> bool:
    fields = cfg.ref.model.fields
    capabilities = fields.get("capabilities") or {}
    return bool(capabilities.get("vision_input"))


def find_vision_entry() -> _VisionEntry | None:
    """Return the first chat_brain chain entry with ``vision_input: true``, or ``None``.

    Resolved fresh each call so role swaps in ``roles.yaml`` land without a Mirror restart.
    """
    try:
        chain = load_chat_brain_chain()
    except Exception:
        log.exception("image handler: chat_brain chain unresolvable")
        return None
    for cfg in chain:
        if not _entry_supports_vision(cfg):
            continue
        try:
            adapter = build_chat_brain_adapter(cfg)
        except RuntimeError as exc:
            log.info(
                "image handler: vision-capable entry %s/%s unavailable (%s); trying next",
                cfg.provider, cfg.model, exc,
            )
            continue
        return _VisionEntry(
            cfg=cfg,
            adapter=adapter,
            options=adapter_options_from_chat_brain(cfg),
        )
    return None


def _build_messages(image_bytes: bytes, *, mime: str, caption: str | None) -> list[dict]:
    encoded = base64.b64encode(image_bytes).decode("ascii")
    stripped = caption.strip() if caption else ""
    prompt = _PROMPT_WITH_CAPTION + stripped if stripped else _PROMPT_NO_CAPTION
    return [
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": prompt},
                {"type": "input_image", "image_url": f"data:{mime};base64,{encoded}"},
            ],
        }
    ]


async def describe_image(
    image_bytes: bytes,
    *,
    mime: str,
    caption: str | None = None,
    cost_ledger: CostLedger | None = None,
    entry: _VisionEntry | None = None,
    max_chars: int = 800,
) -> str:
    """Return a short natural-language description of ``image_bytes``.

    ``entry`` lets tests pin a specific adapter without monkeypatching; production callers omit it.
    """
    if not image_bytes:
        raise ImageHandlerError("empty image bytes")
    if not mime:
        raise ImageHandlerError("missing image mime type")

    chosen = entry or find_vision_entry()
    if chosen is None:
        raise ImageHandlerError("no vision-capable adapter available in chat_brain chain")

    messages = _build_messages(image_bytes, mime=mime, caption=caption)
    text_parts: list[str] = []
    usage_dict: dict | None = None
    try:
        async for chunk in chosen.adapter.stream(messages, options=chosen.options):
            if chunk.type == ChunkType.TEXT:
                text_parts.append(chunk.text)
            elif chunk.type == ChunkType.STOP:
                raw = chunk.raw or {}
                maybe_usage = raw.get("usage")
                if isinstance(maybe_usage, dict):
                    usage_dict = maybe_usage
            elif chunk.type == ChunkType.ERROR:
                raise ImageHandlerError(f"vision adapter error: {chunk.error}")
    except ImageHandlerError:
        raise
    except Exception as exc:
        raise ImageHandlerError(f"vision call failed: {exc}") from exc

    description = "".join(text_parts).strip()
    if not description:
        raise ImageHandlerError("vision adapter returned empty description")
    if max_chars > 0 and len(description) > max_chars:
        description = description[: max_chars - 1].rstrip() + "…"

    if cost_ledger is not None and usage_dict is not None:
        try:
            cost_ledger.record(
                role=_VISION_ROLE,
                model=chosen.cfg.model,
                usage=CostUsage(
                    input_tokens=int(usage_dict.get("input_tokens", 0) or 0),
                    output_tokens=int(usage_dict.get("output_tokens", 0) or 0),
                    cached_tokens=int(usage_dict.get("cached_tokens", 0) or 0),
                ),
            )
        except Exception:
            log.exception(
                "image handler: cost ledger record failed for role=%s model=%s",
                _VISION_ROLE, chosen.cfg.model,
            )
    return description


__all__ = ["ImageHandlerError", "describe_image", "find_vision_entry"]

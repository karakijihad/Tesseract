"""CR-2A unit tests: :mod:`tesseract.integrations._handlers.image`.

The handler walks the chat_brain chain for a vision-capable adapter,
streams a single user message with a base64 ``input_image`` part, and
debits ``channel_vision`` on the cost ledger. We inject a fake
``_VisionEntry`` so these tests don't touch the real provider chain.
"""

from __future__ import annotations

import base64

import pytest

from tesseract.brain.boot import ChatBrainConfig
from tesseract.brain.cost.ledger import CostUsage
from tesseract.config.loader import ProviderConnection, ProviderModel, ResolvedRef
from tesseract.integrations._handlers import image as image_handler
from tesseract.integrations._handlers.image import (
    ImageHandlerError,
    describe_image,
    find_vision_entry,
)
from tesseract.kernel.adapters.base import (
    AdapterOptions,
    ChunkType,
    ErrorKind,
    ModelAdapter,
    StreamChunk,
)


def _model_entry(*, vision: bool, model_id: str = "gpt-5.4-mini") -> ProviderModel:
    return ProviderModel(
        id="m1",
        kind="chat",
        model=model_id,
        fields={
            "model": model_id,
            "context_window": 400000,
            "max_output_tokens": 1024,
            "temperature": 1.0,
            "reasoning_effort": "high",
            "knowledge_cutoff": "2025-01-01",
            "use_responses_api": True,
            "cost_per_mtok_in": 0.75,
            "cost_per_mtok_out": 4.50,
            "capabilities": {
                "vision_input": vision,
                "audio_input": False,
                "video_input": False,
                "pdf_input": True,
                "image_output": False,
                "audio_output": False,
            },
        },
    )


def _resolved_ref(*, vision: bool, model_id: str = "gpt-5.4-mini") -> ResolvedRef:
    connection = ProviderConnection(
        tier="api",
        name="openai",
        adapter="openai",
        timeout_seconds=60,
        max_retries=3,
        api_key_env="OPENAI_API_KEY",
    )
    return ResolvedRef(
        ref=f"api.openai.{model_id.replace('.', '_').replace('-', '_')}",
        connection=connection,
        model=_model_entry(vision=vision, model_id=model_id),
    )


def _chat_cfg(*, vision: bool = True, model_id: str = "gpt-5.4-mini") -> ChatBrainConfig:
    ref = _resolved_ref(vision=vision, model_id=model_id)
    return ChatBrainConfig(
        provider="openai",
        model=model_id,
        tier="api",
        temperature=1.0,
        max_output_tokens=1024,
        context_window=400000,
        reasoning_effort="high",
        knowledge_cutoff="2025-01-01",
        use_responses_api=True,
        compact_threshold=0.5,
        keep_recent_turns=10,
        head_anchor_messages=3,
        active_window_tokens=None,
        summary_char_budget=8_000,
        provider_cfg={"tier": "api", "provider": "openai", "adapter": "openai"},
        ref=ref,
        tool_iteration_cap=12,
        consecutive_error_cap=3,
    )


class _FakeAdapter(ModelAdapter):
    def __init__(self, chunks: list[StreamChunk]) -> None:
        self._chunks = chunks
        self.calls: list[list[dict]] = []

    async def stream(self, messages, tools=None, options=None):  # type: ignore[override]
        del tools, options
        self.calls.append(messages)
        for chunk in self._chunks:
            yield chunk

    def count_tokens(self, messages):  # type: ignore[override]
        del messages
        return 0

    async def check_available(self) -> bool:  # type: ignore[override]
        return True


def _entry(*, vision: bool = True, chunks: list[StreamChunk]) -> image_handler._VisionEntry:
    cfg = _chat_cfg(vision=vision)
    adapter = _FakeAdapter(chunks)
    options = AdapterOptions(model=cfg.model, provider=cfg.provider, role="chat_brain")
    return image_handler._VisionEntry(cfg=cfg, adapter=adapter, options=options)


@pytest.mark.asyncio
async def test_describe_image_returns_text_concatenated_across_chunks() -> None:
    entry = _entry(
        chunks=[
            StreamChunk(type=ChunkType.TEXT, text="A cat "),
            StreamChunk(type=ChunkType.TEXT, text="on a roof."),
            StreamChunk(type=ChunkType.STOP, raw={"usage": {"input_tokens": 10, "output_tokens": 4}}),
        ],
    )

    out = await describe_image(b"PNG-bytes", mime="image/png", entry=entry)
    assert out == "A cat on a roof."


@pytest.mark.asyncio
async def test_describe_image_includes_caption_in_prompt() -> None:
    entry = _entry(
        chunks=[
            StreamChunk(type=ChunkType.TEXT, text="caption-aware description"),
            StreamChunk(type=ChunkType.STOP, raw={}),
        ],
    )

    await describe_image(
        b"PNG-bytes",
        mime="image/png",
        caption="what's in this image?",
        entry=entry,
    )
    messages = entry.adapter.calls[0]  # type: ignore[attr-defined]
    text_part = messages[0]["content"][0]
    assert "User caption: what's in this image?" in text_part["text"]


@pytest.mark.asyncio
async def test_describe_image_skips_caption_when_blank() -> None:
    entry = _entry(
        chunks=[
            StreamChunk(type=ChunkType.TEXT, text="plain description"),
            StreamChunk(type=ChunkType.STOP, raw={}),
        ],
    )

    await describe_image(b"PNG-bytes", mime="image/png", caption="   ", entry=entry)
    messages = entry.adapter.calls[0]  # type: ignore[attr-defined]
    text_part = messages[0]["content"][0]
    assert "User caption" not in text_part["text"]


@pytest.mark.asyncio
async def test_describe_image_embeds_data_url_with_mime() -> None:
    entry = _entry(
        chunks=[
            StreamChunk(type=ChunkType.TEXT, text="ok"),
            StreamChunk(type=ChunkType.STOP, raw={}),
        ],
    )

    image_bytes = b"\x89PNG\r\n\x1a\n"
    await describe_image(image_bytes, mime="image/png", entry=entry)
    messages = entry.adapter.calls[0]  # type: ignore[attr-defined]
    image_part = messages[0]["content"][1]
    expected = f"data:image/png;base64,{base64.b64encode(image_bytes).decode()}"
    assert image_part["image_url"] == expected
    assert image_part["type"] == "input_image"


@pytest.mark.asyncio
async def test_error_chunk_raises_image_handler_error() -> None:
    entry = _entry(
        chunks=[
            StreamChunk(type=ChunkType.ERROR, error="rate limited", error_kind=ErrorKind.TRANSIENT),
        ],
    )

    with pytest.raises(ImageHandlerError) as exc_info:
        await describe_image(b"bytes", mime="image/jpeg", entry=entry)
    assert "rate limited" in str(exc_info.value)


@pytest.mark.asyncio
async def test_empty_response_raises_image_handler_error() -> None:
    entry = _entry(
        chunks=[StreamChunk(type=ChunkType.STOP, raw={})],
    )

    with pytest.raises(ImageHandlerError) as exc_info:
        await describe_image(b"bytes", mime="image/jpeg", entry=entry)
    assert "empty" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_truncates_at_max_chars_with_ellipsis() -> None:
    long = "x" * 1000
    entry = _entry(
        chunks=[
            StreamChunk(type=ChunkType.TEXT, text=long),
            StreamChunk(type=ChunkType.STOP, raw={}),
        ],
    )

    out = await describe_image(b"bytes", mime="image/jpeg", entry=entry, max_chars=50)
    assert len(out) == 50
    assert out.endswith("…")


@pytest.mark.asyncio
async def test_empty_bytes_raises_before_dispatch() -> None:
    with pytest.raises(ImageHandlerError) as exc_info:
        await describe_image(b"", mime="image/png")
    assert "empty" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_missing_mime_raises_before_dispatch() -> None:
    with pytest.raises(ImageHandlerError) as exc_info:
        await describe_image(b"bytes", mime="")
    assert "mime" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_no_vision_capable_entry_raises(monkeypatch) -> None:
    monkeypatch.setattr(
        "tesseract.integrations._handlers.image.find_vision_entry",
        lambda: None,
    )
    with pytest.raises(ImageHandlerError) as exc_info:
        await describe_image(b"bytes", mime="image/png", entry=None)
    assert "vision" in str(exc_info.value).lower()


class _FakeLedger:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, CostUsage]] = []

    def record(self, role: str, model: str, usage: CostUsage):
        self.calls.append((role, model, usage))


@pytest.mark.asyncio
async def test_cost_ledger_records_under_channel_vision_role() -> None:
    entry = _entry(
        chunks=[
            StreamChunk(type=ChunkType.TEXT, text="a photo"),
            StreamChunk(type=ChunkType.STOP, raw={"usage": {"input_tokens": 1200, "output_tokens": 8, "cached_tokens": 200}}),
        ],
    )
    ledger = _FakeLedger()

    await describe_image(
        b"bytes", mime="image/png", entry=entry, cost_ledger=ledger
    )
    assert len(ledger.calls) == 1
    role, model, usage = ledger.calls[0]
    assert role == "channel_vision"
    assert model == "gpt-5.4-mini"
    assert usage.input_tokens == 1200
    assert usage.output_tokens == 8
    assert usage.cached_tokens == 200


@pytest.mark.asyncio
async def test_cost_ledger_ignored_when_stop_has_no_usage() -> None:
    entry = _entry(
        chunks=[
            StreamChunk(type=ChunkType.TEXT, text="a photo"),
            StreamChunk(type=ChunkType.STOP, raw={}),
        ],
    )
    ledger = _FakeLedger()

    await describe_image(
        b"bytes", mime="image/png", entry=entry, cost_ledger=ledger
    )
    assert ledger.calls == []


def test_find_vision_entry_skips_non_vision_entries(monkeypatch) -> None:
    non_vision = _chat_cfg(vision=False, model_id="gpt-5.4-nano")
    vision = _chat_cfg(vision=True, model_id="gpt-5.4-mini")

    monkeypatch.setattr(
        "tesseract.integrations._handlers.image.load_chat_brain_chain",
        lambda: [non_vision, vision],
    )
    built: list[ChatBrainConfig] = []

    def fake_build(cfg):
        built.append(cfg)
        return _FakeAdapter([])

    monkeypatch.setattr(
        "tesseract.integrations._handlers.image.build_chat_brain_adapter",
        fake_build,
    )
    monkeypatch.setattr(
        "tesseract.integrations._handlers.image.adapter_options_from_chat_brain",
        lambda cfg: AdapterOptions(model=cfg.model, provider=cfg.provider, role="chat_brain"),
    )

    result = find_vision_entry()
    assert result is not None
    assert result.cfg.model == "gpt-5.4-mini"
    # Only the vision-capable entry should have been built.
    assert [c.model for c in built] == ["gpt-5.4-mini"]


def test_find_vision_entry_returns_none_when_chain_lacks_vision(monkeypatch) -> None:
    monkeypatch.setattr(
        "tesseract.integrations._handlers.image.load_chat_brain_chain",
        lambda: [_chat_cfg(vision=False)],
    )
    assert find_vision_entry() is None

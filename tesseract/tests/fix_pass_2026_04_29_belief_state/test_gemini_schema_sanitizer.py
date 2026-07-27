"""GeminiAdapter._sanitize_schema regression — must not strip property
names that happen to match dropped JSON Schema keywords.

Pre-fix bug (2026-04-29): the sanitizer dropped any key in the recursive
walk equal to `title`, `default`, `additionalProperties`, etc. Inside a
`properties: {...}` map the keys are field names, not schema keywords —
dropping `title` there erased the memory_save tool's title field while
leaving it in `required`, which Gemini rejects with
`function_declarations[N].parameters.required[1]: property is not defined`.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from tesseract.kernel.adapters.base import ChunkType
from tesseract.kernel.adapters.gemini import GeminiAdapter
from tesseract.kernel.tools.memory_save import MemorySaveInput


def _sanitize(schema: dict) -> dict:
    adapter = GeminiAdapter.__new__(GeminiAdapter)
    return adapter._sanitize_schema(schema)


def test_property_named_title_is_preserved() -> None:
    schema = MemorySaveInput.model_json_schema()
    cleaned = _sanitize(schema)
    props = cleaned.get("properties", {})
    assert "title" in props, "property named 'title' must survive sanitization"
    required = cleaned.get("required") or []
    missing = [r for r in required if r not in props]
    assert missing == [], f"required names missing from properties: {missing}"


def test_top_level_title_metadata_still_dropped() -> None:
    schema = {
        "title": "MySchema",
        "type": "object",
        "properties": {"x": {"type": "string", "title": "X"}},
    }
    cleaned = _sanitize(schema)
    assert "title" not in cleaned, "top-level schema title is metadata and must be dropped"
    # The nested per-property title is also metadata — drop it.
    assert "title" not in cleaned["properties"]["x"]
    # But the property name "x" must remain.
    assert "x" in cleaned["properties"]


def test_property_named_default_is_preserved() -> None:
    """`default` is also in the drop set; same hazard if a tool ever names a
    property 'default'."""
    schema = {
        "type": "object",
        "properties": {
            "default": {"type": "string", "title": "Default"},
            "other": {"type": "string"},
        },
        "required": ["default"],
    }
    cleaned = _sanitize(schema)
    assert "default" in cleaned["properties"]
    assert cleaned["required"] == ["default"]


def test_arbitrary_drop_key_as_property_name_is_preserved() -> None:
    """Generalized: every key in _GEMINI_SCHEMA_DROP_KEYS must survive when
    used as a property name."""
    from tesseract.kernel.adapters.gemini import _GEMINI_SCHEMA_DROP_KEYS

    for key in _GEMINI_SCHEMA_DROP_KEYS:
        schema = {
            "type": "object",
            "properties": {key: {"type": "string"}},
            "required": [key],
        }
        cleaned = _sanitize(schema)
        assert key in cleaned["properties"], f"property name {key!r} dropped by sanitizer"


def test_gemini_missing_function_call_ids_get_unique_fallbacks() -> None:
    """Gemini may omit function-call ids. Falling back to the tool name alone
    makes repeated calls like `glob` collide in Mirror React keys and tool
    result routing."""

    def function_chunk(name: str):
        fc = SimpleNamespace(id="", name=name, args={})
        part = SimpleNamespace(function_call=fc)
        content = SimpleNamespace(parts=[part])
        candidate = SimpleNamespace(content=content, finish_reason=None)
        return SimpleNamespace(text="", candidates=[candidate], usage_metadata=None)

    async def fake_stream():
        yield function_chunk("glob")
        yield function_chunk("glob")

    class FakeModels:
        async def generate_content_stream(self, **_kwargs):
            return fake_stream()

    adapter = GeminiAdapter.__new__(GeminiAdapter)
    adapter._genai = SimpleNamespace(
        types=SimpleNamespace(GenerateContentConfig=lambda **kwargs: kwargs)
    )
    adapter.client = SimpleNamespace(aio=SimpleNamespace(models=FakeModels()))

    chunks = asyncio.run(_collect(adapter._do_stream(
        model="gemini-test",
        contents=[],
        config_kwargs={},
    )))
    ids = [
        chunk.tool_call_id
        for chunk in chunks
        if chunk.type is ChunkType.TOOL_CALL_START
    ]
    assert ids == ["gemini_glob_1", "gemini_glob_2"]


async def _collect(stream):
    return [chunk async for chunk in stream]

from __future__ import annotations

from types import SimpleNamespace

from tesseract.kernel.adapters.gemini import GeminiAdapter


def test_gemini_tool_schema_drops_pydantic_exclusive_minimum():
    adapter = object.__new__(GeminiAdapter)
    adapter._genai = SimpleNamespace(types=SimpleNamespace())

    raw = {
        "type": "object",
        "title": "ReadInput",
        "properties": {
            "limit": {
                "type": "integer",
                "default": 2000,
                "exclusiveMinimum": 0,
                "maximum": 10000,
                "title": "Limit",
            },
            "offset": {
                "anyOf": [{"type": "integer", "minimum": 0}, {"type": "null"}],
                "default": None,
            },
        },
        "additionalProperties": False,
    }

    cleaned = adapter._sanitize_schema(raw)

    assert "title" not in cleaned
    assert "additionalProperties" not in cleaned
    limit = cleaned["properties"]["limit"]
    assert "exclusiveMinimum" not in limit
    assert "default" not in limit
    assert limit["maximum"] == 10000
    assert cleaned["properties"]["offset"]["type"] == "integer"

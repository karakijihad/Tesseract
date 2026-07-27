"""W1 schema curation — the trio-path verbs must advertise their real kernel
input schemas over MCP tools/list, never the permissive
`{"additionalProperties": true}` fallback (P7 live-gate finding pattern)."""

from __future__ import annotations

from tesseract.mirror.server.mcp.tools import _input_schema


def _is_permissive(schema: dict) -> bool:
    return schema.get("additionalProperties") is True and not schema.get("properties")


def test_vault_ingest_advertises_kernel_schema():
    schema = _input_schema("vault.ingest")
    assert not _is_permissive(schema)
    assert "source_path" in schema.get("properties", {})
    assert "source_path" in schema.get("required", [])


def test_lane_send_advertises_kernel_schema():
    schema = _input_schema("lane.send")
    assert not _is_permissive(schema)
    props = schema.get("properties", {})
    assert "lane_id" in props
    assert "message" in props


def test_memory_save_and_update_still_curated():
    for verb in ("memory.save", "memory.update"):
        schema = _input_schema(verb)
        assert not _is_permissive(schema), verb


def test_lane_ensure_and_close_have_real_schemas():
    assert "name" in _input_schema("lane.ensure").get("properties", {})
    assert "lane_id" in _input_schema("lane.close").get("properties", {})

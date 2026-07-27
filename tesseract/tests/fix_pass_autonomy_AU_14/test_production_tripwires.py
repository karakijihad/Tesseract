"""AU-14 Session 14b — production tripwire coverage.

Hooks into adapter_chain (HARD pre-commit + empty-output), image_generate
(uniform / HTTP / shape branches), tavily_search, and web_search.
Each tripwire writes a ``source="production_tripwire"`` row to the
per-role JSONL log; happy paths must NEVER write a row.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from tesseract.brain.adapter_chain import (
    _drift_kind_for_hard,
    _emit_production_tripwire,
)
from tesseract.kernel.adapters.base import AdapterOptions
from tesseract.orchestrator import provider_health as ph


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    return tmp_path


def _read_rows(role: str) -> list[dict[str, Any]]:
    path = ph.provider_health_dir() / f"{role}.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ── note_production_tripwire ────────────────────────────────


def test_note_production_tripwire_writes_row(isolated_home: Path) -> None:
    ph.note_production_tripwire(
        role="chat_brain",
        ref="api.openai.gpt54_nano",
        drift_kind="http_error",
        evidence={"status_code": 500},
    )
    rows = _read_rows("chat_brain")
    assert len(rows) == 1
    row = rows[0]
    assert row["source"] == "production_tripwire"
    assert row["ok"] is False
    assert row["drift_kind"] == "http_error"
    assert row["evidence"]["status_code"] == 500


def test_note_production_tripwire_skips_empty_role_or_ref(isolated_home: Path) -> None:
    ph.note_production_tripwire(role="", ref="api.x.y", drift_kind="http_error")
    ph.note_production_tripwire(role="x", ref="", drift_kind="http_error")
    assert not any(ph.provider_health_dir().glob("*.jsonl"))


def test_note_production_tripwire_routes_drift_to_publisher(isolated_home: Path) -> None:
    captured: list[Any] = []
    ph.note_production_tripwire(
        role="chat_brain",
        ref="api.openai.gpt54_nano",
        drift_kind="http_error",
        evidence={},
        publisher=lambda r: captured.append(r),
    )
    assert len(captured) == 1
    assert captured[0].source == "production_tripwire"


# ── _drift_kind_for_hard classifier ─────────────────────────


def test_drift_kind_classifier_buckets() -> None:
    assert _drift_kind_for_hard(None) == "http_error"
    assert _drift_kind_for_hard("") == "http_error"
    assert _drift_kind_for_hard("BadRequest: shape mismatch in response") == "shape_mismatch"
    assert _drift_kind_for_hard("invalid JSON envelope") == "shape_mismatch"
    assert _drift_kind_for_hard("AuthenticationError 401") == "unavailable"
    assert _drift_kind_for_hard("model not found") == "unavailable"
    assert _drift_kind_for_hard("invalid api key") == "unavailable"
    assert _drift_kind_for_hard("context window exceeded by 12000 tokens") == "schema_error"
    assert _drift_kind_for_hard("HTTP 500 internal server error") == "http_error"


def test_drift_kind_compound_auth_wins_over_shape() -> None:
    """Tie-break: a compound message that names BOTH an auth failure
    AND a shape problem routes to ``unavailable``. AU-5's mapper can
    propose a credential fix; it cannot draft a schema patch for a
    request that never reached the model."""
    assert _drift_kind_for_hard(
        "Authentication failed: schema validation rejected request"
    ) == "unavailable"
    assert _drift_kind_for_hard(
        "AuthenticationError: invalid JSON envelope"
    ) == "unavailable"


def test_emit_production_tripwire_skips_unnamed_options(isolated_home: Path) -> None:
    """No role / no model => no anonymous row."""
    _emit_production_tripwire(
        AdapterOptions(),  # everything empty
        drift_kind="http_error",
        evidence={"error": "x"},
    )
    assert not any(ph.provider_health_dir().glob("*.jsonl"))


def test_emit_production_tripwire_writes_row_for_full_options(isolated_home: Path) -> None:
    _emit_production_tripwire(
        AdapterOptions(role="chat_brain", provider="openai", model="gpt54_nano", tier="api"),
        drift_kind="shape_mismatch",
        evidence={"error": "ContentFilterFinishReasonError", "chain_index": 0},
    )
    rows = _read_rows("chat_brain")
    assert len(rows) == 1
    assert rows[0]["ref"] == "api.openai.gpt54_nano"
    assert rows[0]["drift_kind"] == "shape_mismatch"
    assert rows[0]["source"] == "production_tripwire"


# ── tavily_search tripwires ─────────────────────────────────


class _FakeResponse:
    def __init__(self, *, status_code: int = 200, body_text: str = "", body_json: Any = None) -> None:
        self.status_code = status_code
        self.text = body_text
        self._json = body_json

    def json(self) -> Any:
        if self._json is None:
            raise ValueError("no json")
        return self._json


class _FakeClient:
    """Drop-in for ``httpx.AsyncClient`` with a one-shot response/error."""

    def __init__(self, *, response: _FakeResponse | None = None, raise_exc: Exception | None = None) -> None:
        self._response = response
        self._raise = raise_exc

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def post(self, *args: Any, **kwargs: Any) -> _FakeResponse:
        if self._raise is not None:
            raise self._raise
        return self._response  # type: ignore[return-value]

    async def get(self, *args: Any, **kwargs: Any) -> _FakeResponse:
        if self._raise is not None:
            raise self._raise
        return self._response  # type: ignore[return-value]


def _run_tool(tool: Any, tool_input: Any) -> Any:
    from tesseract.kernel.tools.base import ToolContext
    ctx = ToolContext(workspace_root="", session_id="test", current_call_id="c")
    return asyncio.run(tool.run(tool_input, ctx))


def test_tavily_429_writes_tripwire(isolated_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tesseract.kernel.tools.tavily_search import TavilySearchInput, TavilySearchTool

    monkeypatch.setenv("TAVILY_API_KEY", "dummy")
    monkeypatch.setattr(
        "tesseract.kernel.tools.tavily_search.httpx.AsyncClient",
        lambda *a, **kw: _FakeClient(response=_FakeResponse(status_code=429, body_text="rate limited")),
    )
    result = _run_tool(TavilySearchTool(), TavilySearchInput(query="x"))
    assert result.is_error
    rows = _read_rows("tavily_search")
    assert len(rows) == 1
    assert rows[0]["drift_kind"] == "http_error"
    assert rows[0]["evidence"]["status_code"] == 429
    assert rows[0]["source"] == "production_tripwire"


def test_tavily_non_json_writes_tripwire(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tesseract.kernel.tools.tavily_search import TavilySearchInput, TavilySearchTool

    monkeypatch.setenv("TAVILY_API_KEY", "dummy")
    monkeypatch.setattr(
        "tesseract.kernel.tools.tavily_search.httpx.AsyncClient",
        lambda *a, **kw: _FakeClient(response=_FakeResponse(status_code=200, body_json=None)),
    )
    result = _run_tool(TavilySearchTool(), TavilySearchInput(query="x"))
    assert result.is_error
    rows = _read_rows("tavily_search")
    assert len(rows) == 1
    assert rows[0]["drift_kind"] == "shape_mismatch"


def test_tavily_timeout_writes_tripwire(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tesseract.kernel.tools.tavily_search import TavilySearchInput, TavilySearchTool

    monkeypatch.setenv("TAVILY_API_KEY", "dummy")
    monkeypatch.setattr(
        "tesseract.kernel.tools.tavily_search.httpx.AsyncClient",
        lambda *a, **kw: _FakeClient(raise_exc=httpx.TimeoutException("slow")),
    )
    result = _run_tool(TavilySearchTool(), TavilySearchInput(query="x"))
    assert result.is_error
    rows = _read_rows("tavily_search")
    assert len(rows) == 1
    assert rows[0]["drift_kind"] == "latency_spike"


def test_tavily_happy_path_no_tripwire(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tesseract.kernel.tools.tavily_search import TavilySearchInput, TavilySearchTool

    monkeypatch.setenv("TAVILY_API_KEY", "dummy")
    monkeypatch.setattr(
        "tesseract.kernel.tools.tavily_search.httpx.AsyncClient",
        lambda *a, **kw: _FakeClient(
            response=_FakeResponse(
                status_code=200,
                body_json={"results": [{"title": "t", "url": "https://x", "content": "c"}], "answer": ""},
            )
        ),
    )
    result = _run_tool(TavilySearchTool(), TavilySearchInput(query="x"))
    assert not result.is_error
    assert _read_rows("tavily_search") == []


# ── web_search (Brave) tripwires ────────────────────────────


def test_brave_401_writes_tripwire(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tesseract.kernel.tools.web_search import WebSearchInput, WebSearchTool

    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "dummy")
    monkeypatch.setattr(
        "tesseract.kernel.tools.web_search.httpx.AsyncClient",
        lambda *a, **kw: _FakeClient(response=_FakeResponse(status_code=401, body_text="bad key")),
    )
    result = _run_tool(WebSearchTool(), WebSearchInput(query="x"))
    assert result.is_error
    rows = _read_rows("web_search")
    assert len(rows) == 1
    assert rows[0]["drift_kind"] == "unavailable"
    assert rows[0]["evidence"]["status_code"] == 401


def test_brave_timeout_writes_tripwire(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tesseract.kernel.tools.web_search import WebSearchInput, WebSearchTool

    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "dummy")
    monkeypatch.setattr(
        "tesseract.kernel.tools.web_search.httpx.AsyncClient",
        lambda *a, **kw: _FakeClient(raise_exc=httpx.TimeoutException("slow")),
    )
    result = _run_tool(WebSearchTool(), WebSearchInput(query="x"))
    assert result.is_error
    rows = _read_rows("web_search")
    assert len(rows) == 1
    assert rows[0]["drift_kind"] == "latency_spike"  # matches the Tavily bucket


def test_brave_non_json_writes_tripwire(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tesseract.kernel.tools.web_search import WebSearchInput, WebSearchTool

    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "dummy")
    monkeypatch.setattr(
        "tesseract.kernel.tools.web_search.httpx.AsyncClient",
        lambda *a, **kw: _FakeClient(response=_FakeResponse(status_code=200, body_json=None)),
    )
    result = _run_tool(WebSearchTool(), WebSearchInput(query="x"))
    assert result.is_error
    rows = _read_rows("web_search")
    assert len(rows) == 1
    assert rows[0]["drift_kind"] == "shape_mismatch"


# ── image_generate tripwires ────────────────────────────────


def _image_inputs() -> Any:
    from tesseract.kernel.tools.image_generate import ImageGenerateInput
    return ImageGenerateInput(prompt="probe", steps=10, cfg_scale=3.5, width=512, height=512)


class _StubRef:
    ref = "api.nim.flux1_dev"

    class _Conn:
        tier_enabled = True
        enabled = True
        timeout_seconds = 30.0
        api_key_env = "NIM_FAKE_KEY"
    connection = _Conn()

    class _Model:
        model = "black-forest-labs/flux.1-dev"
        fields = {"base_url_override": "https://fake/genai"}
    model = _Model()


class _StubRole:
    mode = "active"
    primary = _StubRef()


class _StubBundle:
    def role(self, name: str) -> Any:  # noqa: ARG002
        return _StubRole()


def _patch_image_load_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "tesseract.config.loader.load_config",
        lambda: _StubBundle(),
    )
    monkeypatch.setenv("NIM_FAKE_KEY", "dummy")


def test_image_generate_uniform_writes_tripwire(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tesseract.kernel.tools.base import ToolContext
    from tesseract.kernel.tools.image_generate import ImageGenerateTool

    _patch_image_load_config(monkeypatch)
    # 2-byte payload base64-encoded → tiny image bytes → uniform-frame trip.
    monkeypatch.setattr(
        "tesseract.kernel.tools.image_generate.httpx.AsyncClient",
        lambda *a, **kw: _FakeClient(
            response=_FakeResponse(
                status_code=200,
                body_json={"artifacts": [{"base64": "AA=="}]},  # 1 byte after b64-decode
            )
        ),
    )
    ctx = ToolContext(workspace_root="", session_id="t", current_call_id="c")
    result = asyncio.run(ImageGenerateTool().run(_image_inputs(), ctx))
    assert result.is_error
    rows = _read_rows("image_generator")
    assert len(rows) == 1
    assert rows[0]["drift_kind"] == "uniform_output"
    assert rows[0]["source"] == "production_tripwire"
    assert rows[0]["evidence"]["cfg_scale"] == 3.5


def test_image_generate_http_4xx_writes_tripwire(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tesseract.kernel.tools.base import ToolContext
    from tesseract.kernel.tools.image_generate import ImageGenerateTool

    _patch_image_load_config(monkeypatch)
    monkeypatch.setattr(
        "tesseract.kernel.tools.image_generate.httpx.AsyncClient",
        lambda *a, **kw: _FakeClient(
            response=_FakeResponse(status_code=503, body_text="upstream choked")
        ),
    )
    ctx = ToolContext(workspace_root="", session_id="t", current_call_id="c")
    result = asyncio.run(ImageGenerateTool().run(_image_inputs(), ctx))
    assert result.is_error
    rows = _read_rows("image_generator")
    assert len(rows) == 1
    assert rows[0]["drift_kind"] == "http_error"
    assert rows[0]["evidence"]["status_code"] == 503


def test_image_generate_shape_mismatch_writes_tripwire(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tesseract.kernel.tools.base import ToolContext
    from tesseract.kernel.tools.image_generate import ImageGenerateTool

    _patch_image_load_config(monkeypatch)
    # 200 OK, JSON parses but doesn't match expected shape.
    monkeypatch.setattr(
        "tesseract.kernel.tools.image_generate.httpx.AsyncClient",
        lambda *a, **kw: _FakeClient(
            response=_FakeResponse(status_code=200, body_json={"unknown_envelope": True})
        ),
    )
    ctx = ToolContext(workspace_root="", session_id="t", current_call_id="c")
    result = asyncio.run(ImageGenerateTool().run(_image_inputs(), ctx))
    assert result.is_error
    rows = _read_rows("image_generator")
    assert len(rows) == 1
    assert rows[0]["drift_kind"] == "shape_mismatch"

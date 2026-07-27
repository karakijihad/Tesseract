"""AU-14 Session 14a — concrete probe coverage.

Per-probe tests for image / chat / embedding. Drift-kind classifier
correctness is the load-bearing piece — the AU-5 mapper buckets agenda
items by this field.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from tesseract.scheduler.tasks._probes.base import ProbeResult
from tesseract.scheduler.tasks._probes.chat_role import ChatRoleProbe
from tesseract.scheduler.tasks._probes.embedding_role import EmbeddingRoleProbe
from tesseract.scheduler.tasks._probes.image_role import ImageRoleProbe


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    return tmp_path


# ── Image probe ────────────────────────────────────────────


@dataclass
class _FakeToolResult:
    output: str
    is_error: bool = False
    metadata: dict[str, Any] | None = None


class _StubImageTool:
    def __init__(self, result: _FakeToolResult) -> None:
        self._result = result
        self.calls: list[Any] = []

    async def run(self, inp: Any, ctx: Any) -> _FakeToolResult:
        self.calls.append((inp, ctx))
        return self._result


def test_image_probe_healthy_returns_ok(isolated_home: Path) -> None:
    tool = _StubImageTool(
        _FakeToolResult(
            output="/api/downloads/anon/flux_abc.jpg",
            is_error=False,
            metadata={"size_bytes": 120_000, "mime_type": "image/jpeg"},
        )
    )
    probe = ImageRoleProbe(tool=tool)
    result = asyncio.run(probe.probe("image_generator", "api.nim.flux1_dev"))

    assert result.ok is True
    assert result.drift_kind == "none"
    assert result.evidence["size_bytes"] == 120_000
    assert result.evidence["mime_type"] == "image/jpeg"
    assert tool.calls, "tool must be invoked"
    inp = tool.calls[0][0]
    # Phase doc nails the probe shape: 512×512, cfg_scale=3.5, steps=10
    assert inp.width == 512 and inp.height == 512
    assert inp.cfg_scale == 3.5
    assert inp.steps == 10
    assert inp.model_role == "image_generator"


def test_image_probe_uniform_classifies_uniform_output(isolated_home: Path) -> None:
    tool = _StubImageTool(
        _FakeToolResult(
            output=(
                "image_generator returned what looks like a uniform "
                "(black / single-color) frame (2048 bytes ...)"
            ),
            is_error=True,
            metadata={"image_bytes": 2048, "cfg_scale": 3.5},
        )
    )
    probe = ImageRoleProbe(tool=tool)
    result = asyncio.run(probe.probe("image_generator", "api.nim.flux1_dev"))

    assert result.ok is False
    assert result.drift_kind == "uniform_output"
    assert result.evidence["metadata"]["image_bytes"] == 2048


def test_image_probe_http_error_classifies_http_error(isolated_home: Path) -> None:
    tool = _StubImageTool(
        _FakeToolResult(
            output="image_generator HTTP error: timeout",
            is_error=True,
            metadata={},
        )
    )
    probe = ImageRoleProbe(tool=tool)
    result = asyncio.run(probe.probe("image_generator", "api.nim.flux1_dev"))

    assert result.ok is False
    assert result.drift_kind == "http_error"


def test_image_probe_unavailable_classifies_unavailable(isolated_home: Path) -> None:
    tool = _StubImageTool(
        _FakeToolResult(
            output="image_generator role unavailable: env var NIM_API_KEY not set",
            is_error=True,
            metadata={},
        )
    )
    probe = ImageRoleProbe(tool=tool)
    result = asyncio.run(probe.probe("image_generator", "api.nim.flux1_dev"))

    assert result.ok is False
    assert result.drift_kind == "unavailable"


def test_image_probe_exception_classifies_http_error(isolated_home: Path) -> None:
    class _Crashes:
        async def run(self, inp: Any, ctx: Any) -> Any:  # noqa: ARG002
            raise RuntimeError("network down")

    probe = ImageRoleProbe(tool=_Crashes())
    result = asyncio.run(probe.probe("image_generator", "api.nim.flux1_dev"))

    assert result.ok is False
    assert result.drift_kind == "http_error"
    assert "network down" in result.evidence["exception"]


# ── Chat probe ─────────────────────────────────────────────


class _FakeAdapter:
    def __init__(self, response: str) -> None:
        self._response = response

    async def generate(self, prompt: str, options: Any) -> str:  # noqa: ARG002
        return self._response


class _SlowAdapter:
    async def generate(self, prompt: str, options: Any) -> str:  # noqa: ARG002
        await asyncio.sleep(10.0)
        return "should never see this"


class _CrashAdapter:
    async def generate(self, prompt: str, options: Any) -> str:  # noqa: ARG002
        raise RuntimeError("auth_failed")


class _BudgetExhaustedAdapter:
    async def generate(self, prompt: str, options: Any) -> str:  # noqa: ARG002
        from tesseract.brain.cost.ledger import BudgetExhausted

        raise BudgetExhausted("chat_brain", 5.0, 5.0, "role")


def _chain_builder(adapter: Any) -> Any:
    def _build(role_name: str, *, log_label: str = "test", cost_ledger: Any = None) -> list[tuple[Any, Any]]:  # noqa: ARG001
        if adapter is None:
            return []
        options = type("Opts", (), {"extra": {"timeout_seconds": 0.1}})()
        return [(adapter, options)]
    return _build


def test_chat_probe_healthy_returns_ok(isolated_home: Path) -> None:
    probe = ChatRoleProbe(chain_builder=_chain_builder(_FakeAdapter("pong")))
    result = asyncio.run(probe.probe("chat_brain", "api.openai.gpt54_nano"))

    assert result.ok is True
    assert result.drift_kind == "none"
    assert result.evidence["sample"] == "pong"


def test_chat_probe_empty_response_classifies_empty_output(isolated_home: Path) -> None:
    probe = ChatRoleProbe(chain_builder=_chain_builder(_FakeAdapter("   ")))
    result = asyncio.run(probe.probe("chat_brain", "api.openai.gpt54_nano"))

    assert result.ok is False
    assert result.drift_kind == "empty_output"


def test_chat_probe_timeout_classifies_latency_spike(isolated_home: Path) -> None:
    probe = ChatRoleProbe(chain_builder=_chain_builder(_SlowAdapter()))
    result = asyncio.run(probe.probe("chat_brain", "api.openai.gpt54_nano"))

    assert result.ok is False
    assert result.drift_kind == "latency_spike"
    assert "timeout_seconds" in result.evidence


def test_chat_probe_exception_classifies_http_error(isolated_home: Path) -> None:
    probe = ChatRoleProbe(chain_builder=_chain_builder(_CrashAdapter()))
    result = asyncio.run(probe.probe("chat_brain", "api.openai.gpt54_nano"))

    assert result.ok is False
    assert result.drift_kind == "http_error"
    assert "auth_failed" in result.evidence["exception"]


def test_chat_probe_budget_exhausted_is_not_provider_drift(isolated_home: Path) -> None:
    # The metered probe hits its own daily cap → BudgetExhausted. That's a
    # self-imposed spend limit, not a provider fault, so it must NOT surface as
    # http_error drift (which would trigger a false provider_health proposal).
    probe = ChatRoleProbe(chain_builder=_chain_builder(_BudgetExhaustedAdapter()))
    result = asyncio.run(probe.probe("chat_brain", "api.openai.gpt54_nano"))

    assert result.ok is True
    assert result.drift_kind == "none"
    assert result.evidence["skipped"] == "budget_exhausted"


def test_chat_probe_no_chain_classifies_unavailable(isolated_home: Path) -> None:
    probe = ChatRoleProbe(chain_builder=_chain_builder(None))
    result = asyncio.run(probe.probe("chat_brain", "api.openai.gpt54_nano"))

    assert result.ok is False
    assert result.drift_kind == "unavailable"


# ── Embedding probe ────────────────────────────────────────


def test_embedding_probe_healthy_returns_ok(isolated_home: Path) -> None:
    async def _embed(text: str) -> list[float]:  # noqa: ARG001
        return [0.1] * 768

    probe = EmbeddingRoleProbe(embed_fn=_embed)
    # ref is real so _expected_dim resolves to 768; healthy match.
    result = asyncio.run(probe.probe("embeddings", "local.ollama.nomic_embed"))

    assert result.ok is True
    assert result.drift_kind == "none"
    assert result.evidence["actual_dim"] == 768


def test_embedding_probe_wrong_dim_classifies_shape_mismatch(isolated_home: Path) -> None:
    async def _embed(text: str) -> list[float]:  # noqa: ARG001
        return [0.1] * 512  # nomic_embed is 768; 512 = drift

    probe = EmbeddingRoleProbe(embed_fn=_embed)
    result = asyncio.run(probe.probe("embeddings", "local.ollama.nomic_embed"))

    assert result.ok is False
    assert result.drift_kind == "shape_mismatch"
    assert result.evidence["actual_dim"] == 512
    assert result.evidence["expected_dim"] == 768


def test_embedding_probe_empty_vector_classifies_empty_output(isolated_home: Path) -> None:
    async def _embed(text: str) -> list[float]:  # noqa: ARG001
        return []

    probe = EmbeddingRoleProbe(embed_fn=_embed)
    result = asyncio.run(probe.probe("embeddings", "local.ollama.nomic_embed"))

    assert result.ok is False
    assert result.drift_kind == "empty_output"


def test_embedding_probe_exception_classifies_http_error(isolated_home: Path) -> None:
    async def _embed(text: str) -> list[float]:  # noqa: ARG001
        raise ConnectionError("ollama down")

    probe = EmbeddingRoleProbe(embed_fn=_embed)
    result = asyncio.run(probe.probe("embeddings", "local.ollama.nomic_embed"))

    assert result.ok is False
    assert result.drift_kind == "http_error"


def test_embedding_probe_non_sized_classifies_shape_mismatch(isolated_home: Path) -> None:
    async def _embed(text: str) -> Any:  # noqa: ARG001
        return None

    probe = EmbeddingRoleProbe(embed_fn=_embed)
    result = asyncio.run(probe.probe("embeddings", "local.ollama.nomic_embed"))

    assert result.ok is False
    assert result.drift_kind == "shape_mismatch"


def test_probe_result_dataclass_serializes_for_jsonl() -> None:
    """``ProbeResult`` rows must asdict-round-trip — provider_health
    writes via ``json.dumps(asdict(result))``."""
    import json
    from dataclasses import asdict

    result = ProbeResult(
        role="r",
        ref="api.x.y",
        ok=True,
        drift_kind="none",
        evidence={"k": "v"},
        probed_at="2026-05-18T00:00:00+00:00",
        latency_ms=42.0,
    )
    row = json.loads(json.dumps(asdict(result)))
    assert row["role"] == "r"
    assert row["ok"] is True
    assert row["drift_kind"] == "none"
    assert row["evidence"] == {"k": "v"}

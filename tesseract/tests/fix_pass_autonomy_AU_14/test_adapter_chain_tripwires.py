"""AU-14 Session 14b — FallbackAdapter HARD-error tripwire wiring.

A HARD pre-commit error against any chain entry MUST write a
``production_tripwire`` row to provider-health JSONL keyed on
``options.role`` + ``{tier}.{provider}.{model}``. An empty-output
generator exit writes an ``empty_output`` row. Happy paths must NOT
write any row.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from tesseract.brain.adapter_chain import FallbackAdapter
from tesseract.kernel.adapters.base import AdapterOptions, ChunkType, ErrorKind, StreamChunk
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


def _opts(model: str, provider: str = "openai") -> AdapterOptions:
    return AdapterOptions(role="chat_brain", provider=provider, model=model, tier="api")


class _StubAdapter:
    def __init__(self, mode: str, *, kind: ErrorKind = ErrorKind.HARD, text: str = "ok") -> None:
        self._mode = mode
        self._kind = kind
        self._text = text

    async def stream(self, *, messages, tools, options):
        if self._mode == "hard_error":
            yield StreamChunk(
                type=ChunkType.ERROR,
                error="AuthenticationError: invalid api key",
                error_kind=self._kind,
            )
            return
        if self._mode == "empty":
            return
            yield  # type: ignore[unreachable]
        if self._mode == "ok":
            yield StreamChunk(type=ChunkType.TEXT, text=self._text)
            yield StreamChunk(type=ChunkType.STOP, stop_reason="end")
            return
        raise RuntimeError(f"bad mode {self._mode}")

    def count_tokens(self, messages: list[dict[str, Any]]) -> int:
        return 0

    async def check_available(self) -> bool:
        return True


async def _consume(fa: FallbackAdapter) -> list[StreamChunk]:
    return [c async for c in fa.stream(messages=[], tools=None, options=None)]


def test_hard_pre_commit_writes_production_tripwire(isolated_home: Path) -> None:
    chain = [
        (_StubAdapter("hard_error"), _opts("gpt54_nano")),
        (_StubAdapter("ok", text="from fallback"), _opts("gpt54_mini")),
    ]
    fa = FallbackAdapter(chain, transient_retries=0, transient_backoff_ms=0)
    chunks = asyncio.run(_consume(fa))

    # The chain advanced + the fallback served; the tripwire row is the
    # only thing we care about here.
    assert any(c.type == ChunkType.TEXT for c in chunks)
    rows = _read_rows("chat_brain")
    assert len(rows) == 1
    row = rows[0]
    assert row["source"] == "production_tripwire"
    assert row["ref"] == "api.openai.gpt54_nano"  # the FAILING entry, not the fallback
    assert row["drift_kind"] == "unavailable"     # "AuthenticationError…invalid api key"
    assert "AuthenticationError" in row["evidence"]["error"]
    assert row["evidence"]["chain_index"] == 0


def test_transient_pre_commit_no_tripwire(isolated_home: Path) -> None:
    """TRANSIENT errors retry within the entry; they are not drift signals."""
    chain = [
        (_StubAdapter("hard_error", kind=ErrorKind.TRANSIENT), _opts("gpt54_nano")),
        (_StubAdapter("ok", text="t"), _opts("gpt54_mini")),
    ]
    fa = FallbackAdapter(chain, transient_retries=1, transient_backoff_ms=0)
    asyncio.run(_consume(fa))
    # Adapter exhausted TRANSIENT retries → advanced. No tripwire row;
    # the policy is "HARD only" so the JSONL stays out of operator noise.
    rows = _read_rows("chat_brain")
    assert rows == []


def test_empty_output_writes_production_tripwire(isolated_home: Path) -> None:
    chain = [
        (_StubAdapter("empty"), _opts("gpt54_nano")),
        (_StubAdapter("ok", text="from fallback"), _opts("gpt54_mini")),
    ]
    fa = FallbackAdapter(chain, transient_retries=0, transient_backoff_ms=0)
    asyncio.run(_consume(fa))
    rows = _read_rows("chat_brain")
    assert len(rows) == 1
    assert rows[0]["drift_kind"] == "empty_output"
    assert rows[0]["ref"] == "api.openai.gpt54_nano"


def test_happy_path_no_tripwire(isolated_home: Path) -> None:
    chain = [
        (_StubAdapter("ok", text="primary"), _opts("gpt54_nano")),
    ]
    fa = FallbackAdapter(chain, transient_retries=0, transient_backoff_ms=0)
    chunks = asyncio.run(_consume(fa))
    assert any(c.type == ChunkType.TEXT for c in chunks)
    assert _read_rows("chat_brain") == []


def test_raised_exception_classified_hard_writes_tripwire(isolated_home: Path) -> None:
    """An adapter that *raises* a HARD-classified exception (rather than
    yielding an ERROR chunk) must still fire the tripwire. The chain's
    exception path classifies the exception and the same HARD branch
    runs."""

    class _RaisesAuthError:
        async def stream(self, *, messages, tools, options):  # noqa: ARG002
            class AuthenticationError(Exception):
                pass
            raise AuthenticationError("invalid api key")
            yield  # pragma: no cover — generator marker

        def count_tokens(self, m): return 0  # noqa: ARG002
        async def check_available(self): return True

    chain = [
        (_RaisesAuthError(), _opts("gpt54_nano")),
        (_StubAdapter("ok", text="t"), _opts("gpt54_mini")),
    ]
    fa = FallbackAdapter(chain, transient_retries=0, transient_backoff_ms=0)
    asyncio.run(_consume(fa))
    rows = _read_rows("chat_brain")
    assert len(rows) == 1
    assert rows[0]["drift_kind"] == "unavailable"  # auth keywords win the tie-break
    assert rows[0]["ref"] == "api.openai.gpt54_nano"
    assert "invalid api key" in rows[0]["evidence"]["error"]


def test_hard_classifier_buckets_to_shape_mismatch(isolated_home: Path) -> None:
    """A HARD error whose text mentions JSON / shape / schema buckets as
    ``shape_mismatch`` so the AU-5 mapper can route it differently."""
    class _ShapeAdapter:
        async def stream(self, *, messages, tools, options):
            yield StreamChunk(
                type=ChunkType.ERROR,
                error="UnprocessableEntityError: invalid JSON envelope",
                error_kind=ErrorKind.HARD,
            )

        def count_tokens(self, m): return 0
        async def check_available(self): return True

    chain = [
        (_ShapeAdapter(), _opts("gpt54_nano")),
        (_StubAdapter("ok", text="t"), _opts("gpt54_mini")),
    ]
    fa = FallbackAdapter(chain, transient_retries=0, transient_backoff_ms=0)
    asyncio.run(_consume(fa))
    rows = _read_rows("chat_brain")
    assert len(rows) == 1
    assert rows[0]["drift_kind"] == "shape_mismatch"

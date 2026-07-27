"""AU-14 — ProviderProbeJob orchestrator dispatch + bus publication."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from tesseract.orchestrator import provider_health as ph
from tesseract.scheduler.tasks._probes.base import ProbeResult
from tesseract.scheduler.tasks.provider_probe import ProviderProbeJob
from tesseract.scheduler.types import JobContext


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    return tmp_path


# ── Builders ────────────────────────────────────────────────


@dataclass
class _FakeConn:
    tier: str = "api"
    name: str = "openai"
    tier_enabled: bool = True
    enabled: bool = True
    timeout_seconds: float = 30.0
    max_retries: int = 1
    base_url: str = ""


@dataclass
class _FakeModel:
    id: str
    model: str
    kind: str
    fields: dict[str, Any]


@dataclass
class _FakeRef:
    ref: str
    connection: _FakeConn
    model: _FakeModel


@dataclass
class _FakeRole:
    name: str
    mode: str
    primary: _FakeRef | None


@dataclass
class _FakeBundle:
    roles: dict[str, _FakeRole]
    embeddings: _FakeRef | None = None


def _mk_role(name: str, *, kind: str, mode: str = "active", enabled: bool = True) -> _FakeRole:
    if mode != "active":
        return _FakeRole(name=name, mode=mode, primary=None)
    return _FakeRole(
        name=name,
        mode=mode,
        primary=_FakeRef(
            ref=f"api.fake.{name}",
            connection=_FakeConn(enabled=enabled),
            model=_FakeModel(id=name, model=name, kind=kind, fields={"dimensions": 768}),
        ),
    )


class _RecordingProbe:
    """Implements the ``RoleProbe`` Protocol for tests.

    Returns whatever fixed ``ProbeResult`` it was constructed with, and
    captures the (role, ref) it was called for so the dispatch test can
    assert per-kind routing.
    """

    def __init__(self, *, kind: str, ok: bool, drift: str = "none") -> None:
        self.role_kind = kind
        self._ok = ok
        self._drift = drift
        self.calls: list[tuple[str, str]] = []

    async def probe(self, role_name: str, ref: str) -> ProbeResult:
        self.calls.append((role_name, ref))
        return ProbeResult(
            role=role_name,
            ref=ref,
            ok=self._ok,
            drift_kind=self._drift if not self._ok else "none",
            evidence={"k": "v"},
            probed_at=datetime.now(timezone.utc).isoformat(),
            latency_ms=1.0,
        )


# ── Tests ───────────────────────────────────────────────────


def test_orchestrator_dispatches_by_kind(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = _FakeBundle(
        roles={
            "chat_brain": _mk_role("chat_brain", kind="chat"),
            "image_generator": _mk_role("image_generator", kind="image_generation"),
            "deprecated": _mk_role("deprecated", kind="chat", mode="inactive"),
        },
        embeddings=_FakeRef(
            ref="local.ollama.nomic_embed",
            connection=_FakeConn(tier="local", name="ollama"),
            model=_FakeModel(
                id="nomic_embed",
                model="nomic-embed-text",
                kind="embedding",
                fields={"dimensions": 768},
            ),
        ),
    )
    monkeypatch.setattr(
        "tesseract.scheduler.tasks.provider_probe._load_bundle_safely",
        lambda: bundle,
    )

    chat_probe = _RecordingProbe(kind="chat", ok=True)
    image_probe = _RecordingProbe(kind="image_generation", ok=True)
    embedding_probe = _RecordingProbe(kind="embedding", ok=True)
    monkeypatch.setattr(
        "tesseract.scheduler.tasks.provider_probe._build_probes",
        lambda ctx, bundle: {
            "chat": chat_probe,
            "image_generation": image_probe,
            "embedding": embedding_probe,
        },
    )

    captured: list[ProbeResult] = []

    def _capture(result: ProbeResult) -> None:
        captured.append(result)

    job = ProviderProbeJob()
    ctx = JobContext(
        job_name="provider_probe",
        run_id="r1",
        config={"publisher": _capture},
    )
    result = asyncio.run(job.run(ctx))

    assert result.ok is True
    assert result.payload["probed"] == 3
    assert sorted(c[0] for c in chat_probe.calls) == ["chat_brain"]
    assert sorted(c[0] for c in image_probe.calls) == ["image_generator"]
    assert sorted(c[0] for c in embedding_probe.calls) == ["embeddings"]
    # Inactive role never reached a probe.
    for p in (chat_probe, image_probe, embedding_probe):
        assert not any(name == "deprecated" for name, _ in p.calls)


def test_orchestrator_publishes_only_drift_rows(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = _FakeBundle(
        roles={
            "chat_brain": _mk_role("chat_brain", kind="chat"),
            "image_generator": _mk_role("image_generator", kind="image_generation"),
        },
        embeddings=None,
    )
    monkeypatch.setattr(
        "tesseract.scheduler.tasks.provider_probe._load_bundle_safely",
        lambda: bundle,
    )
    monkeypatch.setattr(
        "tesseract.scheduler.tasks.provider_probe._build_probes",
        lambda ctx, bundle: {
            "chat": _RecordingProbe(kind="chat", ok=True),
            "image_generation": _RecordingProbe(
                kind="image_generation", ok=False, drift="uniform_output"
            ),
        },
    )

    seen: list[ProbeResult] = []

    def _pub(result: ProbeResult) -> None:
        seen.append(result)

    job = ProviderProbeJob()
    ctx = JobContext(job_name="provider_probe", run_id="r1", config={"publisher": _pub})
    asyncio.run(job.run(ctx))

    assert len(seen) == 1
    assert seen[0].role == "image_generator"
    assert seen[0].drift_kind == "uniform_output"


def test_orchestrator_writes_jsonl_per_role(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = _FakeBundle(
        roles={
            "chat_brain": _mk_role("chat_brain", kind="chat"),
        },
        embeddings=None,
    )
    monkeypatch.setattr(
        "tesseract.scheduler.tasks.provider_probe._load_bundle_safely",
        lambda: bundle,
    )
    monkeypatch.setattr(
        "tesseract.scheduler.tasks.provider_probe._build_probes",
        lambda ctx, bundle: {"chat": _RecordingProbe(kind="chat", ok=True)},
    )

    job = ProviderProbeJob()
    ctx = JobContext(job_name="provider_probe", run_id="r1", config={"publisher": lambda r: None})
    asyncio.run(job.run(ctx))

    path = ph.provider_health_dir() / "chat_brain.jsonl"
    assert path.exists()
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(rows) == 1
    assert rows[0]["role"] == "chat_brain"


def test_orchestrator_skips_unknown_kind(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = _FakeBundle(
        roles={
            "audio_role": _mk_role("audio_role", kind="audio_stt"),  # no probe for this kind
        },
        embeddings=None,
    )
    monkeypatch.setattr(
        "tesseract.scheduler.tasks.provider_probe._load_bundle_safely",
        lambda: bundle,
    )
    monkeypatch.setattr(
        "tesseract.scheduler.tasks.provider_probe._build_probes",
        lambda ctx, bundle: {"chat": _RecordingProbe(kind="chat", ok=True)},
    )

    job = ProviderProbeJob()
    ctx = JobContext(job_name="provider_probe", run_id="r1", config={"publisher": lambda r: None})
    result = asyncio.run(job.run(ctx))

    assert result.payload["probed"] == 0
    skipped_roles = [s["role"] for s in result.payload["skipped"]]
    assert "audio_role" in skipped_roles


def test_orchestrator_skips_disabled_provider(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = _FakeBundle(
        roles={
            "chat_brain": _mk_role("chat_brain", kind="chat", enabled=False),
        },
        embeddings=None,
    )
    monkeypatch.setattr(
        "tesseract.scheduler.tasks.provider_probe._load_bundle_safely",
        lambda: bundle,
    )
    chat_probe = _RecordingProbe(kind="chat", ok=True)
    monkeypatch.setattr(
        "tesseract.scheduler.tasks.provider_probe._build_probes",
        lambda ctx, bundle: {"chat": chat_probe},
    )

    job = ProviderProbeJob()
    ctx = JobContext(job_name="provider_probe", run_id="r1", config={"publisher": lambda r: None})
    result = asyncio.run(job.run(ctx))

    assert chat_probe.calls == []
    assert result.payload["probed"] == 0


def test_orchestrator_drift_keeps_job_ok_true(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Per phase-doc §2: a drift event is *expected* — the job's
    JobResult.ok stays True so the scheduler doesn't retry."""
    bundle = _FakeBundle(
        roles={
            "chat_brain": _mk_role("chat_brain", kind="chat"),
        },
        embeddings=None,
    )
    monkeypatch.setattr(
        "tesseract.scheduler.tasks.provider_probe._load_bundle_safely",
        lambda: bundle,
    )
    monkeypatch.setattr(
        "tesseract.scheduler.tasks.provider_probe._build_probes",
        lambda ctx, bundle: {
            "chat": _RecordingProbe(kind="chat", ok=False, drift="http_error"),
        },
    )

    job = ProviderProbeJob()
    ctx = JobContext(job_name="provider_probe", run_id="r1", config={"publisher": lambda r: None})
    result = asyncio.run(job.run(ctx))

    assert result.ok is True
    assert result.payload["failures"]
    assert result.payload["failures"][0]["drift_kind"] == "http_error"


def test_orchestrator_handles_load_config_failure(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom() -> Any:
        raise RuntimeError("yaml busted")

    monkeypatch.setattr(
        "tesseract.scheduler.tasks.provider_probe._load_bundle_safely",
        _boom,
    )

    job = ProviderProbeJob()
    ctx = JobContext(job_name="provider_probe", run_id="r1")
    result = asyncio.run(job.run(ctx))
    assert result.ok is False
    assert "load_config failed" in result.detail


def test_orchestrator_default_publisher_routes_to_bus(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ``ctx.config`` has no ``publisher`` key, the orchestrator
    routes drift rows through ``publish_to_bus``. We swap that module
    function and confirm it was called for the failing probe."""
    bundle = _FakeBundle(
        roles={"chat_brain": _mk_role("chat_brain", kind="chat")},
        embeddings=None,
    )
    monkeypatch.setattr(
        "tesseract.scheduler.tasks.provider_probe._load_bundle_safely",
        lambda: bundle,
    )
    monkeypatch.setattr(
        "tesseract.scheduler.tasks.provider_probe._build_probes",
        lambda ctx, bundle: {
            "chat": _RecordingProbe(kind="chat", ok=False, drift="http_error"),
        },
    )

    bus_calls: list[tuple[Any, dict[str, Any]]] = []

    def _fake_publish(source: Any, payload: dict[str, Any], *, event_id: str | None = None) -> None:  # noqa: ARG001
        bus_calls.append((source, payload))

    monkeypatch.setattr(
        "tesseract.scheduler.tasks.provider_probe.publish_to_bus",
        _fake_publish,
    )

    job = ProviderProbeJob()
    ctx = JobContext(job_name="provider_probe", run_id="r1")
    asyncio.run(job.run(ctx))

    assert len(bus_calls) == 1
    src, payload = bus_calls[0]
    assert payload["kind"] == "provider_health"
    assert payload["role"] == "chat_brain"
    assert payload["drift_kind"] == "http_error"


def test_probe_crash_row_is_visible_to_rolling_window(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Crash-path rows must carry a parseable ``probed_at`` so
    ``provider_health.rolling_window`` returns them — otherwise AU-5's
    mapper never sees the worst kind of drift."""
    bundle = _FakeBundle(
        roles={"chat_brain": _mk_role("chat_brain", kind="chat")},
        embeddings=None,
    )
    monkeypatch.setattr(
        "tesseract.scheduler.tasks.provider_probe._load_bundle_safely",
        lambda: bundle,
    )

    class _Crashes:
        role_kind = "chat"

        async def probe(self, role_name: str, ref: str) -> ProbeResult:  # noqa: ARG002
            raise RuntimeError("kaboom-rw")

    monkeypatch.setattr(
        "tesseract.scheduler.tasks.provider_probe._build_probes",
        lambda ctx, bundle: {"chat": _Crashes()},
    )

    job = ProviderProbeJob()
    ctx = JobContext(job_name="provider_probe", run_id="r1", config={"publisher": lambda r: None})
    asyncio.run(job.run(ctx))

    rows = ph.rolling_window("chat_brain", days=7)
    assert len(rows) == 1
    assert rows[0]["drift_kind"] == "http_error"
    assert "kaboom-rw" in rows[0]["evidence"]["exception"]


def test_probe_crash_writes_failure_row(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A probe that raises must be caught and bucketed as an
    ``http_error`` drift row — not propagate."""
    bundle = _FakeBundle(
        roles={"chat_brain": _mk_role("chat_brain", kind="chat")},
        embeddings=None,
    )
    monkeypatch.setattr(
        "tesseract.scheduler.tasks.provider_probe._load_bundle_safely",
        lambda: bundle,
    )

    class _Crashes:
        role_kind = "chat"

        async def probe(self, role_name: str, ref: str) -> ProbeResult:  # noqa: ARG002
            raise RuntimeError("kaboom")

    monkeypatch.setattr(
        "tesseract.scheduler.tasks.provider_probe._build_probes",
        lambda ctx, bundle: {"chat": _Crashes()},
    )

    job = ProviderProbeJob()
    ctx = JobContext(job_name="provider_probe", run_id="r1", config={"publisher": lambda r: None})
    result = asyncio.run(job.run(ctx))

    assert result.payload["probed"] == 1
    failures = result.payload["failures"]
    assert len(failures) == 1
    assert failures[0]["drift_kind"] == "http_error"
    assert "kaboom" in failures[0]["evidence"]["exception"]

"""Vault lint contradict-pass must let a rehydrated-tripped breaker probe.

Without this, the rehydrate fix from `test_breaker_rehydrate.py` would
self-defeat: a new process loads the breaker as tripped, the for-loop
short-circuits at the first iteration, no LLM call is made, no
record_success() is called, and no "reset" event ever lands in the
JSONL. The breaker stays open across every subsequent run and the
conscience signal is stuck `warn` forever.

The fix counts failures *this run* instead of using the cross-run
`is_tripped` flag as the loop guard. The first call goes through as a
half-open probe; success heals (record_success writes "reset" because
is_tripped=True from rehydrate), failure leaves state honest.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from tesseract.brain.boot import VaultConfig
from tesseract.context.circuit_breaker import CircuitBreaker
from tesseract.kernel.adapters.base import AdapterOptions
from tesseract.memory.vault_lint import VaultLinter
from tesseract.memory.vault_manager import VaultManager


@dataclass
class _StubAdapter:
    responses: list[str] = field(default_factory=list)
    raises: Exception | None = None
    call_count: int = 0

    async def generate(self, prompt: str, options: AdapterOptions) -> str:
        self.call_count += 1
        if self.raises is not None:
            raise self.raises
        if not self.responses:
            return '{"verdict": "reinforce", "reason": ""}'
        idx = min(self.call_count - 1, len(self.responses) - 1)
        return self.responses[idx]

    async def stream(self, *args, **kwargs):  # pragma: no cover
        raise NotImplementedError


def _seed_two_source_pages(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    (vault / "wiki").mkdir(parents=True)
    (vault / "raw").mkdir()
    (vault / "wiki" / "INDEX.md").write_text("# Index\n", encoding="utf-8")
    for slug in ("alpha", "beta"):
        (vault / "wiki" / f"{slug}.md").write_text(
            "---\n"
            f"title: {slug}\n"
            "type: Source\n"
            f"source_path: raw/{slug}.md\n"
            "date_added: 2026-04-22\n"
            "concepts: [shared-concept]\n"
            "entities: []\n"
            "related_slugs: [other]\n"
            "backlinks_from: [other]\n"
            "---\n\n"
            f"Body {slug}.\n\nSecond paragraph for {slug}.\n",
            encoding="utf-8",
        )
        (vault / "raw" / f"{slug}.md").write_text(f"raw {slug}\n", encoding="utf-8")
    return vault


def _build_linter(vault_root: Path, adapter, log_dir: Path) -> VaultLinter:
    agents_dir = Path(__file__).resolve().parents[2] / "agents"
    cfg = VaultConfig(
        max_extract_chars=3000,
        scale_split_threshold=80,
        stale_grace_days=180,
        contradiction_pair_limit=50,
        max_seed_slugs=6,
        max_expanded_slugs=12,
        search_rrf_k=60,
        search_default_top_k=5,
    )
    return VaultLinter(
        vault_manager=VaultManager(vault_root=vault_root),
        config=cfg,
        adapter=adapter,
        adapter_options=AdapterOptions(),
        log_dir=log_dir,
        agents_dir=agents_dir,
    )


def _write_tripped_jsonl(log_dir: Path) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / "vault_lint.jsonl"
    path.write_text(
        json.dumps({
            "event": "tripped",
            "breaker": "vault_lint",
            "failures": 3,
            "error": "stale upstream failure",
            "timestamp": "2026-05-01T00:00:00+00:00",
        }) + "\n",
        encoding="utf-8",
    )
    return path


async def test_rehydrated_tripped_breaker_heals_on_first_success(tmp_path: Path) -> None:
    vault = _seed_two_source_pages(tmp_path)
    log_dir = tmp_path / "breakers"
    log_path = _write_tripped_jsonl(log_dir)

    adapter = _StubAdapter(responses=['{"verdict": "weaken", "reason": "ok"}'])
    linter = _build_linter(vault, adapter, log_dir)

    assert linter._breaker.is_tripped is True

    report = await linter.run(dry_run=True)

    assert adapter.call_count == 1
    assert linter._breaker.is_tripped is False

    lines = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert lines[-1]["event"] == "reset"
    assert lines[-1]["breaker"] == "vault_lint"
    assert not any(f.startswith("contradict: breaker tripped") for f in report.failures)


async def test_rehydrated_tripped_breaker_stays_tripped_when_probe_fails(tmp_path: Path) -> None:
    vault = _seed_two_source_pages(tmp_path)
    log_dir = tmp_path / "breakers"
    log_path = _write_tripped_jsonl(log_dir)

    adapter = _StubAdapter(raises=RuntimeError("still broken"))
    linter = _build_linter(vault, adapter, log_dir)

    assert linter._breaker.is_tripped is True

    await linter.run(dry_run=True)

    assert adapter.call_count == 1
    assert linter._breaker.is_tripped is True

    lines = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert all(line["event"] != "reset" for line in lines)

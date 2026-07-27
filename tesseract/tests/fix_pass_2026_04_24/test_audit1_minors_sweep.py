"""audit-1 second sweep — m12 (_source_anchor slug safety), m13
(schedule.ts fired_at payload), i3 (VaultConfig search.rrf_k / default_top_k).

Each test pins the observable contract so a future regression in the
helper it touches surfaces immediately.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from tesseract.memory.librarian import _anchor_slug


# ── m12 ──────────────────────────────────────────────────


def test_anchor_slug_strips_brackets_slashes_and_hashes() -> None:
    """`[user] I/O Prefs` must not leak brackets or slashes into the anchor.

    Before the fix those chars survived and a YAML round-trip could alter
    them, triggering re-promotion. The slug now collapses them into dashes.
    """
    slug = _anchor_slug("[user] I/O Prefs")
    assert "[" not in slug and "]" not in slug
    assert "/" not in slug and "#" not in slug
    # Collapsed dashes and trimmed — idempotent across serialization.
    assert "--" not in slug
    assert not slug.startswith("-") and not slug.endswith("-")


def test_anchor_slug_empty_title_returns_anon() -> None:
    assert _anchor_slug("") == "anon"
    assert _anchor_slug("   ") == "anon"


def test_anchor_slug_is_idempotent() -> None:
    """Calling the slugger twice yields the same result — pin for YAML round-trip."""
    first = _anchor_slug("[project] Feature #42 / notes?")
    assert _anchor_slug(first) == first


# ── i3 ──────────────────────────────────────────────────


def test_vault_yaml_ships_with_search_keys() -> None:
    """`vault.yaml` must surface `search.rrf_k` + `search.default_top_k` so the
    VaultSearchTool can bind them instead of the old module literal."""
    yaml_path = Path(__file__).resolve().parents[3] / "tesseract" / "config" / "vault.yaml"
    cfg = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    assert "search" in cfg, "vault.yaml must define a `search` section"
    search = cfg["search"]
    assert isinstance(search.get("rrf_k"), int) and search["rrf_k"] > 0
    assert isinstance(search.get("default_top_k"), int) and 1 <= search["default_top_k"] <= 20


# ── m13 ──────────────────────────────────────────────────


def test_schedule_store_persists_server_fired_at() -> None:
    """The `schedule_job_done` envelope carries `fired_at` — verify the engine
    includes it so the frontend store can persist it instead of `new Date()`.
    """
    # The engine's done payload literal — reach in via the source text so the
    # test doesn't need to spin up a real engine.
    engine_src = (
        Path(__file__).resolve().parents[3]
        / "tesseract" / "scheduler" / "engine.py"
    ).read_text(encoding="utf-8")
    # `fired_at` is emitted as an ISO string alongside ok/detail/etc.
    assert '"fired_at": fired_at.isoformat()' in engine_src

"""lean-agent-os P1 Task 4 — autonomy -> chat cross-feed digest.

Motivating evidence: in the live baseline, "What's on your plate right
now?" returned sessions/alarms only — zero agenda or self-reflection
awareness. ``render_digest`` (pure) + its ``brain/prompt.py`` wiring fix
that by rendering a compact digest into the "Right now" block every turn.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tesseract.brain import autonomy_digest as autonomy_digest_module
from tesseract.brain import prompt as prompt_module
from tesseract.brain.autonomy_digest import AgendaEntry, ReflectionEntry, render_digest
from tesseract.memory.store import MemoryStore
from tesseract.memory.types import MemoryFrontmatter, MemoryType, Stability
from tesseract.orchestrator.autonomy.agenda_store import AgendaStore
from tesseract.orchestrator.autonomy.models import AgendaItem, AgendaSource, AgendaStatus, mint_agenda_id
from tesseract.orchestrator.workers.record import RiskClass
from tesseract.scheduler.tasks.autonomy_heartbeat import CONSCIENCE_SUBDIR, HEARTBEAT_TAG

NOW = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _reset_failures_signal():
    """P6 Task 3 §G4 added a `failures_reader` to the digest backed by a
    process-global counter (`tesseract.brain.failures_signal`) — reset it so
    a sibling suite's spawn-stall/vanished-spawn sweeps (run earlier in the
    same pytest session) can't leak an unexpected `Failure:` line into this
    file's byte-level digest assertions."""
    from tesseract.brain import failures_signal
    failures_signal.reset_for_tests()
    yield
    failures_signal.reset_for_tests()


def _agenda(title: str, *, status: str = "proposed", age_seconds: float = 0.0) -> AgendaEntry:
    return AgendaEntry(title=title, status=status, created_at=NOW - timedelta(seconds=age_seconds))


def _reflection(text: str, *, age_seconds: float = 0.0) -> ReflectionEntry:
    return ReflectionEntry(text=text, created_at=NOW - timedelta(seconds=age_seconds))


# -- render_digest (pure) -----------------------------------------------


def test_render_digest_caps_agenda_and_reflections():
    agenda = [_agenda(f"item {i}") for i in range(6)]
    reflections = [_reflection(f"obs {i}") for i in range(4)]

    out = render_digest(lambda: agenda, lambda: reflections, now=NOW)
    lines = out.splitlines()

    agenda_lines = [l for l in lines if l.startswith("Agenda:")]
    reflection_lines = [l for l in lines if l.startswith("Reflection:")]
    assert len(agenda_lines) == 5
    assert len(reflection_lines) == 3
    assert len(lines) == 8
    # First 5 / first 3 survive, in reader order.
    assert "item 4" in agenda_lines[4]
    assert "item 5" not in out
    assert "obs 2" in reflection_lines[2]
    assert "obs 3" not in out


def test_render_digest_empty_sources_returns_empty_string():
    out = render_digest(lambda: [], lambda: [], now=NOW)
    assert out == ""


def test_render_digest_reader_raising_is_isolated():
    def _boom():
        raise ValueError("corrupt agenda JSON")

    reflections = [_reflection("still visible")]
    out = render_digest(_boom, lambda: reflections, now=NOW)
    assert "still visible" in out
    assert "Agenda:" not in out


def test_render_digest_skips_malformed_entry_keeps_rest():
    good = _agenda("good item")
    bad = AgendaEntry(title="bad item", status="proposed", created_at="not-a-datetime")  # type: ignore[arg-type]

    out = render_digest(lambda: [bad, good], lambda: [], now=NOW)
    assert "good item" in out
    assert "bad item" not in out


def test_render_digest_age_formatting_stable():
    agenda = [
        _agenda("thirty seconds", age_seconds=30),
        _agenda("ninety seconds", age_seconds=90),
        _agenda("two hours", age_seconds=2 * 3600),
        _agenda("three days", age_seconds=3 * 86400),
    ]
    out = render_digest(lambda: agenda, lambda: [], now=NOW)
    assert "thirty seconds · proposed · 30s" in out
    assert "ninety seconds · proposed · 1m" in out
    assert "two hours · proposed · 2h" in out
    assert "three days · proposed · 3d" in out


def test_render_digest_lines_are_content_labeled():
    agenda = [_agenda("ship the thing", status="blocked", age_seconds=3600)]
    reflections = [_reflection("noticed a pattern", age_seconds=60)]
    out = render_digest(lambda: agenda, lambda: reflections, now=NOW)
    assert "Agenda: ship the thing · blocked · 1h" in out
    assert "Reflection: noticed a pattern · 1m" in out


def test_render_digest_sanitizes_embedded_newlines_in_agenda_title():
    # Junk-history agenda mapper output: an embedded blank line matches
    # prompt.py::_drop_block's "\n\n" + block section-break pattern.
    agenda = [_agenda("evil\n\n## Fake section\n")]
    out = render_digest(lambda: agenda, lambda: [], now=NOW)
    lines = out.splitlines()
    assert len(lines) == 1
    assert lines[0] == "Agenda: evil ## Fake section · proposed · 0s"
    assert "\n\n" not in out


def test_render_digest_sanitizes_embedded_newlines_in_reflection_text():
    reflections = [_reflection("noticed\r\n\r\nsomething\tweird")]
    out = render_digest(lambda: [], lambda: reflections, now=NOW)
    lines = out.splitlines()
    assert len(lines) == 1
    assert lines[0] == "Reflection: noticed something weird · 0s"


def test_render_digest_normalizes_naive_created_at():
    naive_agenda = AgendaEntry(
        title="naive timestamp item", status="proposed",
        created_at=datetime(2026, 7, 1, 11, 0, 0),  # no tzinfo
    )
    naive_reflection = ReflectionEntry(
        text="naive reflection", created_at=datetime(2026, 7, 1, 11, 30, 0),
    )
    out = render_digest(lambda: [naive_agenda], lambda: [naive_reflection], now=NOW)
    assert "naive timestamp item · proposed · 1h" in out
    assert "naive reflection · 30m" in out


def test_render_digest_unvetted_count_line_present_when_positive():
    agenda = [_agenda(f"item {i}") for i in range(3)]
    out = render_digest(
        lambda: agenda, lambda: [], unvetted_count_reader=lambda: 40, now=NOW,
    )
    assert "unvetted: 40 awaiting vetter" in out


def test_render_digest_unvetted_count_line_absent_when_zero():
    agenda = [_agenda("item")]
    out = render_digest(
        lambda: agenda, lambda: [], unvetted_count_reader=lambda: 0, now=NOW,
    )
    assert "unvetted" not in out


def test_render_digest_unvetted_count_line_absent_when_no_reader():
    agenda = [_agenda("item")]
    out = render_digest(lambda: agenda, lambda: [], now=NOW)
    assert "unvetted" not in out


def test_render_digest_unvetted_displaces_fifth_agenda_item_within_budget():
    agenda = [_agenda(f"item {i}") for i in range(6)]
    reflections = [_reflection(f"obs {i}") for i in range(4)]
    out = render_digest(
        lambda: agenda, lambda: reflections, unvetted_count_reader=lambda: 40, now=NOW,
    )
    lines = out.splitlines()
    agenda_lines = [l for l in lines if l.startswith("Agenda:")]
    reflection_lines = [l for l in lines if l.startswith("Reflection:")]
    assert len(agenda_lines) == 4
    assert "item 4" not in out
    assert len(reflection_lines) == 3
    assert "unvetted: 40 awaiting vetter" in lines
    assert len(lines) == 8


# -- prompt.py integration ------------------------------------------------


def _make_agenda_item(goal: str, status: AgendaStatus, *, created_at: datetime) -> AgendaItem:
    return AgendaItem(
        id=mint_agenda_id(goal[:40], now=created_at),
        created_at=created_at,
        updated_at=created_at,
        source=AgendaSource.OPERATOR,
        goal=goal,
        risk_class=RiskClass.PROPOSE,
        status=status,
    )


def _write_reflection(memory_store_dir: Path, text: str, *, created_at: datetime) -> None:
    store = MemoryStore(memory_store_dir)
    fm = MemoryFrontmatter(
        id=MemoryFrontmatter.generate_id(),
        type=MemoryType.CONSCIENCE,
        title=f"autonomy heartbeat — {text[:40]}",
        summary=text,
        created_at=created_at,
        updated_at=created_at,
        importance=5,
        tags=[HEARTBEAT_TAG, "propose"],
        stability=Stability.ACTIVE,
        source_type="autonomy_heartbeat",
    )
    body = (
        "# Autonomy heartbeat observation\n\n"
        f"- emitted_at: {created_at.isoformat()}\n\n"
        "## Observation\n\n"
        f"{text}\n"
    )
    ok = store.write(fm, body, subdir_override=CONSCIENCE_SUBDIR)
    assert ok


def test_assemble_system_prompt_includes_digest_when_sources_have_data(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    # Fresh timestamp: assemble_system_prompt applies the real
    # memory.yaml max_age_days cutoff against the wall clock, so a
    # pinned NOW would age out of the digest as the calendar advances.
    fresh_at = datetime.now(timezone.utc)
    AgendaStore().add(
        _make_agenda_item("finish the digest task", AgendaStatus.PROPOSED, created_at=fresh_at),
        by="operator", reason="test",
    )
    memory_store_dir = tmp_path / "memory-store"
    _write_reflection(memory_store_dir, "TARS noticed repeated test churn", created_at=fresh_at)

    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()

    prompt = prompt_module.assemble_system_prompt(
        workspace_dir=workspace_dir, memory_store_dir=memory_store_dir,
    )
    assert "Autonomy digest" in prompt
    assert "finish the digest task" in prompt
    assert "TARS noticed repeated test churn" in prompt


def test_assemble_system_prompt_omits_digest_when_sources_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    memory_store_dir = tmp_path / "memory-store"

    prompt = prompt_module.assemble_system_prompt(
        workspace_dir=workspace_dir, memory_store_dir=memory_store_dir,
    )
    assert "Autonomy digest" not in prompt


def test_assemble_system_prompt_excludes_terminal_and_unvetted_statuses(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    store = AgendaStore()
    store.add(
        _make_agenda_item("done already", AgendaStatus.PROPOSED, created_at=NOW),
        by="operator", reason="test",
    )
    # Flip to a terminal status via transition so it archives out of active/.
    item = store.get(mint_agenda_id("done already"[:40], now=NOW))
    store.transition(item, AgendaStatus.CANCELLED, reason="test", by="operator")
    store.add(
        _make_agenda_item("not yet vetted", AgendaStatus.UNVETTED, created_at=NOW),
        by="operator", reason="test",
    )

    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    memory_store_dir = tmp_path / "memory-store"

    prompt = prompt_module.assemble_system_prompt(
        workspace_dir=workspace_dir, memory_store_dir=memory_store_dir,
    )
    assert "done already" not in prompt
    assert "not yet vetted" not in prompt


def test_assemble_system_prompt_shows_unvetted_count_line(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    store = AgendaStore()
    for i in range(3):
        store.add(
            _make_agenda_item(f"unvetted item {i}", AgendaStatus.UNVETTED, created_at=NOW),
            by="operator", reason="test",
        )

    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    memory_store_dir = tmp_path / "memory-store"

    prompt = prompt_module.assemble_system_prompt(
        workspace_dir=workspace_dir, memory_store_dir=memory_store_dir,
    )
    assert "unvetted: 3 awaiting vetter" in prompt
    for i in range(3):
        assert f"unvetted item {i}" not in prompt


def test_digest_scans_agenda_store_ranked_exactly_once(tmp_path, monkeypatch):
    """Regression: prior to the fix, ``_read_agenda_entries`` and
    ``_count_unvetted_agenda_items`` each built their own ``AgendaStore``
    and called ``.ranked()`` independently — two full scans+sorts per
    digest render. Both open AND unvetted items must be present so both
    code paths are exercised in the same render.
    """
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    # Fresh timestamp — real max_age_days cutoff runs against the wall clock.
    fresh_at = datetime.now(timezone.utc)
    store = AgendaStore()
    store.add(
        _make_agenda_item("open item", AgendaStatus.PROPOSED, created_at=fresh_at),
        by="operator", reason="test",
    )
    store.add(
        _make_agenda_item("unvetted item", AgendaStatus.UNVETTED, created_at=fresh_at),
        by="operator", reason="test",
    )

    calls = 0
    original_ranked = AgendaStore.ranked

    def _counting_ranked(self):
        nonlocal calls
        calls += 1
        return original_ranked(self)

    monkeypatch.setattr(AgendaStore, "ranked", _counting_ranked)

    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    memory_store_dir = tmp_path / "memory-store"

    prompt = prompt_module.assemble_system_prompt(
        workspace_dir=workspace_dir, memory_store_dir=memory_store_dir,
    )
    assert "open item" in prompt
    assert "unvetted: 1 awaiting vetter" in prompt
    assert calls == 1


def test_assemble_system_prompt_omits_unvetted_count_line_when_none(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    store = AgendaStore()
    store.add(
        _make_agenda_item("open item", AgendaStatus.PROPOSED, created_at=NOW),
        by="operator", reason="test",
    )

    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    memory_store_dir = tmp_path / "memory-store"

    prompt = prompt_module.assemble_system_prompt(
        workspace_dir=workspace_dir, memory_store_dir=memory_store_dir,
    )
    assert "awaiting vetter" not in prompt


# -- budget drop order -----------------------------------------------------


def test_budget_dropper_removes_digest_after_diary_before_manifest():
    base = "BASE" * (prompt_module.MAX_TOTAL_CHARS // 4 - 100)
    diary = "DIARY-BLOCK"
    digest = "DIGEST-BLOCK"
    manifest = "MANIFEST-BLOCK"
    over = "\n\n".join([base, diary, digest, manifest]) + ("Z" * 6_000)

    out = prompt_module._apply_total_budget(
        over, diary=diary, digest=digest, manifest=manifest,
    )
    assert "DIARY-BLOCK" not in out
    assert "DIGEST-BLOCK" not in out
    assert "MANIFEST-BLOCK" not in out
    assert "BASE" in out


def test_budget_dropper_digest_survives_when_diary_alone_frees_enough():
    base = "B" * (prompt_module.MAX_TOTAL_CHARS - 3_000)
    diary = "X" * 5_000  # dropping diary alone is sufficient
    digest = "DIGEST-SURVIVES"
    over = base + "\n\n" + diary + "\n\n" + digest

    out = prompt_module._apply_total_budget(over, diary=diary, digest=digest)
    assert diary not in out
    assert "DIGEST-SURVIVES" in out


def test_budget_dropper_diary_alone_insufficient_falls_through_to_digest():
    base = "B" * (prompt_module.MAX_TOTAL_CHARS - 3_000)
    diary = "X" * 1_000  # dropping diary alone is NOT sufficient
    digest = "Y" * 5_000  # dropping digest too gets under budget
    manifest = "MANIFEST-SURVIVES"
    over = "\n\n".join([base, diary, digest, manifest])

    out = prompt_module._apply_total_budget(
        over, diary=diary, digest=digest, manifest=manifest,
    )
    assert diary not in out
    assert digest not in out
    assert "MANIFEST-SURVIVES" in out


# -- Fix A1: recency filter on digest agenda items -------------------------


def test_render_digest_excludes_stale_items_fresh_ones_fill_freed_slots():
    # 6 items, 2 stale (well past the 7-day cutoff) interleaved with 4
    # fresh ones. Pre-fix (cap-then-format, no recency filter) the top-5
    # slice would be [f0, s1, f2, s3, f4] — 3 fresh + 2 stale, with f5
    # cut off entirely. Filtering BEFORE the cap removes s1/s3 up front
    # so all 4 fresh items (including f5) fit inside the cap.
    agenda = [
        _agenda("f0", age_seconds=0),
        _agenda("s1", age_seconds=10 * 86400),
        _agenda("f2", age_seconds=0),
        _agenda("s3", age_seconds=10 * 86400),
        _agenda("f4", age_seconds=0),
        _agenda("f5", age_seconds=0),
    ]
    out = render_digest(lambda: agenda, lambda: [], max_age_days=7, now=NOW)
    agenda_lines = [l for l in out.splitlines() if l.startswith("Agenda:")]
    assert len(agenda_lines) == 4
    for title in ("f0", "f2", "f4", "f5"):
        assert title in out
    assert "s1" not in out
    assert "s3" not in out


def test_render_digest_max_age_boundary_is_inclusive():
    exactly_at_edge = _agenda("edge item", age_seconds=7 * 86400)
    just_over = _agenda("just over", age_seconds=7 * 86400 + 1)
    out = render_digest(
        lambda: [exactly_at_edge, just_over], lambda: [], max_age_days=7, now=NOW,
    )
    assert "edge item" in out
    assert "just over" not in out


def test_render_digest_excludes_naive_created_at_older_than_cutoff():
    naive_stale = AgendaEntry(
        title="naive stale item", status="proposed",
        created_at=NOW.replace(tzinfo=None) - timedelta(days=10),
    )
    naive_fresh = AgendaEntry(
        title="naive fresh item", status="proposed",
        created_at=NOW.replace(tzinfo=None) - timedelta(hours=1),
    )
    out = render_digest(
        lambda: [naive_stale, naive_fresh], lambda: [], max_age_days=7, now=NOW,
    )
    assert "naive stale item" not in out
    assert "naive fresh item" in out


def test_render_digest_no_max_age_days_applies_no_filter():
    stale = _agenda("ancient item", age_seconds=100 * 86400)
    out = render_digest(lambda: [stale], lambda: [], now=NOW)
    assert "ancient item" in out


def test_load_autonomy_digest_config_missing_key_raises(tmp_path, monkeypatch):
    bad_yaml = tmp_path / "memory.yaml"
    bad_yaml.write_text("autonomy_digest: {}\n", encoding="utf-8")
    monkeypatch.setattr(autonomy_digest_module, "MEMORY_YAML", bad_yaml)
    with pytest.raises(RuntimeError, match="max_age_days"):
        autonomy_digest_module.load_autonomy_digest_config()


def test_load_autonomy_digest_config_missing_section_raises(tmp_path, monkeypatch):
    bad_yaml = tmp_path / "memory.yaml"
    bad_yaml.write_text("other: 1\n", encoding="utf-8")
    monkeypatch.setattr(autonomy_digest_module, "MEMORY_YAML", bad_yaml)
    with pytest.raises(RuntimeError, match="autonomy_digest"):
        autonomy_digest_module.load_autonomy_digest_config()


def test_load_autonomy_digest_config_reads_real_memory_yaml():
    cfg = autonomy_digest_module.load_autonomy_digest_config()
    assert cfg.max_age_days == 7


def test_assemble_system_prompt_excludes_stale_agenda_item_from_digest(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    stale_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
    fresh_at = datetime.now(timezone.utc)
    store = AgendaStore()
    store.add(
        _make_agenda_item(
            "ancient resume queued item", AgendaStatus.RESUME_QUEUED, created_at=stale_at,
        ),
        by="operator", reason="test",
    )
    store.add(
        _make_agenda_item("fresh open item", AgendaStatus.PROPOSED, created_at=fresh_at),
        by="operator", reason="test",
    )

    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    memory_store_dir = tmp_path / "memory-store"

    prompt = prompt_module.assemble_system_prompt(
        workspace_dir=workspace_dir, memory_store_dir=memory_store_dir,
    )
    assert "fresh open item" in prompt
    assert "ancient resume queued item" not in prompt


# -- Fix A2: commitment framing ---------------------------------------------


def test_assemble_system_prompt_digest_includes_commitment_framing_when_nonempty(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    # Fresh timestamp — real max_age_days cutoff runs against the wall clock.
    AgendaStore().add(
        _make_agenda_item(
            "finish the digest task", AgendaStatus.PROPOSED, created_at=datetime.now(timezone.utc),
        ),
        by="operator", reason="test",
    )
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    memory_store_dir = tmp_path / "memory-store"

    prompt = prompt_module.assemble_system_prompt(
        workspace_dir=workspace_dir, memory_store_dir=memory_store_dir,
    )
    assert prompt_module.AUTONOMY_DIGEST_LEAD in prompt


def test_assemble_system_prompt_omits_commitment_framing_when_digest_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("TESSERACT_HOME", str(tmp_path))
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    memory_store_dir = tmp_path / "memory-store"

    prompt = prompt_module.assemble_system_prompt(
        workspace_dir=workspace_dir, memory_store_dir=memory_store_dir,
    )
    assert prompt_module.AUTONOMY_DIGEST_LEAD not in prompt

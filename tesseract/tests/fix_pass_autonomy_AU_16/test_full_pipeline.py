"""AU-16 full-wiring verification — chat-turn producer + 5 lifecycle jobs
+ trees populated end-to-end (the "OpenHuman state" from the canonical
store).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import yaml

from tesseract.memory.leaves import (
    LeafState,
    LeafStore,
    MemoryLeaf,
    mint_leaf_id,
)
from tesseract.memory.leaf_seals import iter_seals, seals_root
from tesseract.memory.trees.global_tree import (
    daily_digest_path,
    list_digest_dates,
)
from tesseract.memory.trees.source_tree import (
    list_source_tree_paths,
    read_source_tree,
)
from tesseract.memory.trees.topic_tree import (
    TOPIC_ACTIVATION_THRESHOLD,
    is_topic_active,
    list_active_topics,
)
from tesseract.scheduler.tasks.leaf_append import AppendBufferJob
from tesseract.scheduler.tasks.leaf_digest_daily import DigestDailyJob
from tesseract.scheduler.tasks.leaf_extract import ExtractChunkJob, extract_entities
from tesseract.scheduler.tasks.leaf_seal import SealJob
from tesseract.scheduler.tasks.leaf_topic_route import TopicRouteJob
from tesseract.scheduler.types import JobContext


def _ctx(**config) -> JobContext:
    return JobContext(job_name="test", config=dict(config))


def _make_leaf(
    body: str,
    *,
    source: str = "chat:cockpit",
) -> MemoryLeaf:
    now = datetime.now(timezone.utc)
    return MemoryLeaf(
        id=mint_leaf_id(),
        source=source,
        created_at=now,
        updated_at=now,
        body=body,
    )


# ---- entity heuristic --------------------------------------------------


def test_extract_entities_picks_wikilinks_and_capitalised_tokens() -> None:
    body = (
        "Operator chatted about [[ProjectX]] today. The team also "
        "raised TARS-related questions about Anthropic and OpenAI."
    )
    ents = extract_entities(body)
    # Wikilink first; then capitalised tokens (stopwords filtered).
    assert ents[0] == "ProjectX"
    assert "Anthropic" in ents
    assert "OpenAI" in ents
    assert "The" not in ents       # stopword
    assert "Operator" not in ents  # explicit stopword for chat framing


def test_extract_entities_dedups_across_sources() -> None:
    body = "[[ProjectX]] · ProjectX twice via cap-sniff but only once in list."
    ents = extract_entities(body)
    assert ents.count("ProjectX") == 1


# ---- end-to-end (chat-turn → leaf → trees) ----------------------------


async def test_full_pipeline_chat_leaf_to_source_tree(isolated_home: Path) -> None:
    """Seed one leaf in PENDING_EXTRACTION (the shape a chat-turn
    producer would write); run extract → append → seal; verify the
    seal artefact landed AND the source tree carries an AU-16
    frontmatter block with a kind: source-summary tag."""
    LeafStore().add(
        _make_leaf(
            "User: tell me about [[ProjectAlpha]] and what we know.\n\n"
            "Assistant: I've got a few notes on ProjectAlpha and the "
            "associated work. There's also overlap with [[OpenHuman]] "
            "we should consider here.",
            source="chat:cockpit",
        )
    )
    await ExtractChunkJob().run(_ctx())
    await AppendBufferJob().run(_ctx())
    await SealJob().run(_ctx(max_buffer_leaves=1, max_buffer_age_seconds=999999))

    # Seal artefact landed.
    seals = list(iter_seals())
    assert len(seals) == 1
    assert seals[0].source_slug == "chat-cockpit"

    # Source tree exists with AU-16 frontmatter.
    source_md = read_source_tree("chat-cockpit")
    assert source_md is not None
    assert source_md.startswith("---\n")
    end = source_md.index("\n---\n", 4)
    fm = yaml.safe_load(source_md[4:end])
    assert fm["kind"] == "source-summary"
    assert "source-summary" in fm["tags"]
    assert "sealed" in fm["tags"]


async def test_full_pipeline_topic_tree_activates_from_chat_entities(
    isolated_home: Path,
) -> None:
    """Three chat turns mentioning the same entity activate a topic
    tree with the AU-16 frontmatter banner (red-hub color group)."""
    for _ in range(TOPIC_ACTIVATION_THRESHOLD):
        LeafStore().add(
            _make_leaf(
                "Operator brought up MysteryProject again — discussion "
                "circled around MysteryProject scope and timelines.",
                source="chat:cockpit",
            )
        )
    await ExtractChunkJob().run(_ctx())
    await AppendBufferJob().run(_ctx())
    await SealJob().run(_ctx(max_buffer_leaves=TOPIC_ACTIVATION_THRESHOLD))
    result = await TopicRouteJob().run(_ctx())
    assert "MysteryProject" in result.payload["activated"]
    assert is_topic_active("MysteryProject")
    # Topic file carries the red-hub frontmatter tag.
    from tesseract.memory.trees.topic_tree import topic_tree_path

    text = topic_tree_path("MysteryProject").read_text(encoding="utf-8")
    assert text.startswith("---\nkind: topic-summary")


async def test_full_pipeline_global_digest_lands(isolated_home: Path) -> None:
    """Seal → DigestDailyJob → global digest file present with the
    AU-16 global-digest tag (yellow rollup color group)."""
    LeafStore().add(
        _make_leaf(
            "User asked a long-enough question to admit. "
            "Assistant produced a sufficient reply.",
            source="chat:cockpit",
        )
    )
    await ExtractChunkJob().run(_ctx())
    await AppendBufferJob().run(_ctx())
    await SealJob().run(_ctx(max_buffer_leaves=1, max_buffer_age_seconds=999999))

    result = await DigestDailyJob().run(_ctx())
    assert result.ok
    today = datetime.now(timezone.utc).date()
    path = daily_digest_path(today)
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    end = text.index("\n---\n", 4)
    fm = yaml.safe_load(text[4:end])
    assert fm["kind"] == "global-digest"


# ---- schedule.yaml carries every leaf job ----------------------------


def test_schedule_yaml_wires_all_5_leaf_jobs() -> None:
    """The handler references resolve to the live classes."""
    data = yaml.safe_load(
        Path("tesseract/config/schedule.yaml").read_text(encoding="utf-8")
    )
    rows = {row["name"]: row for row in data["jobs"]}
    expected = {
        "leaf_extract": "tesseract.scheduler.tasks.leaf_extract.ExtractChunkJob",
        "leaf_append": "tesseract.scheduler.tasks.leaf_append.AppendBufferJob",
        "leaf_seal": "tesseract.scheduler.tasks.leaf_seal.SealJob",
        "leaf_topic_route": "tesseract.scheduler.tasks.leaf_topic_route.TopicRouteJob",
        "leaf_digest_daily": "tesseract.scheduler.tasks.leaf_digest_daily.DigestDailyJob",
    }
    for name, handler in expected.items():
        assert name in rows, f"{name} missing from schedule.yaml"
        assert rows[name]["handler"] == handler
        assert rows[name]["enabled"] is True


def test_schedule_yaml_does_not_carry_deleted_wiki_export() -> None:
    """The wiki_export row was deleted alongside its handler module."""
    data = yaml.safe_load(
        Path("tesseract/config/schedule.yaml").read_text(encoding="utf-8")
    )
    names = {row["name"] for row in data["jobs"]}
    assert "wiki_export" not in names


# ---- chat-turn producer ----------------------------------------------


def test_chat_session_emit_turn_leaf_writes_leaf(isolated_home: Path) -> None:
    """Stub a ChatSession with two history turns (user + assistant) and
    verify ``_emit_turn_leaf`` lands a PENDING_EXTRACTION leaf."""
    from types import SimpleNamespace
    from tesseract.brain.chat import ChatSession

    session = SimpleNamespace(
        history=[
            {"role": "user", "content": "What do we know about [[ProjectAlpha]]?"},
            {
                "role": "assistant",
                "content": (
                    "ProjectAlpha is our internal codename for the autonomy "
                    "rollout. We've shipped several phases already."
                ),
            },
        ],
        session_kind="cockpit",
        channel_display_name=None,
    )
    # Borrow the real method via unbound call.
    ChatSession._emit_turn_leaf(session, 0)

    leaves = list(LeafStore().iter_active())
    assert len(leaves) == 1
    leaf = leaves[0]
    assert leaf.state is LeafState.PENDING_EXTRACTION
    assert leaf.source == "chat:cockpit"
    assert "ProjectAlpha" in leaf.body
    assert "Assistant:" in leaf.body


def test_chat_session_emit_turn_leaf_skips_empty_assistant(isolated_home: Path) -> None:
    from types import SimpleNamespace
    from tesseract.brain.chat import ChatSession

    session = SimpleNamespace(
        history=[
            {"role": "user", "content": "Tell me something."},
        ],
        session_kind="cockpit",
        channel_display_name=None,
    )
    ChatSession._emit_turn_leaf(session, 0)
    assert list(LeafStore().iter_active()) == []


def test_chat_session_emit_turn_leaf_uses_channel_source(isolated_home: Path) -> None:
    from types import SimpleNamespace
    from tesseract.brain.chat import ChatSession

    session = SimpleNamespace(
        history=[
            {"role": "user", "content": "Hi from Telegram"},
            {
                "role": "assistant",
                "content": "Hello — long enough body content to admit cleanly.",
            },
        ],
        session_kind="channel",
        channel_display_name="telegram-42",
    )
    ChatSession._emit_turn_leaf(session, 0)
    leaves = list(LeafStore().iter_active())
    assert len(leaves) == 1
    assert leaves[0].source == "channel:telegram-42"

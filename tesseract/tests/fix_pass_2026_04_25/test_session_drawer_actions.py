"""P15X-A: SessionDrawer Preview / Rename / Duplicate.

Covers `session_store` helpers (rename_session, duplicate_session,
preview_session) plus the path-traversal slug guard. Live HTTP coverage
of the new routes lives in `test_session_drawer_routes.py`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tesseract.brain.session_store import (
    _is_valid_slug,
    duplicate_session,
    preview_session,
    rename_session,
    save_session,
)


def _seed(session_dir: Path, name: str = "alpha") -> Path:
    return save_session(
        session_dir,
        name,
        model="gpt-5.4-nano",
        started_at="2026-04-25T10:00:00+00:00",
        history=[
            {"role": "user", "content": "hello tars"},
            {"role": "assistant", "content": "hi operator, what's up?"},
            {"role": "tool", "content": "ignore me — not user/assistant"},
            {"role": "assistant", "content": "second assistant turn"},
        ],
    )


# ── slug guard ────────────────────────────────────────────


@pytest.mark.parametrize(
    "slug",
    [
        "ok-name",
        "a.b.c",
        "with_underscore",
        "1234",
        "session-2026-04-25",
    ],
)
def test_slug_accepts_safe_names(slug: str):
    assert _is_valid_slug(slug)


@pytest.mark.parametrize(
    "slug",
    [
        "",                       # empty
        "   ",                    # whitespace only
        " leading-space",         # not stripped
        ".hidden",                # leading dot
        "../escape",              # path traversal
        "a/b",                    # forward slash
        "a\\b",                   # backslash
        "evil:name",              # colon (illegal on Windows)
        "name with space",        # space
        "x" * 81,                 # over the 80-char cap
        "café",                   # non-ASCII letter (NFC vs NFD on macOS)
        "néo",               # NFC e-acute mid-slug
        "néo",         # NFD e + combining accent (visually identical)
    ],
)
def test_slug_rejects_unsafe_names(slug: str):
    assert not _is_valid_slug(slug)


# ── rename ────────────────────────────────────────────────


def test_rename_happy_path(tmp_path: Path):
    _seed(tmp_path, "alpha")
    ok, reason = rename_session(tmp_path, "alpha", "beta")
    assert ok and reason == ""
    assert not (tmp_path / "alpha.json").exists()
    assert (tmp_path / "beta.json").exists()


def test_rename_strips_json_suffix(tmp_path: Path):
    _seed(tmp_path, "alpha")
    ok, _ = rename_session(tmp_path, "alpha.json", "beta.json")
    assert ok
    assert (tmp_path / "beta.json").exists()


def test_rename_to_same_name_is_noop(tmp_path: Path):
    _seed(tmp_path, "alpha")
    ok, reason = rename_session(tmp_path, "alpha", "alpha")
    assert ok and reason == ""
    assert (tmp_path / "alpha.json").exists()


def test_rename_refuses_to_overwrite(tmp_path: Path):
    _seed(tmp_path, "alpha")
    _seed(tmp_path, "beta")
    ok, reason = rename_session(tmp_path, "alpha", "beta")
    assert not ok and reason == "exists"
    assert (tmp_path / "alpha.json").exists()


def test_rename_missing_source(tmp_path: Path):
    ok, reason = rename_session(tmp_path, "ghost", "still-ghost")
    assert not ok and reason == "not_found"


def test_rename_rejects_path_traversal(tmp_path: Path):
    _seed(tmp_path, "alpha")
    ok, reason = rename_session(tmp_path, "alpha", "../../etc/passwd")
    assert not ok and reason == "invalid_name"
    assert (tmp_path / "alpha.json").exists()


# ── duplicate ─────────────────────────────────────────────


def test_duplicate_happy_path(tmp_path: Path):
    src = _seed(tmp_path, "alpha")
    ok, reason = duplicate_session(tmp_path, "alpha", "alpha-copy")
    assert ok and reason == ""
    assert src.exists(), "duplicate must not delete source"
    dst = tmp_path / "alpha-copy.json"
    assert dst.exists()
    assert json.loads(dst.read_text(encoding="utf-8")) == json.loads(
        src.read_text(encoding="utf-8")
    )


def test_duplicate_refuses_to_overwrite(tmp_path: Path):
    _seed(tmp_path, "alpha")
    _seed(tmp_path, "beta")
    ok, reason = duplicate_session(tmp_path, "alpha", "beta")
    assert not ok and reason == "exists"


def test_duplicate_rejects_invalid_dest(tmp_path: Path):
    _seed(tmp_path, "alpha")
    ok, reason = duplicate_session(tmp_path, "alpha", "../escape")
    assert not ok and reason == "invalid_name"


# ── preview ───────────────────────────────────────────────


def test_preview_returns_user_assistant_text_only(tmp_path: Path):
    _seed(tmp_path, "alpha")
    preview = preview_session(tmp_path, "alpha", max_turns=6)
    assert preview is not None
    assert preview["session_id"] == "alpha"
    assert preview["model"] == "gpt-5.4-nano"
    roles = [t["role"] for t in preview["turns"]]
    assert roles == ["user", "assistant", "assistant"]
    assert "ignore me" not in " ".join(t["text"] for t in preview["turns"])


def test_preview_caps_at_max_turns(tmp_path: Path):
    save_session(
        tmp_path,
        "many",
        model="gpt-5.4-nano",
        started_at="2026-04-25T10:00:00+00:00",
        history=[
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"turn {i}"}
            for i in range(20)
        ],
    )
    preview = preview_session(tmp_path, "many", max_turns=4)
    assert preview is not None
    assert len(preview["turns"]) == 4


def test_preview_skips_reasoning_blobs(tmp_path: Path):
    save_session(
        tmp_path,
        "reason",
        model="gpt-5.4-nano",
        started_at="2026-04-25T10:00:00+00:00",
        history=[
            {"role": "assistant", "content": "blob", "_reasoning": True},
            {"role": "assistant", "content": "real text"},
        ],
    )
    preview = preview_session(tmp_path, "reason", max_turns=6)
    assert preview is not None
    assert len(preview["turns"]) == 1
    assert preview["turns"][0]["text"] == "real text"


def test_preview_handles_list_content(tmp_path: Path):
    save_session(
        tmp_path,
        "parts",
        model="gpt-5.4-nano",
        started_at="2026-04-25T10:00:00+00:00",
        history=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "first part"},
                    {"type": "input_image", "image_url": "..."},
                    {"type": "text", "text": "second part"},
                ],
            },
        ],
    )
    preview = preview_session(tmp_path, "parts", max_turns=6)
    assert preview is not None
    assert preview["turns"][0]["text"] == "first part second part"


def test_preview_handles_responses_api_content(tmp_path: Path):
    """Responses API ('chat_brain' = gpt-5.4-nano) emits `output_text` /
    `input_text` content blocks; the OpenAI-Responses adapter persists them
    verbatim. _extract_text must recognize both shapes."""
    save_session(
        tmp_path,
        "responses",
        model="gpt-5.4-nano",
        started_at="2026-04-25T10:00:00+00:00",
        history=[
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "user said this"},
                ],
            },
            {
                "role": "assistant",
                "content": [
                    {"type": "output_text", "text": "assistant replied this"},
                ],
            },
        ],
    )
    preview = preview_session(tmp_path, "responses", max_turns=6)
    assert preview is not None
    assert preview["turns"][0]["text"] == "user said this"
    assert preview["turns"][1]["text"] == "assistant replied this"


def test_preview_returns_empty_turns_for_silent_session(tmp_path: Path):
    """Session with only tool/system turns → 200 with empty `turns`,
    not None. SessionDrawer renders a '(no extractable turns)' placeholder."""
    save_session(
        tmp_path,
        "silent",
        model="gpt-5.4-nano",
        started_at="2026-04-25T10:00:00+00:00",
        history=[
            {"role": "tool", "content": "tool only"},
            {"role": "system", "content": "system only"},
        ],
    )
    preview = preview_session(tmp_path, "silent", max_turns=6)
    assert preview is not None
    assert preview["turns"] == []


def test_preview_invalid_slug_returns_none(tmp_path: Path):
    assert preview_session(tmp_path, "../escape") is None


def test_preview_missing_session_returns_none(tmp_path: Path):
    assert preview_session(tmp_path, "ghost") is None

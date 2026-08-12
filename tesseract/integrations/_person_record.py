"""Person-record writer — multi-channel identity link.

Lives under ``<TESSERACT_HOME>/memory-store/reference/people/<slug>.md``.
The contract is seeded in ``_shared/channel-adapter-protocol.md`` §"Person-record
cross-link": every ``adapter.approve()`` lands BOTH (1) the adapter's own allowlist
row AND (2) one of these markdown records, so the operator can see at a glance
which person owns which ``chat_id`` across channels.

Body shape — frontmatter (memory-store conventions) plus a delimited YAML block
the upsert path round-trips:

    ---
    id: mem_xxxxxxxx
    type: reference
    title: Jane Doe
    summary: Telegram contact (operator tier)
    created_at: 2026-05-14T08:00:00+00:00
    updated_at: 2026-05-14T08:00:00+00:00
    importance: 6
    tags: [person, channel:telegram]
    ---

    # Jane Doe

    <!-- person-record:begin -->
    channels:
      - telegram
    chat_ids:
      telegram:
        - 12345
    tier: operator
    ttl: null
    display_name: Jane Doe
    <!-- person-record:end -->

    (operator-curated free-form notes below)

Upsert semantics: re-running ``upsert_person_record(channel, user_id, ...)`` on
an existing slug parses the delimited block, merges ``chat_ids[<channel>]`` (de-duped),
updates ``tier`` / ``ttl`` / ``display_name`` to the new values, and rewrites
the block in place. Any free-form notes the operator added below the block are
preserved verbatim. The frontmatter's ``id`` (mem_-prefixed) is preserved on
upsert so memory-store callers that already linked to the record do not stale-out.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from tesseract.paths import TESSERACT_DIR

log = logging.getLogger(__name__)

_BLOCK_BEGIN = "<!-- person-record:begin -->"
_BLOCK_END = "<!-- person-record:end -->"
_BLOCK_RE = re.compile(
    re.escape(_BLOCK_BEGIN) + r"\n(.*?)\n" + re.escape(_BLOCK_END),
    re.DOTALL,
)


def _people_dir() -> Path:
    # `TESSERACT_HOME` is resolved from the env var at every call so test
    # fixtures that `monkeypatch.setenv("TESSERACT_HOME", tmp_path)` after
    # module import still land under tmp_path. The fallback is the source
    # anchor (``TESSERACT_DIR``), NOT the import-time `tesseract.paths`
    # constant — that constant captures the env var at import time and
    # a missing env var would otherwise silently route writes to whichever
    # home was set when paths.py first loaded — the import-time-capture
    # anti-pattern, same shape as the log-discipline rule.
    val = os.environ.get("TESSERACT_HOME")
    root = Path(val).resolve() if val else TESSERACT_DIR.resolve()
    out = root / "memory-store" / "reference" / "people"
    out.mkdir(parents=True, exist_ok=True)
    return out


def slugify(value: str) -> str:
    """Lower-snake-case slug.

    ASCII alnum + underscore only — matches ``MemoryFrontmatter.slug`` regex and
    keeps the filename portable across Windows/Linux. Empty / unparseable input
    collapses to ``unknown`` rather than blank so an upsert never produces an
    invalid path; callers that care should pass a meaningful fallback.
    """
    cleaned = re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")
    return cleaned or "unknown"


def person_record_path(slug: str) -> Path:
    return _people_dir() / f"{slug}.md"


def upsert_person_record(
    *,
    channel: str,
    user_id: str,
    tier: str,
    ttl_iso: str | None,
    display_name: str | None,
) -> Path:
    """Create or update the person record for ``user_id`` on ``channel``.

    Slug resolution priority: explicit ``display_name`` slug → ``f"chat_{user_id}"``.
    The fallback keeps every approved chat surfaced as a record even when the
    Telegram update didn't carry a username.

    Idempotent: re-approving the same chat with the same fields is a no-op
    semantically (the ``chat_ids`` set is de-duplicated; ``updated_at`` refreshes).
    """
    base = display_name or f"chat_{user_id}"
    slug = slugify(base)
    path = person_record_path(slug)

    now_iso = datetime.now(timezone.utc).isoformat()

    if path.exists():
        text = path.read_text(encoding="utf-8")
        fm, body = _split_frontmatter(text)
        block_data, notes = _split_body_block(body)
        block_data = _merge_block(
            block_data,
            channel=channel,
            user_id=user_id,
            tier=tier,
            ttl_iso=ttl_iso,
            display_name=display_name,
        )
        fm.setdefault("id", _new_memory_id())
        fm["title"] = display_name or fm.get("title") or slug
        fm["summary"] = _summary_for(channel, tier)
        fm["updated_at"] = now_iso
        fm.setdefault("created_at", now_iso)
        fm["tags"] = _tags_for(channel, block_data.get("channels") or [])
        new_text = _render_record(fm=fm, block_data=block_data, notes=notes)
    else:
        fm = {
            "id": _new_memory_id(),
            "type": "reference",
            "title": display_name or slug,
            "summary": _summary_for(channel, tier),
            "created_at": now_iso,
            "updated_at": now_iso,
            "importance": 6,
            "tags": _tags_for(channel, [channel]),
        }
        block_data = _merge_block(
            {},
            channel=channel,
            user_id=user_id,
            tier=tier,
            ttl_iso=ttl_iso,
            display_name=display_name,
        )
        notes = ""
        new_text = _render_record(fm=fm, block_data=block_data, notes=notes)

    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(new_text, encoding="utf-8")
    tmp.replace(path)
    return path


def read_person_record(slug: str) -> dict[str, Any] | None:
    """Return the parsed block of an existing record, or ``None`` if absent.

    Used by tests + the route layer's future identity-link queries; keeps the
    block-parsing logic colocated with the writer.
    """
    path = person_record_path(slug)
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    _fm, body = _split_frontmatter(text)
    block_data, _notes = _split_body_block(body)
    return block_data


# ── helpers ────────────────────────────────────────────────────────────────


def _new_memory_id() -> str:
    return f"mem_{secrets.token_hex(4)}"


def _summary_for(channel: str, tier: str) -> str:
    return f"{channel.capitalize()} contact ({tier} tier)"


def _tags_for(channel: str, channels: list[Any]) -> list[str]:
    base: list[str] = ["person"]
    seen: set[str] = set()
    out: list[str] = []
    candidates = list(channels) + [channel]
    for c in candidates:
        if not isinstance(c, str) or not c:
            continue
        tag = f"channel:{c}"
        if tag in seen:
            continue
        seen.add(tag)
        out.append(tag)
    return base + sorted(out)


def _merge_block(
    existing: dict[str, Any],
    *,
    channel: str,
    user_id: str,
    tier: str,
    ttl_iso: str | None,
    display_name: str | None,
) -> dict[str, Any]:
    channels = list(existing.get("channels") or [])
    if channel not in channels:
        channels.append(channel)
    channels = sorted({c for c in channels if isinstance(c, str)})

    chat_ids_raw = existing.get("chat_ids") or {}
    chat_ids: dict[str, list[Any]] = {}
    if isinstance(chat_ids_raw, dict):
        for k, v in chat_ids_raw.items():
            if not isinstance(k, str):
                continue
            if isinstance(v, list):
                chat_ids[k] = list(v)
            else:
                chat_ids[k] = [v]
    existing_for_channel = chat_ids.get(channel, [])
    coerced = _coerce_chat_id(user_id)
    if coerced not in existing_for_channel:
        existing_for_channel.append(coerced)
    chat_ids[channel] = existing_for_channel

    out: dict[str, Any] = {
        "channels": channels,
        "chat_ids": chat_ids,
        "tier": tier,
        "ttl": ttl_iso,
    }
    if display_name:
        out["display_name"] = display_name
    elif existing.get("display_name"):
        out["display_name"] = existing["display_name"]
    return out


def _coerce_chat_id(user_id: str) -> Any:
    """Telegram chat_ids are ints; other channels may stay strings. Try int
    first so the YAML round-trip stays consistent with how the bridge
    represents the value upstream."""
    try:
        return int(user_id)
    except (TypeError, ValueError):
        return user_id


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---\n"):
        return {}, text
    try:
        end = text.index("\n---\n", 4)
    except ValueError:
        return {}, text
    fm_text = text[4:end]
    body = text[end + len("\n---\n") :]
    try:
        loaded = yaml.safe_load(fm_text) or {}
    except yaml.YAMLError:
        log.warning("person_record: malformed frontmatter; treating as empty")
        loaded = {}
    if not isinstance(loaded, dict):
        loaded = {}
    return loaded, body


def _split_body_block(body: str) -> tuple[dict[str, Any], str]:
    """Pull the structured block out of ``body``; return ``(data, notes)``.

    ``notes`` is everything except the block + its surrounding heading line so
    operator-curated free-form text survives an upsert intact.
    """
    match = _BLOCK_RE.search(body)
    if match is None:
        return {}, body
    try:
        data = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError:
        log.warning("person_record: malformed block; ignoring")
        data = {}
    if not isinstance(data, dict):
        data = {}
    notes = (body[: match.start()] + body[match.end() :]).strip()
    return data, notes


def _render_record(*, fm: dict[str, Any], block_data: dict[str, Any], notes: str) -> str:
    fm_text = yaml.dump(fm, sort_keys=False, default_flow_style=False).rstrip()
    block_text = yaml.dump(block_data, sort_keys=False, default_flow_style=False).rstrip()
    title = fm.get("title") or fm.get("id") or "Person"
    heading = f"# {title}"
    parts = ["---", fm_text, "---", "", heading, "", _BLOCK_BEGIN, block_text, _BLOCK_END]
    if notes:
        parts.extend(["", notes])
    return "\n".join(parts) + "\n"


__all__ = [
    "person_record_path",
    "read_person_record",
    "slugify",
    "upsert_person_record",
]

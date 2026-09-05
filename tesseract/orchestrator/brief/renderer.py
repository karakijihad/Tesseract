"""BriefRenderer — stitch the daily-brief sub-digesters into one markdown file.

Renderer flow:

  1. Invoke the digester agents in fixed order. Mission-digest gets the
     agenda-store activity payload (see
     :mod:`tesseract.orchestrator.brief.activity`); vault-digest gets the
     recent ingest-log rows; the rest get ``{"since_hours": 24}``. Every
     section reads a store this machine owns, so none of them can state
     something that has stopped being true since it was written down.
  2. Stitch outputs under the section headers. Drop header AND body for
     whitespace-only outputs (empty-section rule). All-empty → fallback
     "No notable activity in the past 24 hours."
  3. Wrap in frontmatter + ``# Daily Brief — <iso-date>`` title. Atomic
     write to ``memory-store/daily/briefs/<iso-date>.md``.
  4. Write a brief-as-memory record (type=PROJECT, source_type=
     daily_brief) so ``memory_search`` can recall the brief.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from tesseract.memory.store import MemoryStore
from tesseract.memory.types import MemoryFrontmatter, MemoryType, Stability
from tesseract.orchestrator.brief.activity import collect_yesterday_activity
from tesseract.orchestrator.brief.learnings import collect_consolidated_learnings
from tesseract.orchestrator.brief.workspace import collect_workspace_activity

logger = logging.getLogger(__name__)

SECTION_ORDER: tuple[tuple[str, str], ...] = (
    ("workspace-digest", "## Yesterday in TESSERACT"),
    # The watchman's last sweep. Renderer-only (no sub-agent, no
    # model): the findings are already counted and written, and a digester
    # asked to summarise them could only add a way for them to be wrong.
    # Dropped entirely when the runtime was quiet, which is most mornings.
    ("runtime-report", "## What broke"),
    ("mission-digest", "## Yesterday with you"),
    ("memory-digest", "## What I learned"),
    ("vault-digest", "## Vault"),
)


_SINCE_24H_PAYLOAD: dict[str, object] = {"since_hours": 24}


DigesterInvoker = Callable[[str, dict], Awaitable[str]]

@dataclass
class RenderResult:
    path: Path
    body: str
    sections_rendered: list[str] = field(default_factory=list)
    sections_dropped: list[str] = field(default_factory=list)
    memory_id: str | None = None
    overwritten: bool = False
    skipped_existing: bool = False
    # Structured payload + workspace event id when the
    # renderer was given an event_store. Consumers (workspace fan-out,
    # tests) read these to surface the newsletter card.
    workspace_payload: dict | None = None
    workspace_event_id: str | None = None


class BriefRenderer:
    """Daily-brief orchestrator. One instance per call site (REPL slash,
    cron). Re-entrant: ``render`` is the only public method and it
    owns all I/O.
    """

    def __init__(
        self,
        *,
        briefs_dir: Path,
        invoke_digester: DigesterInvoker,
        memory_store: MemoryStore | None,
        event_store: "Any | None" = None,
        vault_wiki_dir: Path | None = None,
        home: Path | None = None,
    ) -> None:
        self._briefs_dir = Path(briefs_dir)
        self._invoke_digester = invoke_digester
        self._memory_store = memory_store
        # vault/wiki/ingest-log.md location. When wired, the renderer
        # pre-reads recent entries and hands them to vault-digest as a
        # structured payload, so the agent cannot invent wiki pages when
        # its `file_read` call does not fire.
        self._vault_wiki_dir = Path(vault_wiki_dir) if vault_wiki_dir is not None else None
        # When wired (REST + cron), each render writes a
        # `daily_brief` workspace event so the newsletter card appears
        # in the operator's workspace stream. Tests pass an in-memory
        # EventStore over tmp_path.
        self._event_store = event_store
        # TESSERACT_HOME root the agenda store is read under. ``None``
        # skips the LLM round-trip for the section that reads it.
        self._home = Path(home) if home is not None else None

    async def render(
        self,
        target_date: date,
        *,
        prior_brief: str = "",
        overwrite: bool = True,
    ) -> RenderResult:
        out_path = self._briefs_dir / f"{target_date.isoformat()}.md"
        already_existed = out_path.exists()
        if already_existed and not overwrite:
            existing = out_path.read_text(encoding="utf-8")
            return RenderResult(
                path=out_path,
                body=existing,
                skipped_existing=True,
            )

        vault_entries = self._read_recent_ingest_entries(
            since_hours=24,
            now=datetime.now(timezone.utc),
        )

        activity_payload = self._collect_yesterday_activity_payload(target_date)
        workspace_payload = self._collect_workspace_payload(target_date)
        learnings_payload = self._collect_learnings_payload(target_date)

        section_bodies: dict[str, str] = {}
        for slug, _header in SECTION_ORDER:
            if slug == "runtime-report":
                section_bodies[slug] = _collect_runtime_block()
                continue
            if slug == "mission-digest":
                if activity_payload is None:
                    # Renderer not wired to a TESSERACT_HOME for the
                    # agenda store — fall through so legacy callers
                    # (tests that stub the digester) keep working.
                    payload = dict(_SINCE_24H_PAYLOAD)
                elif not activity_payload["items"]:
                    # No agenda item went DONE/BLOCKED in the window —
                    # skip the LLM round-trip, mirrors the vault
                    # empty-signal rule. Anything the agent could produce
                    # would be hallucination.
                    section_bodies[slug] = ""
                    continue
                else:
                    payload = dict(activity_payload)
            elif slug == "vault-digest":
                payload = {
                    "since_hours": 24,
                    "entries": vault_entries,
                }
                if self._vault_wiki_dir is not None and not vault_entries:
                    # Renderer is wired to a real wiki dir and the log
                    # has no rows in the window → skip the LLM. Anything
                    # the agent could produce would be hallucination.
                    # When ``vault_wiki_dir`` is None the renderer can't
                    # ground the agent; fall through so legacy callers
                    # (tests that stub the digester) keep working.
                    section_bodies[slug] = ""
                    continue
            elif slug == "workspace-digest":
                # No fallthrough to `{"since_hours": 24}` here, deliberately.
                # A renderer with no event store cannot ground this section,
                # and asking anyway is the exact fault this phase removes: the
                # invoker attaches no tools, so the agent would be answering
                # from nothing. Not wired and nothing happened are both an
                # empty section.
                if workspace_payload is None or (
                    not workspace_payload["events"] and not workspace_payload["comments"]
                ):
                    section_bodies[slug] = ""
                    continue
                payload = dict(workspace_payload)
            elif slug == "memory-digest":
                # Same rule. The third empty case is real and correct: the
                # overnight pass ran with no app attached, so it left no record
                # of what it promoted, and there is nothing to restate.
                if learnings_payload is None or not learnings_payload["learnings"]:
                    section_bodies[slug] = ""
                    continue
                payload = dict(learnings_payload)
            else:
                # A section added to SECTION_ORDER with nothing grounding it
                # renders empty rather than being asked for prose it cannot
                # have checked. That is this phase's rule and it holds for the
                # next section as much as for the two that prompted it.
                section_bodies[slug] = ""
                continue
            try:
                raw = await self._invoke_digester(slug, payload)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "brief: digester %s failed (%s); treating section as empty",
                    slug, exc,
                )
                raw = ""
            section_bodies[slug] = (raw or "").strip()

        body, rendered, dropped = _assemble_body(
            target_date=target_date,
            section_bodies=section_bodies,
        )

        frontmatter = _build_frontmatter(target_date=target_date)
        full_text = f"---\n{frontmatter}---\n\n{body}\n"
        _atomic_write(out_path, full_text)

        memory_id = self._write_brief_memory(
            target_date=target_date,
            body=body,
            out_path=out_path,
        )

        workspace_payload = _build_workspace_payload(
            target_date=target_date,
            section_bodies=section_bodies,
        )
        workspace_event_id = self._write_workspace_event(
            target_date=target_date,
            payload=workspace_payload,
        )

        return RenderResult(
            path=out_path,
            body=body,
            sections_rendered=rendered,
            sections_dropped=dropped,
            memory_id=memory_id,
            overwritten=already_existed,
            workspace_payload=workspace_payload,
            workspace_event_id=workspace_event_id,
        )

    def _write_workspace_event(
        self,
        *,
        target_date: date,
        payload: dict,
    ) -> str | None:
        """Emit a ``daily_brief`` workspace event so the newsletter card
        appears in the operator's workspace stream. No-op when no
        ``EventStore`` was wired (REPL/tests without the workspace
        substrate). Failures are logged + swallowed — the markdown
        write is the authoritative artifact.
        """
        store = self._event_store
        if store is None:
            return None
        try:
            from tesseract.workspace_events.events import WorkspaceEvent
        except Exception:  # noqa: BLE001
            logger.exception("brief: workspace_events import failed; skipping event")
            return None
        sections = payload.get("sections") or {}
        title = f"Daily brief — {target_date.isoformat()}"
        summary = _first_sentences(
            _summary_seed(sections),
            count=2,
        ) or f"Daily brief for {target_date.isoformat()}."
        try:
            event = WorkspaceEvent.new(
                kind="daily_brief",
                source="daily_brief",
                title=title,
                summary=summary,
                payload=payload,
                priority=4,
                author_id="system",
                author_display="the assistant",
            )
            store.append_event(event)
        except Exception:  # noqa: BLE001
            logger.exception("brief: workspace event append failed")
            return None
        logger.info(
            "brief: workspace_event_appended date=%s", target_date.isoformat(),
        )
        return event.event_id

    def _collect_yesterday_activity_payload(
        self, target_date: date
    ) -> dict[str, Any] | None:
        """Pre-fetch the ``mission-digest`` payload from the agenda store.

        Returns ``None`` when the renderer was not wired to a home so
        callers (tests that stub the digester) fall through to the
        ``since_hours`` payload.
        """
        if self._home is None:
            return None
        try:
            return collect_yesterday_activity(
                home=self._home, target_date=target_date,
            )
        except Exception:  # noqa: BLE001
            logger.exception("brief: yesterday-activity pre-fetch failed")
            return {"since_hours": 24, "items": []}

    def _collect_workspace_payload(self, target_date: date) -> dict[str, Any] | None:
        """Pre-fetch the ``workspace-digest`` payload from the event store.

        None when the renderer was not wired to one, which the caller treats
        the same as a quiet day: the section is skipped. A renderer that cannot
        reach the stream has nothing to hand the agent, and asking anyway is
        what this phase removed.
        """
        if self._event_store is None:
            return None
        try:
            return collect_workspace_activity(
                event_store=self._event_store, target_date=target_date,
            )
        except Exception:  # noqa: BLE001
            logger.exception("brief: workspace pre-fetch failed")
            return {"since_hours": 24, "events": [], "comments": []}

    def _collect_learnings_payload(self, target_date: date) -> dict[str, Any] | None:
        """Pre-fetch the ``memory-digest`` payload: what the overnight pass
        promoted, read back from the store by id.

        Needs both halves. The event store says WHICH memories the consolidator
        lifted; the memory store says what they say. Without either one there
        is nothing to restate, and the section is skipped rather than guessed
        at.
        """
        if self._event_store is None or self._memory_store is None:
            return None
        try:
            return collect_consolidated_learnings(
                event_store=self._event_store,
                memory_store=self._memory_store,
                target_date=target_date,
            )
        except Exception:  # noqa: BLE001
            logger.exception("brief: consolidated-learnings pre-fetch failed")
            return {"since_hours": 24, "learnings": []}

    def _read_recent_ingest_entries(
        self,
        *,
        since_hours: int,
        now: datetime,
    ) -> list[dict[str, str]]:
        """Parse ``vault/wiki/ingest-log.md`` for rows within the window.

        Returns a list of ``{date, title, slug, status}`` dicts ordered
        as they appear in the log (newest-first by ingest convention).
        Empty list when the log is absent, unparseable, or has no rows
        within ``since_hours``. ``status`` is ``"new"`` today; the
        librarian's log only records ingests, not updates — see the
        plan for the update-detection
        roadmap.
        """
        if self._vault_wiki_dir is None:
            return []
        log_path = self._vault_wiki_dir / "ingest-log.md"
        if not log_path.exists():
            logger.info(
                "brief: ingest-log not found at %s — vault-digest section "
                "will be skipped", log_path,
            )
            return []
        try:
            text = log_path.read_text(encoding="utf-8")
        except OSError:
            logger.exception("brief: failed to read %s", log_path)
            return []
        # Date-granular comparison: ingest-log rows store only YYYY-MM-DD.
        # Anchor each entry at end-of-day UTC so a row dated "yesterday"
        # is treated as <23h59m old at the typical morning-brief tick,
        # preventing valid same-calendar-day rows from being filtered as
        # stale. The renderer's `since_hours` parameter is therefore a
        # ceiling on calendar-date inclusion, not an hour-precise filter
        # — which matches the granularity the librarian writes.
        cutoff = now - timedelta(hours=since_hours)
        entries: list[dict[str, str]] = []
        for line in text.splitlines():
            parsed = _parse_ingest_line(line)
            if parsed is None:
                continue
            try:
                entry_dt = datetime.fromisoformat(parsed["date"]).replace(
                    hour=23, minute=59, second=59, tzinfo=timezone.utc,
                )
            except ValueError:
                continue
            if entry_dt < cutoff:
                continue
            entries.append(parsed)
        return entries

    def _write_brief_memory(
        self,
        *,
        target_date: date,
        body: str,
        out_path: Path,
    ) -> str | None:
        if self._memory_store is None:
            return None
        try:
            store_dir = self._memory_store.store_dir
            rel = _relative_or_string(out_path, store_dir.parent)
        except Exception:  # noqa: BLE001
            rel = str(out_path)
        summary = _first_sentences(body, count=2)
        memory_id = f"mem_{secrets.token_hex(4)}"
        now = datetime.now(timezone.utc)
        fm = MemoryFrontmatter(
            id=memory_id,
            type=MemoryType.PROJECT,
            title=f"Daily brief — {target_date.isoformat()}",
            summary=summary,
            created_at=now,
            updated_at=now,
            importance=5,
            tags=["daily_brief"],
            source_path=rel,
            source_type="daily_brief",
            stability=Stability.ACTIVE,
        )
        memory_body = (
            f"Daily brief for {target_date.isoformat()}.\n\n"
            f"{summary}\n\n"
            f"Full brief: {rel}"
        )
        try:
            written = self._memory_store.write(fm, memory_body)
        except Exception as exc:  # noqa: BLE001
            logger.warning("brief: memory write failed (%s)", exc)
            return None
        return memory_id if written else None


def _assemble_body(
    *,
    target_date: date,
    section_bodies: dict[str, str],
) -> tuple[str, list[str], list[str]]:
    """Stitch sections per renderer-spec.

    Returns (full_body, rendered_section_slugs, dropped_section_slugs).
    """
    parts: list[str] = [f"# Daily Brief — {target_date.isoformat()}"]
    rendered: list[str] = []
    dropped: list[str] = []
    for slug, header in SECTION_ORDER:
        raw = section_bodies.get(slug, "").strip()
        if not raw:
            dropped.append(slug)
            continue
        parts.append(header)
        parts.append(raw)
        rendered.append(slug)

    if not rendered:
        return (
            f"# Daily Brief — {target_date.isoformat()}\n\n"
            "No notable activity in the past 24 hours."
        ), [], [slug for slug, _ in SECTION_ORDER]

    return "\n\n".join(parts), rendered, dropped


def _build_workspace_payload(
    *,
    target_date: date,
    section_bodies: dict[str, str],
) -> dict:
    """Assemble the structured payload the workspace card consumes.

    Voice-prose sections stay as plain paragraphs. The schema is locked:
    adding fields stays backward-compatible, renaming does not.
    """
    vault_body = (section_bodies.get("vault-digest") or "").strip()
    return {
        "kind": "daily_brief",
        "date": target_date.isoformat(),
        "sections": {
            "yesterday_in_tesseract": (section_bodies.get("workspace-digest") or "").strip(),
            "yesterday_with_you": (section_bodies.get("mission-digest") or "").strip(),
            "what_i_learned": (section_bodies.get("memory-digest") or "").strip(),
            "vault": _split_vault_bullets(vault_body),
        },
    }


def _collect_runtime_block() -> str:
    """The watchman's last sweep, as bullets. Empty when it was quiet.

    Reads the artifact rather than re-reading the logs: the counting has
    already happened, and a second reader of the same logs is a second answer
    that can disagree with the first.
    """
    from tesseract.orchestrator.watchman.report import watchman_dir

    path = watchman_dir() / "latest.json"
    if not path.exists():
        return ""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.exception("brief: watchman latest.json unreadable")
        return ""
    findings = [f for f in (payload.get("findings") or []) if isinstance(f, dict)]
    if not findings:
        return ""
    lines = [
        f"- {f.get('source')} — {f.get('summary')}"
        for f in findings
        if f.get("summary")
    ]
    observed = str(payload.get("observed_at") or "")
    if observed:
        lines.append(f"\n_As of {observed}._")
    return "\n".join(lines)


def _split_vault_bullets(text: str) -> list[str]:
    """Vault digest returns a short flat bullet list. Keep it as a list
    so the card renderer can render structured bullets, but accept
    prose too — non-bullet text becomes a single-item list."""
    if not text:
        return []
    bullets: list[str] = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        if stripped.startswith("- "):
            bullets.append(stripped[2:].strip())
        elif bullets:
            bullets[-1] = f"{bullets[-1]} {stripped}"
        else:
            bullets.append(stripped)
    return bullets


def _summary_seed(sections: dict | None) -> str:
    if not isinstance(sections, dict):
        return ""
    for key in (
        "yesterday_in_tesseract",
        "yesterday_with_you",
        "what_i_learned",
    ):
        value = sections.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _build_frontmatter(*, target_date: date) -> str:
    payload = {
        "date": target_date.isoformat(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "generator": "daily-brief",
        "sources": [slug for slug, _ in SECTION_ORDER],
    }
    return yaml.safe_dump(payload, sort_keys=False)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(str(tmp), str(path))


_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")

# `- **YYYY-MM-DD** — [Title](slug.md) · topic: `topic` · source: `path``
_INGEST_LINE_RE = re.compile(
    r"^-\s+\*\*(?P<date>\d{4}-\d{2}-\d{2})\*\*\s+[—-]+\s+"
    r"\[(?P<title>[^\]]+)\]\((?P<slug>[^)]+)\)"
)


def _parse_ingest_line(line: str) -> dict[str, str] | None:
    """Parse one row of ``vault/wiki/ingest-log.md``. Returns None when
    the line is a header, blank, or otherwise off-format."""
    match = _INGEST_LINE_RE.match(line.strip())
    if match is None:
        return None
    slug = match.group("slug").strip()
    if slug.endswith(".md"):
        slug = slug[:-3]
    return {
        "date": match.group("date"),
        "title": match.group("title").strip(),
        "slug": slug,
        "status": "new",
    }


def _first_sentences(text: str, count: int = 2) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("##"):
            continue
        sentences = _SENTENCE_RE.split(stripped)
        return " ".join(sentences[:count]).strip()
    return ""


def _relative_or_string(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root)).replace("\\", "/")
    except ValueError:
        return str(path)


__all__ = [
    "BriefRenderer",
    "RenderResult",
    "SECTION_ORDER",
    "DigesterInvoker",
]

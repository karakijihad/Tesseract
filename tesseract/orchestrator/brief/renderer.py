"""BriefRenderer — stitch the daily-brief sub-digesters into one markdown file.

MO-9-13 refactor: the world section's input is no longer the operator-
curated ``tracked-topics.yaml`` list, but the three fixed pillars
(:data:`tesseract.orchestrator.brief.pillars.DEFAULT_PILLARS`) plus the
operator's :class:`~tesseract.orchestrator.brief.interests.InterestsProfile`.

Renderer flow per ``_shared/brief-renderer-spec.md`` (updated):

  1. Load the interests profile (zero-state if file missing).
  2. Pre-fetch Tavily results per pillar. Dedupe against the
     ``_dedupe.json`` store. Track per-iteration cost (call count + USD
     estimate) and stop fetching when ``loop_cost_caps`` is hit.
  3. Sort kept results within each pillar by
     :func:`~tesseract.orchestrator.brief.interests.score_url` descending
     so world-digest sees them pre-ranked by operator affinity.
  4. Invoke the five digester agents in fixed order. World-digest gets
     the pillar metadata + pre-fetched results + interests payload;
     mission-digest gets the agenda-store activity payload (see
     :mod:`tesseract.orchestrator.brief.activity` — mission engine
     deleted, section now reports DONE/BLOCKED agenda items); the other
     three get ``{"since_hours": 24}``.
  5. Stitch outputs under the renderer-spec section headers. Drop
     header AND body for whitespace-only outputs (empty-section rule).
     All-empty → fallback "No notable activity in the past 24 hours."
  6. Wrap in frontmatter + ``# Daily Brief — <iso-date>`` title. Atomic
     write to ``memory-store/daily/briefs/<iso-date>.md``.
  7. Write a brief-as-memory record (type=PROJECT, source_type=
     daily_brief) so ``memory_search`` can recall the brief.
  8. Prune dedupe store (drop entries older than the widest pillar
     window) + atomic save.
"""

from __future__ import annotations

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
from tesseract.orchestrator.brief.dedupe import DedupeStore
from tesseract.orchestrator.brief.ecosystem import (
    DEFAULT_SINCE_DAYS as ECOSYSTEM_DEFAULT_SINCE_DAYS,
    collect_ecosystem_inputs,
    has_any_signal,
)
from tesseract.orchestrator.brief.interests import (
    InterestsProfile,
    load_profile,
    score_url,
)
from tesseract.orchestrator.brief.pillars import DEFAULT_PILLARS, Pillar, render_query

logger = logging.getLogger(__name__)

SECTION_ORDER: tuple[tuple[str, str], ...] = (
    ("workspace-digest", "## Yesterday in TESSERACT"),
    ("mission-digest", "## Yesterday with you"),
    ("memory-digest", "## What I learned"),
    ("vault-digest", "## Vault"),
    ("ecosystem-digest", "## Ecosystem"),
    ("world-digest", "## World"),
    # AU-23 — strategist initiatives. Renderer-only (no sub-agent):
    # `_collect_strategist_block` reads the most recent
    # `strategist_summary` workspace event and emits the block verbatim.
    # Section is dropped when no batch exists in the lookback window.
    ("strategist-initiatives", "## Initiatives"),
)


# AU-23 — how far back the renderer searches for a `strategist_summary`
# workspace event. Defaults to 72h so a 3-day strategist cadence reliably
# lands in the next brief; widen if the scheduler runs less often.
DEFAULT_STRATEGIST_LOOKBACK_HOURS = 72

_SINCE_24H_PAYLOAD: dict[str, object] = {"since_hours": 24}


DigesterInvoker = Callable[[str, dict], Awaitable[str]]
TavilyFetcher = Callable[[str, dict], Awaitable[list[dict]]]
# `compile_source(raw_rel_path)` — VaultLibrarian's wiki-compilation entry
# point. Renderer holds a typing-erased reference so the orchestrator
# layer never imports the memory layer directly.
LibrarianCompile = Callable[[str], Awaitable[object | None]]

# Per-day cap on auto-promoted world cards. World-digest pulls at most
# ``Pillar.max_results`` per pillar (5-10 typical) across 3 pillars; the
# cap is a backstop against runaway ingest if pillars grow.
_AUTO_PROMOTE_MAX_PER_DAY = 30
_AUTO_PROMOTE_DIR = "world-brief"


@dataclass(frozen=True)
class CostCaps:
    """Per-iteration ceiling. ``loop_cost_caps`` in ``permissions.yaml``."""
    max_usd: float = 0.50
    max_tavily_calls: int = 30
    usd_per_tavily_call: float = 0.005


@dataclass
class RenderResult:
    path: Path
    body: str
    sections_rendered: list[str] = field(default_factory=list)
    sections_dropped: list[str] = field(default_factory=list)
    tavily_calls: int = 0
    estimated_usd: float = 0.0
    cost_cap_hit: bool = False
    memory_id: str | None = None
    overwritten: bool = False
    skipped_existing: bool = False
    # MO-9-14 — structured payload + workspace event id when the
    # renderer was given an event_store. Consumers (workspace fan-out,
    # tests) read these to surface the newsletter card.
    workspace_payload: dict | None = None
    workspace_event_id: str | None = None
    # World cards auto-promoted into ``vault/raw/world-brief/<date>/``.
    # Each entry is ``{slug, title, url, compiled}`` where ``compiled``
    # is True when the librarian successfully turned the raw file into a
    # wiki page on the spot. Empty list when auto-promote is unwired.
    vault_promoted: list[dict] = field(default_factory=list)


class BriefRenderer:
    """Daily-brief orchestrator. One instance per call site (REPL slash,
    cron). Re-entrant: ``render`` is the only public method and it
    owns all I/O.
    """

    def __init__(
        self,
        *,
        briefs_dir: Path,
        pillars: tuple[Pillar, ...] = DEFAULT_PILLARS,
        interests_path: Path | None = None,
        dedupe_path: Path | None = None,
        invoke_digester: DigesterInvoker,
        tavily_search: TavilyFetcher | None,
        memory_store: MemoryStore | None,
        cost_caps: CostCaps | None = None,
        event_store: "Any | None" = None,
        vault_wiki_dir: Path | None = None,
        vault_raw_dir: Path | None = None,
        librarian_compile: LibrarianCompile | None = None,
        ecosystem_home: Path | None = None,
        ecosystem_since_days: int = ECOSYSTEM_DEFAULT_SINCE_DAYS,
        strategist_lookback_hours: int = DEFAULT_STRATEGIST_LOOKBACK_HOURS,
    ) -> None:
        self._briefs_dir = Path(briefs_dir)
        self._pillars = tuple(pillars)
        self._interests_path = Path(interests_path) if interests_path is not None else None
        self._dedupe_path = (
            Path(dedupe_path)
            if dedupe_path is not None
            else self._briefs_dir / "_dedupe.json"
        )
        self._invoke_digester = invoke_digester
        self._tavily_search = tavily_search
        self._memory_store = memory_store
        self._cost_caps = cost_caps or CostCaps()
        # vault/wiki/ingest-log.md location. When wired, the renderer
        # pre-reads recent entries and hands them to vault-digest as a
        # structured payload — mirrors how world-digest is grounded by
        # pre-fetched Tavily results, prevents the agent from
        # hallucinating wiki pages when its `file_read` call doesn't fire.
        self._vault_wiki_dir = Path(vault_wiki_dir) if vault_wiki_dir is not None else None
        # vault/raw/ destination for auto-promoted world cards. When
        # ``vault_raw_dir`` and ``librarian_compile`` are both wired, each
        # render writes one markdown file per kept world hit under
        # ``vault/raw/world-brief/<date>/<slug>.md`` and calls the
        # librarian to compile it into a wiki page. The next day's brief
        # then sees those pages in the ingest log → grounded vault
        # section. Either dep missing → no auto-promote (REPL / cold
        # scheduler boot stays cheap).
        self._vault_raw_dir = Path(vault_raw_dir) if vault_raw_dir is not None else None
        self._librarian_compile = librarian_compile
        # MO-9-14 — when wired (REST + cron), each render writes a
        # `daily_brief` workspace event so the newsletter card appears
        # in the operator's workspace stream. Tests pass an in-memory
        # EventStore over tmp_path.
        self._event_store = event_store
        # AU-24 — base for the ecosystem-radar pre-fetcher (memory
        # leaves, agenda items, docs-watch snapshots, provider digests).
        # ``None`` skips the LLM round-trip for that section, matching
        # the all-empty vault rule.
        self._ecosystem_home = Path(ecosystem_home) if ecosystem_home is not None else None
        self._ecosystem_since_days = int(ecosystem_since_days)
        # AU-23 — strategist batch lookback. Reads only the most recent
        # `strategist_summary` workspace event within this window so
        # the brief surfaces the current portfolio without reaching
        # into the agenda store.
        self._strategist_lookback_hours = int(strategist_lookback_hours)

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

        profile = load_profile(self._interests_path)
        dedupe = DedupeStore(self._dedupe_path)
        world_results, tavily_calls, cap_hit = await self._fetch_world_results(
            profile=profile,
            dedupe=dedupe,
            today=target_date,
        )

        vault_entries = self._read_recent_ingest_entries(
            since_hours=24,
            now=datetime.now(timezone.utc),
        )

        ecosystem_payload = self._collect_ecosystem_payload(target_date)
        ecosystem_has_signal = has_any_signal(ecosystem_payload) if ecosystem_payload else False

        activity_payload = self._collect_yesterday_activity_payload(target_date)

        section_bodies: dict[str, str] = {}
        for slug, _header in SECTION_ORDER:
            if slug == "strategist-initiatives":
                # Renderer-only section, no sub-agent. Skip if no recent
                # batch — `_assemble_body` drops empty sections.
                section_bodies[slug] = _collect_strategist_block(
                    event_store=self._event_store,
                    lookback_hours=self._strategist_lookback_hours,
                    now=datetime.now(timezone.utc),
                )
                continue
            if slug == "world-digest":
                payload: dict[str, object] = {
                    "pillars": [
                        {
                            "name": p.name,
                            "max_results": p.max_results,
                            "dedupe_window_days": p.dedupe_window_days,
                        }
                        for p in self._pillars
                    ],
                    "tavily_results": world_results,
                    "interests_profile": dict(profile.pillars),
                    "cost_cap_reached": cap_hit,
                }
            elif slug == "mission-digest":
                if activity_payload is None:
                    # Renderer not wired to a TESSERACT_HOME for the
                    # agenda store — fall through so legacy callers
                    # (tests that stub the digester) keep working.
                    payload = dict(_SINCE_24H_PAYLOAD)
                elif not activity_payload["items"]:
                    # No agenda item went DONE/BLOCKED in the window —
                    # skip the LLM round-trip, mirrors the vault/
                    # ecosystem empty-signal rule. Anything the agent
                    # could produce would be hallucination.
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
            elif slug == "ecosystem-digest":
                if ecosystem_payload is None:
                    # Renderer not wired to a TESSERACT_HOME for ecosystem
                    # — fall through so legacy callers (tests that stub
                    # the digester) keep working.
                    payload = dict(_SINCE_24H_PAYLOAD)
                elif not ecosystem_has_signal:
                    # All four streams empty in the window → skip the
                    # LLM, mirrors the vault-empty rule.
                    section_bodies[slug] = ""
                    continue
                else:
                    payload = dict(ecosystem_payload)
            else:
                payload = dict(_SINCE_24H_PAYLOAD)
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
            cost_cap_reached=cap_hit,
        )

        frontmatter = _build_frontmatter(target_date=target_date)
        full_text = f"---\n{frontmatter}---\n\n{body}\n"
        _atomic_write(out_path, full_text)

        if self._pillars:
            window_cap = max(
                (p.dedupe_window_days for p in self._pillars),
                default=7,
            )
            dedupe.prune(window_cap, target_date)
            dedupe.save()

        memory_id = self._write_brief_memory(
            target_date=target_date,
            body=body,
            out_path=out_path,
        )

        workspace_payload = _build_workspace_payload(
            target_date=target_date,
            section_bodies=section_bodies,
            world_results=world_results,
            pillar_names=tuple(p.name for p in self._pillars),
            cost_cap_reached=cap_hit,
        )
        workspace_event_id = self._write_workspace_event(
            target_date=target_date,
            payload=workspace_payload,
            cost_cap_reached=cap_hit,
        )

        promoted = await self._auto_promote_world_results(
            target_date=target_date,
            world_results=world_results,
        )

        return RenderResult(
            path=out_path,
            body=body,
            sections_rendered=rendered,
            sections_dropped=dropped,
            tavily_calls=tavily_calls,
            estimated_usd=tavily_calls * self._cost_caps.usd_per_tavily_call,
            cost_cap_hit=cap_hit,
            memory_id=memory_id,
            overwritten=already_existed,
            workspace_payload=workspace_payload,
            workspace_event_id=workspace_event_id,
            vault_promoted=promoted,
        )

    async def _fetch_world_results(
        self,
        *,
        profile: InterestsProfile,
        dedupe: DedupeStore,
        today: date,
    ) -> tuple[dict[str, list[dict]], int, bool]:
        """Per-pillar Tavily fetch with dedupe + cost cap + affinity sort.

        Returns the results dict keyed by pillar name (sorted descending
        by interest score), the call count, and whether the cap fired
        mid-iteration.
        """
        if not self._pillars or self._tavily_search is None:
            return {}, 0, False

        out: dict[str, list[dict]] = {}
        calls = 0
        cap_hit = False
        for pillar in self._pillars:
            if _cost_cap_exceeded(calls, self._cost_caps):
                cap_hit = True
                break
            query = render_query(pillar, today)
            try:
                results = await self._tavily_search(
                    query,
                    {
                        "max_results": pillar.max_results,
                        "include_domains": [],
                        "exclude_domains": [],
                    },
                )
            except Exception as exc:  # noqa: BLE001
                logger.info(
                    "brief: tavily failed for pillar %r (%s); treating as empty",
                    pillar.name, exc,
                )
                results = []
            calls += 1
            kept: list[dict] = []
            for hit in results or []:
                url = str(hit.get("url") or "").strip()
                if not url:
                    continue
                if dedupe.is_seen(url, pillar.dedupe_window_days, today):
                    continue
                dedupe.mark_seen(url, today)
                kept.append(hit)
            kept.sort(
                key=lambda h: score_url(
                    profile,
                    pillar.name,
                    str(h.get("title") or ""),
                    str(h.get("content") or h.get("summary") or ""),
                ),
                reverse=True,
            )
            out[pillar.name] = kept
        return out, calls, cap_hit

    def _write_workspace_event(
        self,
        *,
        target_date: date,
        payload: dict,
        cost_cap_reached: bool,
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
        world = sections.get("world") if isinstance(sections, dict) else None
        card_count = 0
        if isinstance(world, dict):
            for items in world.values():
                if isinstance(items, list):
                    card_count += len(items)
        title = f"Daily brief — {target_date.isoformat()}"
        summary = _first_sentences(
            _summary_seed(sections),
            count=2,
        ) or (
            "World section partial — cost cap reached."
            if cost_cap_reached
            else f"Daily brief for {target_date.isoformat()}."
        )
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
            "brief: workspace_event_appended date=%s cards=%d cost_cap=%s",
            target_date.isoformat(), card_count, cost_cap_reached,
        )
        return event.event_id

    async def _auto_promote_world_results(
        self,
        *,
        target_date: date,
        world_results: dict[str, list[dict]],
    ) -> list[dict]:
        """Write each kept world hit as a raw vault source and ask the
        librarian to compile it into a wiki page.

        No-op when ``vault_raw_dir`` or ``librarian_compile`` is unwired
        (REPL / cold scheduler). Fail-soft: any single write or compile
        failure is logged and skipped — the brief itself is the
        authoritative artifact, vault-promotion is best-effort.
        """
        if self._vault_raw_dir is None or self._librarian_compile is None:
            return []
        if not world_results:
            return []
        vault_root = self._vault_raw_dir.parent  # ``vault/``
        out_dir = self._vault_raw_dir / _AUTO_PROMOTE_DIR / target_date.isoformat()
        out_dir.mkdir(parents=True, exist_ok=True)

        promoted: list[dict] = []
        seen_slugs: set[str] = set()
        for pillar_name, hits in world_results.items():
            for hit in hits:
                if len(promoted) >= _AUTO_PROMOTE_MAX_PER_DAY:
                    return promoted
                title = str(hit.get("title") or "").strip()
                url = str(hit.get("url") or "").strip()
                if not title or not url:
                    continue
                slug = _world_hit_slug(title, url)
                if not slug or slug in seen_slugs:
                    continue
                seen_slugs.add(slug)
                raw_file = out_dir / f"{slug}.md"
                if raw_file.exists():
                    # Idempotent re-render — same hit already on disk.
                    continue
                summary = str(hit.get("content") or hit.get("summary") or "").strip()
                source = str(hit.get("source") or "").strip()
                published = str(
                    hit.get("published_at")
                    or hit.get("published_date")
                    or hit.get("publishedAt")
                    or ""
                ).strip()
                raw_file.write_text(
                    _format_world_raw(
                        title=title,
                        url=url,
                        summary=summary,
                        source=source,
                        published=published,
                        pillar=pillar_name,
                        captured_on=target_date.isoformat(),
                    ),
                    encoding="utf-8",
                )
                # ``relative_to`` raises ValueError when the operator
                # configured ``vault_raw_dir`` outside the vault root
                # (so ``vault_root`` is wrong). Treat as a misconfig:
                # raw file is on disk, compile is skipped, render
                # continues. Wider try-block than the compile call
                # alone so an unhandled ValueError can't abort render
                # after the brief was already written.
                raw_rel: str | None = None
                compiled = False
                try:
                    raw_rel = raw_file.relative_to(vault_root).as_posix()
                    result = await self._librarian_compile(raw_rel)
                    compiled = result is not None
                except ValueError:
                    logger.warning(
                        "brief: vault auto-promote skipped — %s is not under %s "
                        "(check vault_raw_dir config)",
                        raw_file, vault_root,
                    )
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "brief: vault auto-promote compile failed for %s",
                        raw_rel or raw_file,
                    )
                promoted.append({
                    "slug": slug,
                    "title": title,
                    "url": url,
                    "compiled": compiled,
                })
        return promoted

    def _collect_ecosystem_payload(
        self, target_date: date
    ) -> dict[str, Any] | None:
        """Pre-fetch the AU-24 ecosystem-radar inputs from disk.

        Returns ``None`` when the renderer was not wired to a
        ``ecosystem_home`` — callers (typically tests stubbing the
        digester) then bypass the ecosystem section entirely. Failures
        in any single stream are absorbed inside
        :func:`collect_ecosystem_inputs`; this wrapper only catches the
        truly unexpected case where the helper itself raises.
        """
        if self._ecosystem_home is None:
            return None
        try:
            return collect_ecosystem_inputs(
                home=self._ecosystem_home,
                target_date=target_date,
                since_days=self._ecosystem_since_days,
            )
        except Exception:  # noqa: BLE001
            logger.exception("brief: ecosystem pre-fetch failed")
            return {
                "since_days": self._ecosystem_since_days,
                "target_date": target_date.isoformat(),
                "memory_signals": [],
                "memory_leaves": [],
                "docs_watch": [],
                "provider_watch": [],
            }

    def _collect_yesterday_activity_payload(
        self, target_date: date
    ) -> dict[str, Any] | None:
        """Pre-fetch the ``mission-digest`` payload from the agenda store.

        Reuses ``_ecosystem_home`` (same ``TESSERACT_HOME`` root the
        AU-24 pre-fetcher reads ``agenda/`` under) rather than adding a
        second home parameter — one root, two different agenda-store
        filters. Returns ``None`` when unwired so callers (tests that
        stub the digester) fall through to the legacy ``since_hours``
        payload.
        """
        if self._ecosystem_home is None:
            return None
        try:
            return collect_yesterday_activity(
                home=self._ecosystem_home, target_date=target_date,
            )
        except Exception:  # noqa: BLE001
            logger.exception("brief: yesterday-activity pre-fetch failed")
            return {"since_hours": 24, "items": []}

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
        plan in ``Docs/Plan/vault-autoingest/`` for the update-detection
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


def _cost_cap_exceeded(calls: int, caps: CostCaps) -> bool:
    if caps.max_tavily_calls and calls >= caps.max_tavily_calls:
        return True
    if caps.max_usd and (calls * caps.usd_per_tavily_call) >= caps.max_usd:
        return True
    return False


def _assemble_body(
    *,
    target_date: date,
    section_bodies: dict[str, str],
    cost_cap_reached: bool,
) -> tuple[str, list[str], list[str]]:
    """Stitch sections per renderer-spec.

    Returns (full_body, rendered_section_slugs, dropped_section_slugs).
    """
    parts: list[str] = [f"# Daily Brief — {target_date.isoformat()}"]
    rendered: list[str] = []
    dropped: list[str] = []
    for slug, header in SECTION_ORDER:
        raw = section_bodies.get(slug, "").strip()
        if slug == "world-digest" and cost_cap_reached and not raw:
            raw = "World section partial — cost cap reached."
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
    world_results: dict[str, list[dict]],
    pillar_names: tuple[str, ...],
    cost_cap_reached: bool,
) -> dict:
    """Assemble the structured payload the workspace card consumes.

    Voice-prose sections stay as plain paragraphs (the four non-world
    sub-digesters already emit voice-friendly text). The world section
    is per-pillar structured cards keyed by pillar name so the React
    component can render groups directly without re-parsing prose. The
    schema is locked in MO-9-14's contract — adding fields stays
    backward-compatible; renaming requires a phase update.
    """
    vault_body = (section_bodies.get("vault-digest") or "").strip()
    ecosystem_body = (section_bodies.get("ecosystem-digest") or "").strip()
    initiatives_body = (section_bodies.get("strategist-initiatives") or "").strip()
    return {
        "kind": "daily_brief",
        "date": target_date.isoformat(),
        "sections": {
            "yesterday_in_tesseract": (section_bodies.get("workspace-digest") or "").strip(),
            "yesterday_with_you": (section_bodies.get("mission-digest") or "").strip(),
            "what_i_learned": (section_bodies.get("memory-digest") or "").strip(),
            "vault": _split_vault_bullets(vault_body),
            # AU-24 — ecosystem digester emits voice-prose just like the
            # workspace / mission / memory sections; surface as a single
            # paragraph string. Empty when the digester returned nothing
            # or the renderer skipped the section for absent signal.
            "ecosystem": ecosystem_body,
            # AU-23 — strategist initiatives. Bullet list per
            # `_collect_strategist_block`; surfaced as the same flat-
            # bullet shape as the vault section so the React card can
            # render them as `<li>` rows without re-parsing prose.
            "initiatives": _split_vault_bullets(initiatives_body),
            "world": {
                pillar: [_to_world_card(hit) for hit in (world_results.get(pillar) or [])]
                for pillar in pillar_names
            },
        },
        "cost_cap_reached": bool(cost_cap_reached),
    }


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


def _collect_strategist_block(
    *,
    event_store: Any | None,
    lookback_hours: int,
    now: datetime,
) -> str:
    """Render the most recent strategist batch as voice-safe prose for the
    `## Initiatives` brief section.

    Returns the empty string when no event store is wired, when no
    `strategist_summary` workspace event exists, or when the most recent
    one falls outside the lookback window — `_assemble_body` drops empty
    sections so the operator sees nothing instead of a blank header.
    """
    if event_store is None:
        return ""
    try:
        events = event_store.list_events(
            kinds=("strategist_summary",),
            limit=5,
        )
    except Exception:  # noqa: BLE001
        logger.exception("brief: strategist_summary lookup failed")
        return ""
    if not events:
        return ""
    cutoff = now - timedelta(hours=max(1, int(lookback_hours)))
    most_recent = events[0]
    ts_raw = str(getattr(most_recent, "ts", ""))
    if not ts_raw:
        return ""
    try:
        ev_ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
    except ValueError:
        logger.warning("brief: strategist_summary ts unparseable: %r", ts_raw)
        return ""
    if ev_ts.tzinfo is None:
        ev_ts = ev_ts.replace(tzinfo=timezone.utc)
    if ev_ts < cutoff:
        return ""
    payload = getattr(most_recent, "payload", {}) or {}
    initiatives = payload.get("initiatives") or []
    if not isinstance(initiatives, list) or not initiatives:
        return ""
    lines: list[str] = []
    for raw in initiatives:
        if not isinstance(raw, dict):
            continue
        goal = str(raw.get("goal") or "").strip()
        if not goal:
            continue
        rationale = str(raw.get("rationale") or "").strip()
        risk = str(raw.get("suggested_risk_class") or "").strip()
        try:
            confidence = float(raw.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        try:
            horizon = int(raw.get("horizon_days") or 0)
        except (TypeError, ValueError):
            horizon = 0
        # Voice-safe (per daily-brief.md anti-output rules): no bold, no
        # links. Lead with the goal; trail with the why and a small
        # confidence/horizon clause.
        bullet = f"- {goal}"
        if rationale:
            bullet += f" — {rationale[:280]}"
        meta_bits: list[str] = []
        if risk:
            meta_bits.append(risk)
        if confidence:
            meta_bits.append(f"confidence {confidence:.0%}")
        if horizon:
            meta_bits.append(f"horizon {horizon}d")
        if meta_bits:
            bullet += f" (" + ", ".join(meta_bits) + ")"
        lines.append(bullet)
    return "\n".join(lines).strip()


def _to_world_card(hit: dict) -> dict:
    """Project a Tavily hit into the renderer-stable card shape."""
    url = str(hit.get("url") or "").strip()
    title = str(hit.get("title") or "").strip()
    summary = str(hit.get("content") or hit.get("summary") or "").strip()
    source = str(hit.get("source") or "").strip()
    if not source and url:
        # Best-effort publisher inference from the URL host.
        try:
            from urllib.parse import urlparse
            host = urlparse(url).netloc
            source = host[4:] if host.startswith("www.") else host
        except Exception:  # noqa: BLE001
            source = ""
    published_at = str(
        hit.get("published_at")
        or hit.get("published_date")
        or hit.get("publishedAt")
        or ""
    ).strip()
    return {
        "title": title,
        "summary": summary,
        "source": source,
        "url": url,
        "published_at": published_at,
    }


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


def _world_hit_slug(title: str, url: str) -> str:
    """Stable slug for an auto-promoted world card. Lower-case, ASCII,
    `-` separators, capped at 60 chars — same rules as the vault
    librarian's slug helper. Falls back to a hash of the URL when the
    title slugifies to empty (e.g. CJK-only titles)."""
    from tesseract.memory.vault_manager import slugify

    slug = slugify(title)
    if slug:
        return slug
    import hashlib

    return "world-" + hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]


def _format_world_raw(
    *,
    title: str,
    url: str,
    summary: str,
    source: str,
    published: str,
    pillar: str,
    captured_on: str,
) -> str:
    """Render an auto-promoted world card as a vault-ingestable markdown
    file. The librarian's text extractor takes the file's prose and
    classifies; YAML frontmatter is informational only."""
    front = {
        "title": title,
        "source": source,
        "url": url,
        "published_at": published,
        "captured_on": captured_on,
        "pillar": pillar,
        "kind": "world-brief",
    }
    front_text = yaml.safe_dump(front, sort_keys=False).strip()
    parts = [f"---\n{front_text}\n---", f"# {title}", ""]
    if summary:
        parts.extend([summary, ""])
    parts.append(f"Source: {source or url}")
    if published:
        parts.append(f"Published: {published}")
    parts.append(f"URL: {url}")
    return "\n".join(parts) + "\n"


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
    "CostCaps",
    "RenderResult",
    "SECTION_ORDER",
    "DigesterInvoker",
    "TavilyFetcher",
]

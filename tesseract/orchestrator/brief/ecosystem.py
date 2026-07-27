"""AU-24 — ecosystem-radar pre-fetcher for the daily brief.

Reads the last ``since_days`` of four AI-ecosystem signal streams and
returns one structured payload the ``ecosystem-digest`` agent consumes:

* **memory_signals** — :class:`AgendaItem` records with
  ``source=memory_signal`` (AU-11c entity-threshold trips + future
  memory-side producers wired through ``AutonomyEventBus``).
* **memory_leaves** — :class:`MemoryLeaf` records historically landed
  by the operator's discovery watchlist (AU-11c producer job retired
  P4 prune wave 2; existing leaves still surface here until they age
  out of the window).
* **docs_watch** — markdown snapshots at
  ``<TESSERACT_HOME>/autonomy/watchlist-snapshots/<source>.md`` whose
  mtime falls inside the window. Historical — the AU-11a producer job
  was retired P4 prune wave 2; existing snapshots still surface here
  until they age out.
* **provider_watch** — daily digests at
  ``<TESSERACT_HOME>/memory-store/daily/providers/<iso-date>.md`` from
  the ``provider_watch`` scheduler job.

Pure I/O. No LLM calls. Returns a dict the brief renderer hands to the
ecosystem digester verbatim — bounded so the prompt stays inside the
``agents_default`` context window (per-stream cap below).
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from tesseract.paths import CONFIG_DIR

log = logging.getLogger(__name__)

DEFAULT_SINCE_DAYS = 7
MAX_PER_STREAM = 20
MAX_BODY_PREVIEW = 600
MAX_RATIONALE_PREVIEW = 400
MAX_SNAPSHOT_PREVIEW = 600
MAX_PROVIDER_PREVIEW = 800

# Watchlist (AU-11a) — source-name → canonical URL list. Read once per
# pre-fetch and used to enrich docs-watch rows with the URLs they were
# originally extracted from. AU-24 source-URL provenance lands through
# this lookup (audit 2026-05-20 §M4) so the ecosystem-digester agent
# can cite real sources instead of inventing URLs.
_DEFAULT_WATCHLIST_PATH = CONFIG_DIR / "autonomy-watchlist.yaml"

# Liberal URL match: http(s)://host[/...]. Stops at whitespace, parens,
# angle brackets, and common trailing punctuation. Bounded so a 10k-byte
# body can't pin re2.
_URL_RE = re.compile(r"https?://[^\s)>\]\"']+")


def collect_ecosystem_inputs(
    *,
    home: Path,
    target_date: date,
    since_days: int = DEFAULT_SINCE_DAYS,
    watchlist_path: Path | None = None,
) -> dict[str, Any]:
    """Read the four streams' last-``since_days`` windows.

    Each stream is independently fail-soft — a missing directory or a
    malformed file yields an empty list for that stream rather than
    aborting the whole collection. The renderer treats an all-empty
    return as "drop the ecosystem section".

    ``watchlist_path`` overrides the default autonomy-watchlist.yaml
    location for tests; when ``None``, the repo-relative path is used
    so production runs pick up the same file the (now-retired) docs-watch
    producer wrote snapshots for. The watchlist enriches docs-watch rows
    with their canonical source URLs (audit 2026-05-20 §M4 provenance fix).
    """
    cutoff = datetime.combine(
        target_date - timedelta(days=since_days),
        datetime.min.time(),
        tzinfo=timezone.utc,
    )
    watchlist = _load_watchlist_urls(watchlist_path)
    return {
        "since_days": since_days,
        "target_date": target_date.isoformat(),
        "memory_signals": _read_memory_signals(home, cutoff),
        "memory_leaves": _read_memory_leaves(home, cutoff),
        "docs_watch": _read_docs_watch(home, cutoff, watchlist=watchlist),
        "provider_watch": _read_provider_watch(home, cutoff, target_date),
    }


def _read_memory_signals(home: Path, cutoff: datetime) -> list[dict[str, Any]]:
    agenda_root = home / "agenda"
    if not agenda_root.exists():
        return []
    out: list[dict[str, Any]] = []
    for jf in _iter_json_under(agenda_root):
        record = _load_json(jf)
        if record is None:
            continue
        if str(record.get("source") or "").strip().lower() != "memory_signal":
            continue
        created_at = _parse_iso(record.get("created_at"))
        if created_at is None or created_at < cutoff:
            continue
        # Scan the FULL rationale + goal for URLs before truncating the
        # preview — otherwise a URL straddling MAX_RATIONALE_PREVIEW
        # gets emitted as a broken half-URL.
        rationale_full = str(record.get("rationale") or "")
        goal = str(record.get("goal") or "").strip()
        url = _first_url(rationale_full) or _first_url(goal)
        rationale = rationale_full[:MAX_RATIONALE_PREVIEW]
        kind = _extract_signal_kind(rationale, goal)
        out.append(
            {
                "created_at": created_at.isoformat(),
                "kind": kind,
                "goal": goal,
                "rationale": rationale,
                # Provenance — strategist / digester needs a citable URL.
                # Memory signals don't carry a structured URL field;
                # scan the rationale (which may embed one) and fall
                # back to empty string when none is present.
                "url": url,
            }
        )
    out.sort(key=lambda r: r["created_at"], reverse=True)
    return out[:MAX_PER_STREAM]


def _read_memory_leaves(home: Path, cutoff: datetime) -> list[dict[str, Any]]:
    leaves_root = home / "memory-store" / "leaves"
    if not leaves_root.exists():
        return []
    out: list[dict[str, Any]] = []
    for jf in _iter_json_under(leaves_root):
        record = _load_json(jf)
        if record is None:
            continue
        created_at = _parse_iso(record.get("created_at"))
        if created_at is None or created_at < cutoff:
            continue
        # Scan the FULL body + source for URLs before truncating the
        # preview — otherwise a URL straddling MAX_BODY_PREVIEW gets
        # emitted as a broken half-URL.
        body_full = str(record.get("body") or "")
        body = body_full[:MAX_BODY_PREVIEW]
        entities = record.get("entities") or []
        if not isinstance(entities, list):
            entities = []
        source = str(record.get("source") or "").strip()
        # Leaf records carry no structured URL field; pull the first
        # URL out of the body or source string when present so the
        # digester can cite real upstream pages instead of inventing
        # them. Empty string when neither side carries a URL.
        url = _first_url(body_full) or _first_url(source)
        out.append(
            {
                "created_at": created_at.isoformat(),
                "source": source,
                "title": str(record.get("title") or "").strip(),
                "body": body,
                "entities": [str(e) for e in entities[:10]],
                "state": str(record.get("state") or "").strip(),
                "url": url,
            }
        )
    out.sort(key=lambda r: r["created_at"], reverse=True)
    return out[:MAX_PER_STREAM]


def _read_docs_watch(
    home: Path,
    cutoff: datetime,
    *,
    watchlist: dict[str, list[str]] | None = None,
) -> list[dict[str, Any]]:
    snap_dir = home / "autonomy" / "watchlist-snapshots"
    if not snap_dir.exists():
        return []
    watchlist = watchlist or {}
    out: list[dict[str, Any]] = []
    for md in sorted(snap_dir.glob("*.md")):
        try:
            mtime = datetime.fromtimestamp(md.stat().st_mtime, tz=timezone.utc)
        except OSError:
            continue
        if mtime < cutoff:
            continue
        try:
            text = md.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        preview = text.strip()[:MAX_SNAPSHOT_PREVIEW]
        # Provenance — prefer the canonical URLs that the watchlist
        # extracted from (operator-curated, stable across snapshot
        # rewrites). Fall back to URLs embedded in the snapshot body
        # when the watchlist entry is missing.
        source_urls = list(watchlist.get(md.stem) or [])
        if not source_urls:
            embedded = _first_url(text)
            if embedded:
                source_urls = [embedded]
        out.append(
            {
                "source": md.stem,
                "last_modified": mtime.isoformat(),
                "preview": preview,
                "source_urls": source_urls,
                "url": source_urls[0] if source_urls else "",
            }
        )
    out.sort(key=lambda r: r["last_modified"], reverse=True)
    return out[:MAX_PER_STREAM]


def _read_provider_watch(
    home: Path, cutoff: datetime, target_date: date
) -> list[dict[str, Any]]:
    digests_dir = home / "memory-store" / "daily" / "providers"
    if not digests_dir.exists():
        return []
    out: list[dict[str, Any]] = []
    for md in sorted(digests_dir.glob("*.md"), reverse=True):
        try:
            entry_date = date.fromisoformat(md.stem)
        except ValueError:
            continue
        entry_dt = datetime.combine(
            entry_date, datetime.min.time(), tzinfo=timezone.utc
        )
        if entry_dt < cutoff:
            continue
        if entry_date > target_date:
            continue
        try:
            text = md.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # Scan the FULL digest for a URL before truncating the
        # preview — otherwise a URL straddling MAX_PROVIDER_PREVIEW
        # gets emitted as a broken half-URL.
        stripped = text.strip()
        preview = stripped[:MAX_PROVIDER_PREVIEW]
        out.append(
            {
                "date": entry_date.isoformat(),
                "preview": preview,
                # Provider digests routinely paste an upstream URL in
                # the rendered markdown; surface the first one so the
                # ecosystem-digester can cite it. Empty when the digest
                # is prose-only.
                "url": _first_url(stripped),
            }
        )
    return out[:MAX_PER_STREAM]


def has_any_signal(payload: dict[str, Any]) -> bool:
    """True when at least one stream returned a row. The renderer skips
    the LLM round-trip on an all-empty payload — anything the agent
    could produce would be hallucination."""
    for key in ("memory_signals", "memory_leaves", "docs_watch", "provider_watch"):
        items = payload.get(key) or []
        if items:
            return True
    return False


def _iter_json_under(root: Path):
    if not root.exists():
        return
    for path in root.rglob("*.json"):
        if path.is_file():
            yield path


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        log.info("ecosystem: skipping malformed json %s", path.name)
        return None
    return data if isinstance(data, dict) else None


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _first_url(text: str | None) -> str:
    """Return the first http(s) URL in ``text`` (stripped of common
    trailing punctuation), or the empty string when none is found."""
    if not text:
        return ""
    match = _URL_RE.search(text)
    if not match:
        return ""
    url = match.group(0)
    # Strip terminal punctuation that the bounded regex may have
    # consumed inside the URL itself (sentence-ending commas/periods,
    # closing-bracket-pair confusion). Keep query strings intact.
    while url and url[-1] in ".,;:!?":
        url = url[:-1]
    return url


def _load_watchlist_urls(override: Path | None) -> dict[str, list[str]]:
    """Read ``autonomy-watchlist.yaml`` and return ``{source_name: [urls…]}``.

    Returns an empty dict when the file is absent or unparseable. We don't
    want a malformed watchlist to abort the whole ecosystem prefetch — the
    docs-watch rows just lose their canonical URL hint, which is the same
    state as before the AU-24 provenance fix.
    """
    path = override
    if path is None:
        # Resolve relative to the tesseract package root so a launcher
        # cwd anywhere on disk still finds the bundled watchlist.
        try:
            from tesseract.paths import CONFIG_DIR
            path = (CONFIG_DIR / "autonomy-watchlist.yaml")
        except Exception:  # noqa: BLE001
            path = _DEFAULT_WATCHLIST_PATH
    if not path.exists():
        return {}
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        log.info("ecosystem: watchlist read failed at %s", path)
        return {}
    sources = raw.get("sources") if isinstance(raw, dict) else None
    if not isinstance(sources, list):
        return {}
    out: dict[str, list[str]] = {}
    for row in sources:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        urls = row.get("urls") or []
        if not name or not isinstance(urls, list):
            continue
        clean = [str(u).strip() for u in urls if isinstance(u, str) and u.strip()]
        if clean:
            out[name] = clean
    return out


def _extract_signal_kind(rationale: str, goal: str) -> str:
    for fragment in rationale.split("|"):
        s = fragment.strip()
        if s.startswith("memory_signal:"):
            return s.split(":", 1)[1].strip() or "memory_signal"
    if goal.startswith("review memory signal"):
        head = goal.split(":", 1)
        if len(head) == 2 and "(" in head[0]:
            kind = head[0].rsplit("(", 1)[-1].rstrip(")").strip()
            if kind:
                return kind
    return "memory_signal"


__all__ = [
    "collect_ecosystem_inputs",
    "has_any_signal",
    "DEFAULT_SINCE_DAYS",
    "MAX_PER_STREAM",
]

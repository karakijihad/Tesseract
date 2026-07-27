"""VaultRawWatchJob — AU-22.

Watches `<TESSERACT_HOME>/vault/raw/<YYYYMMDD>/` for new operator-dropped
research material and routes each file through one of two paths:

* AUTO — file passes all three safety filters (allowed type, size cap,
  no prior failure). The job calls the inline ingest helper, appends a
  cursor row, and emits one batched `nudge` workspace event summarising
  the tick.

* ASK — file fails a filter. The job bundles failing files into a single
  `vault_raw_ingest_batch` workspace event; the operator approves or
  rejects via the inbox. The approve handler in
  `mirror/server/routes/workspace.py` calls `apply_ask_batch` here to
  run the same ingest helper.

Files never move out of their date folder regardless of path taken;
the cursor JSONL is the only source of truth for what has been ingested.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from fnmatch import fnmatch
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from tesseract.memory._shortcut_extractor import extract_url
from tesseract.memory.vault_indexer import VaultIndexer
from tesseract.memory.vault_manager import VaultManager
from tesseract.paths import CONFIG_DIR, TESSERACT_HOME
from tesseract.scheduler.base_job import BaseJob
from tesseract.scheduler.types import JobContext, JobResult
from tesseract.workspace_events.events import EventStore, WorkspaceEvent

log = logging.getLogger(__name__)

DEFAULT_MAX_AUTO_SIZE_MB = 50
DEFAULT_BATCH_SIZE_MAX = 50
DEFAULT_EXCLUDED_GLOBS: tuple[str, ...] = (
    "~$*",
    ".DS_Store",
    "Thumbs.db",
    "*.tmp",
    "*.meta.yaml",
)
SHORTCUT_EXTENSIONS = frozenset({".url", ".lnk"})
_FOLDER_RE = re.compile(r"^\d{8}$")
_SHA_CHUNK = 1 << 20  # 1 MiB


@dataclass(frozen=True)
class RawWatchConfig:
    enabled: bool
    mode: str  # "auto" | "ask_all"
    max_auto_size_mb: int
    auto_url_allowlist: tuple[str, ...]
    batch_size_max: int
    excluded_globs: tuple[str, ...]
    nonconforming_folders: str  # "log" | "warn" | "error"
    notify_on_empty: bool


@dataclass(frozen=True)
class _Candidate:
    folder: str
    relpath: str  # forward-slash, vault-raw-relative (e.g. "20260518/foo.pdf")
    abs_path: Path
    sha256: str
    size_bytes: int


@dataclass
class _TickReport:
    folders_scanned: int = 0
    skipped_nonconforming: list[str] = field(default_factory=list)
    auto_ingested: list[str] = field(default_factory=list)
    auto_failed: list[str] = field(default_factory=list)
    ask_queued: list[dict[str, Any]] = field(default_factory=list)
    batch_event_id: str | None = None
    inform_event_id: str | None = None


class VaultRawWatchJob(BaseJob):
    uses_llm = False

    async def run(self, ctx: JobContext) -> JobResult:
        t0 = time.monotonic()
        try:
            cfg = _resolve_config(ctx)
            if not cfg.enabled:
                return JobResult(
                    job_name=ctx.job_name,
                    run_id=ctx.run_id,
                    ok=True,
                    detail="raw_watch disabled",
                    duration_ms=(time.monotonic() - t0) * 1000.0,
                )

            vault_manager = _resolve_vault_manager(ctx)
            indexer = _resolve_vault_indexer(ctx)
            cursor_path = _resolve_cursor_path(ctx)
            event_store = _resolve_event_store(ctx)

            seen = _read_cursor_keys(cursor_path)

            folders, nonconforming = _enumerate_folders(vault_manager.raw_dir)
            report = _TickReport(
                folders_scanned=len(folders),
                skipped_nonconforming=nonconforming,
            )

            _log_nonconforming(nonconforming, cfg.nonconforming_folders)

            candidates = _gather_candidates(folders, cfg.excluded_globs, seen)
            if not candidates:
                _maybe_finalise_empty(report, event_store, cfg, ctx)
                return JobResult(
                    job_name=ctx.job_name,
                    run_id=ctx.run_id,
                    ok=True,
                    detail=f"folders={report.folders_scanned} no_new_files",
                    payload=_payload(report),
                    duration_ms=(time.monotonic() - t0) * 1000.0,
                )

            auto_batch: list[_Candidate] = []
            ask_batch: list[tuple[_Candidate, str]] = []  # (candidate, ask_reason)

            for cand in candidates[: cfg.batch_size_max]:
                reason = _classify(cand, cfg)
                if reason is None:
                    auto_batch.append(cand)
                else:
                    ask_batch.append((cand, reason))

            now = ctx.fired_at.astimezone(timezone.utc)
            for cand in auto_batch:
                try:
                    await _ingest_one(cand, vault_manager, indexer, decision="auto", now=now)
                    _append_cursor(
                        cursor_path,
                        cand=cand,
                        ingest_status="ingested",
                        decision="auto",
                        reason=None,
                        when=now,
                    )
                    report.auto_ingested.append(cand.relpath)
                except Exception as exc:  # noqa: BLE001 — per-file isolation
                    log.warning("raw_watch auto-ingest failed for %s: %s", cand.relpath, exc)
                    _append_cursor(
                        cursor_path,
                        cand=cand,
                        ingest_status="failed",
                        decision="auto",
                        reason=str(exc)[:240],
                        when=now,
                    )
                    report.auto_failed.append(cand.relpath)

            if ask_batch:
                files_payload: list[dict[str, Any]] = []
                for cand, reason in ask_batch:
                    files_payload.append(
                        {
                            "folder": cand.folder,
                            "relpath": cand.relpath,
                            "sha256": cand.sha256,
                            "size_bytes": cand.size_bytes,
                            "suggested_path": f"raw/{cand.relpath}",
                            "ask_reason": reason,
                            "extractor_preview": _preview_for(cand, vault_manager, indexer),
                        }
                    )
                ev = _build_ask_event(files_payload, when=now)
                event_store.append_event(ev)
                report.batch_event_id = ev.event_id
                report.ask_queued = files_payload

            if report.auto_ingested or report.auto_failed:
                inform = _build_inform_event(report, when=now)
                event_store.append_event(inform)
                report.inform_event_id = inform.event_id

            return JobResult(
                job_name=ctx.job_name,
                run_id=ctx.run_id,
                ok=True,
                detail=(
                    f"folders={report.folders_scanned} "
                    f"auto={len(report.auto_ingested)} "
                    f"ask={len(report.ask_queued)} "
                    f"failed={len(report.auto_failed)}"
                ),
                payload=_payload(report),
                duration_ms=(time.monotonic() - t0) * 1000.0,
            )
        except Exception as exc:  # noqa: BLE001 — handler contract forbids raising
            log.exception("vault_raw_watch crashed")
            return JobResult(
                job_name=ctx.job_name,
                run_id=ctx.run_id,
                ok=False,
                detail=f"unhandled: {exc!r}",
                duration_ms=(time.monotonic() - t0) * 1000.0,
            )


# ─── Shared apply helper consumed by the approve handler ─────────────


async def apply_ask_batch(
    *,
    files: list[dict[str, Any]],
    decisions: dict[str, str],  # {relpath: "approved"|"denied"}
    vault_manager: VaultManager,
    indexer: VaultIndexer | None,
    cursor_path: Path,
    when: datetime | None = None,
) -> dict[str, Any]:
    """Apply operator decisions on a previously-queued ASK batch.

    The approve handler in `routes/workspace.py` calls this with the
    files payload from the original event and a per-file decision map
    (default `approved` when missing). Per-file failures are logged to
    the cursor jsonl but do not abort the batch.

    Returns a small summary dict the handler echoes back to the UI.
    """
    when = when or datetime.now(timezone.utc)
    ingested: list[str] = []
    denied: list[str] = []
    failed: list[tuple[str, str]] = []
    for entry in files:
        relpath = str(entry.get("relpath") or "").strip()
        if not relpath:
            continue
        decision = decisions.get(relpath, "approved")
        folder = str(entry.get("folder") or "").strip()
        sha = str(entry.get("sha256") or "").strip()
        size = int(entry.get("size_bytes") or 0)
        if not folder or not sha:
            continue
        abs_path = vault_manager.raw_dir / Path(relpath.replace("\\", "/"))
        cand = _Candidate(
            folder=folder,
            relpath=relpath,
            abs_path=abs_path,
            sha256=sha,
            size_bytes=size,
        )
        if decision == "denied":
            _append_cursor(
                cursor_path,
                cand=cand,
                ingest_status="denied",
                decision="ask",
                reason="operator_denied",
                when=when,
            )
            denied.append(relpath)
            continue
        try:
            await _ingest_one(cand, vault_manager, indexer, decision="ask", now=when)
        except Exception as exc:  # noqa: BLE001
            log.warning("raw_watch ask-ingest failed for %s: %s", relpath, exc)
            _append_cursor(
                cursor_path,
                cand=cand,
                ingest_status="failed",
                decision="ask",
                reason=str(exc)[:240],
                when=when,
            )
            failed.append((relpath, str(exc)[:240]))
            continue
        _append_cursor(
            cursor_path,
            cand=cand,
            ingest_status="ingested",
            decision="ask",
            reason=None,
            when=when,
        )
        ingested.append(relpath)
    return {
        "ingested": ingested,
        "denied": denied,
        "failed": failed,
    }


# ─── Ingest helper (no copy — file stays in raw/) ─────────────────────


async def _ingest_one(
    cand: _Candidate,
    vault_manager: VaultManager,
    indexer: VaultIndexer | None,
    *,
    decision: str,
    now: datetime,
) -> None:
    """Side-effect contract: write a `.meta.yaml` sidecar next to the
    file and index it for retrieval. The file is intentionally NOT
    copied via :meth:`VaultManager.file_to_vault` — operator-dropped
    files stay in `vault/raw/<YYYYMMDD>/` exactly where they landed,
    operator-visible and operator-mutable (per phase plan §2: "Files
    **stay in their date folder after ingest**"). This is a deliberate
    deviation from the immutable-vault path used by the manual
    `vault_ingest` tool, which copies the source into the vault tree
    and chmod-locks it. The cursor JSONL is the durable record of what
    has been ingested; the file is just where it is.
    """
    vault_rel = f"raw/{cand.relpath}"
    title = cand.abs_path.stem.replace("-", " ").replace("_", " ").title()

    meta: dict[str, Any] = {
        "source_type": cand.abs_path.suffix.lstrip(".").lower(),
        "ingested_at": now.isoformat(),
        "ingest_decision": decision,
        "content_hash": f"sha256:{cand.sha256}",
        "tags": [],
        "notes": "vault_raw_watch auto-ingest" if decision == "auto" else "vault_raw_watch ask-approved",
    }
    if cand.abs_path.suffix.lower() in SHORTCUT_EXTENSIONS:
        target = extract_url(cand.abs_path)
        if target.url:
            meta["source_url"] = target.url

    vault_manager.write_meta_sidecar(vault_rel, meta)

    if indexer is not None:
        await indexer.index_vault_file(vault_rel, title, cand.abs_path)


def _preview_for(cand: _Candidate, vault_manager: VaultManager, indexer: VaultIndexer | None) -> str:
    """Cheap, no-side-effect preview for the ASK row.

    Reads at most 240 bytes from the candidate; full extraction happens
    only on Approve so the inbox stays cheap to render.
    """
    suffix = cand.abs_path.suffix.lower()
    if suffix in SHORTCUT_EXTENSIONS:
        target = extract_url(cand.abs_path)
        if target.url:
            return f"shortcut → {target.url}"[:240]
        return "shortcut (no URL recovered)"
    try:
        sample = cand.abs_path.read_bytes()[:240]
        text = sample.decode("utf-8", errors="replace").strip().replace("\n", " ")
        return text[:240] if text else "(binary)"
    except OSError:
        return "(unreadable)"


# ─── Candidate enumeration + dedup ───────────────────────────────────


def _enumerate_folders(raw_dir: Path) -> tuple[list[Path], list[str]]:
    if not raw_dir.exists():
        return [], []
    folders: list[Path] = []
    nonconforming: list[str] = []
    for child in sorted(raw_dir.iterdir(), reverse=True):
        if not child.is_dir():
            continue
        if _FOLDER_RE.match(child.name):
            folders.append(child)
        else:
            nonconforming.append(child.name)
    return folders, nonconforming


def _gather_candidates(
    folders: list[Path],
    excluded_globs: tuple[str, ...],
    seen: set[tuple[str, str]],
) -> list[_Candidate]:
    out: list[_Candidate] = []
    for folder in folders:
        for path in sorted(folder.iterdir()):
            if not path.is_file():
                continue
            if _is_excluded(path.name, excluded_globs):
                continue
            sha = _file_sha256(path)
            if not sha:
                continue
            key = (folder.name, sha)
            if key in seen:
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            out.append(
                _Candidate(
                    folder=folder.name,
                    relpath=f"{folder.name}/{path.name}",
                    abs_path=path,
                    sha256=sha,
                    size_bytes=size,
                )
            )
    return out


def _classify(cand: _Candidate, cfg: RawWatchConfig) -> str | None:
    """Return None when the candidate may auto-ingest; otherwise a reason.

    Note: the "prior failure" check from the phase doc draft folded into
    the SHA-dedup at `_read_cursor_keys` — any row (success, deny, or
    failure) marks the (folder, sha) pair as seen, so a failed file is
    never re-proposed by either path. Operator must replace the file
    (producing a new SHA) for a retry. The cleaner contract — see the
    INDEX.md planning note 2026-05-18 — eliminates a separate retry
    cooldown timer.
    """
    if cfg.mode == "ask_all":
        return "mode=ask_all"
    suffix = cand.abs_path.suffix.lower()
    if suffix in SHORTCUT_EXTENSIONS:
        if not _shortcut_in_allowlist(cand, cfg.auto_url_allowlist):
            return ".url/.lnk cost gate"
    cap_bytes = cfg.max_auto_size_mb * 1024 * 1024
    if cap_bytes > 0 and cand.size_bytes > cap_bytes:
        return f"size>{cfg.max_auto_size_mb}MB"
    return None


def _shortcut_in_allowlist(cand: _Candidate, allowlist: tuple[str, ...]) -> bool:
    if not allowlist:
        return False
    target = extract_url(cand.abs_path)
    if not target.url:
        return False
    return any(target.url.startswith(prefix) for prefix in allowlist)


def _is_excluded(name: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch(name, pattern) for pattern in patterns)


def _file_sha256(path: Path) -> str:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as fh:
            while True:
                chunk = fh.read(_SHA_CHUNK)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()
    except OSError as exc:
        log.info("raw_watch: sha read failed for %s (%s)", path.name, exc)
        return ""


# ─── Cursor JSONL ─────────────────────────────────────────────────────


def _append_cursor(
    cursor_path: Path,
    *,
    cand: _Candidate,
    ingest_status: str,
    decision: str,
    reason: str | None,
    when: datetime,
) -> None:
    row = {
        "folder": cand.folder,
        "relpath": cand.relpath,
        "sha256": cand.sha256,
        "size_bytes": cand.size_bytes,
        "ingested_at": when.isoformat(),
        "ingest_status": ingest_status,
        "decision": decision,
    }
    if reason:
        row["reason"] = reason
    cursor_path.parent.mkdir(parents=True, exist_ok=True)
    with cursor_path.open("a", encoding="utf-8", newline="") as fh:
        fh.write(json.dumps(row) + "\n")


def _read_cursor_keys(cursor_path: Path) -> set[tuple[str, str]]:
    """All (folder, sha) seen — any ingest_status counts as seen so the
    watcher never re-proposes a file the operator has already touched.
    Per AU-22 contract: failed files require operator replacement (new
    SHA) before another attempt."""
    if not cursor_path.exists():
        return set()
    keys: set[tuple[str, str]] = set()
    try:
        for line in cursor_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            folder = str(row.get("folder") or "")
            sha = str(row.get("sha256") or "")
            if folder and sha:
                keys.add((folder, sha))
    except OSError:
        return set()
    return keys


# ─── Workspace events ────────────────────────────────────────────────


def _build_ask_event(files: list[dict[str, Any]], *, when: datetime) -> WorkspaceEvent:
    folders = sorted({entry["folder"] for entry in files})
    return WorkspaceEvent.new(
        kind="vault_raw_ingest_batch",
        source="tars",
        title=f"Vault inbox — {len(files)} file(s) await approval",
        summary=(
            f"{len(files)} file(s) in {', '.join(folders)} need operator approval "
            "before vault ingest. Reasons listed per file in the payload."
        ),
        payload={
            "files": files,
            "folders": folders,
            "queued_at": when.isoformat(),
        },
    )


def _build_inform_event(report: _TickReport, *, when: datetime) -> WorkspaceEvent:
    folders = sorted(
        {entry.split("/", 1)[0] for entry in (report.auto_ingested + report.auto_failed)}
    )
    summary_bits = [f"{len(report.auto_ingested)} auto-ingested"]
    if report.auto_failed:
        summary_bits.append(f"{len(report.auto_failed)} failed")
    summary_bits.append(f"folders {', '.join(folders) or '(none)'}")
    return WorkspaceEvent.new(
        kind="nudge",
        source="tars",
        title="Vault raw-watch — auto-ingest summary",
        summary=" · ".join(summary_bits),
        payload={
            "auto_ingested": report.auto_ingested,
            "auto_failed": report.auto_failed,
            "folders": folders,
            "tick_at": when.isoformat(),
        },
    )


def _maybe_finalise_empty(
    report: _TickReport,
    event_store: EventStore,
    cfg: RawWatchConfig,
    ctx: JobContext,
) -> None:
    """When `notify_on_empty` is True surface a quiet nudge so the
    operator can confirm the watcher ran. Defaults silent."""
    if not cfg.notify_on_empty:
        return
    ev = WorkspaceEvent.new(
        kind="nudge",
        source="tars",
        title="Vault raw-watch — empty tick",
        summary=(
            f"folders_scanned={report.folders_scanned}, "
            f"skipped_nonconforming={len(report.skipped_nonconforming)}"
        ),
        payload={
            "folders_scanned": report.folders_scanned,
            "skipped_nonconforming": report.skipped_nonconforming,
            "tick_at": ctx.fired_at.astimezone(timezone.utc).isoformat(),
        },
    )
    event_store.append_event(ev)
    report.inform_event_id = ev.event_id


def _log_nonconforming(skipped: list[str], policy: str) -> None:
    if not skipped:
        return
    msg = f"vault_raw_watch: skipping non-YYYYMMDD folder(s): {', '.join(skipped)}"
    if policy == "error":
        log.error(msg)
    elif policy == "warn":
        log.warning(msg)
    else:
        log.info(msg)


def _payload(report: _TickReport) -> dict[str, Any]:
    return {
        "folders_scanned": report.folders_scanned,
        "skipped_nonconforming": report.skipped_nonconforming,
        "auto_ingested": report.auto_ingested,
        "auto_failed": report.auto_failed,
        "ask_queued_count": len(report.ask_queued),
        "batch_event_id": report.batch_event_id,
        "inform_event_id": report.inform_event_id,
    }


# ─── Config + dependency resolution ──────────────────────────────────


def _cfg_get(ctx: JobContext, key: str) -> Any | None:
    if isinstance(ctx.config, dict):
        return ctx.config.get(key)
    return None


def _resolve_home() -> Path:
    """Resolve TESSERACT_HOME at call time so tests' monkeypatched env
    points the watcher at tmp_path rather than the production tree."""
    override = os.environ.get("TESSERACT_HOME")
    return Path(override).resolve() if override else TESSERACT_HOME


def _resolve_vault_manager(ctx: JobContext) -> VaultManager:
    injected = _cfg_get(ctx, "vault_manager")
    if isinstance(injected, VaultManager):
        return injected
    return VaultManager(vault_root=_resolve_home() / "vault")


def _resolve_vault_indexer(ctx: JobContext) -> VaultIndexer | None:
    injected = _cfg_get(ctx, "vault_indexer")
    if isinstance(injected, VaultIndexer):
        return injected
    if injected is None and isinstance(ctx.config, dict) and "vault_indexer" in ctx.config:
        return None
    app = ctx.app
    if app is not None and hasattr(app, "get"):
        registry = app.get("tool_registry")
        if registry is not None:
            tool = getattr(registry, "get", lambda _name: None)("vault_ingest")
            indexer = getattr(tool, "_indexer", None)
            if isinstance(indexer, VaultIndexer):
                return indexer
    return None


def _resolve_cursor_path(ctx: JobContext) -> Path:
    override = _cfg_get(ctx, "cursor_path")
    if override:
        return Path(override)
    return _resolve_home() / "autonomy" / "vault-raw-cursors.jsonl"


def _resolve_event_store(ctx: JobContext) -> EventStore:
    injected = _cfg_get(ctx, "event_store")
    if isinstance(injected, EventStore):
        return injected
    app = ctx.app
    if app is not None and hasattr(app, "get"):
        store = app.get("workspace_event_store")
        if isinstance(store, EventStore):
            return store
    return EventStore(_resolve_home() / "logs")


def _resolve_config(ctx: JobContext) -> RawWatchConfig:
    cfg_dict: dict[str, Any] | None = _cfg_get(ctx, "raw_watch")
    if cfg_dict is None:
        cfg_dict = _load_raw_watch_from_yaml(ctx)
    cfg_dict = cfg_dict or {}

    enabled = bool(cfg_dict.get("enabled", True))
    mode = str(cfg_dict.get("mode", "auto")).lower()
    if mode not in {"auto", "ask_all"}:
        log.warning("raw_watch: unknown mode=%r — falling back to 'auto'", mode)
        mode = "auto"
    max_auto_size_mb = int(cfg_dict.get("max_auto_size_mb", DEFAULT_MAX_AUTO_SIZE_MB))
    raw_allow = cfg_dict.get("auto_url_allowlist") or []
    auto_url_allowlist = tuple(str(p) for p in raw_allow if isinstance(p, str) and p.strip())
    batch_size_max = int(cfg_dict.get("batch_size_max", DEFAULT_BATCH_SIZE_MAX))
    raw_excluded = cfg_dict.get("excluded_globs")
    if raw_excluded is None:
        excluded_globs = DEFAULT_EXCLUDED_GLOBS
    else:
        excluded_globs = tuple(str(p) for p in raw_excluded if isinstance(p, str))
    nonconforming = str(cfg_dict.get("nonconforming_folders", "log")).lower()
    if nonconforming not in {"log", "warn", "error"}:
        nonconforming = "log"
    notify_on_empty = bool(cfg_dict.get("notify_on_empty", False))

    return RawWatchConfig(
        enabled=enabled,
        mode=mode,
        max_auto_size_mb=max_auto_size_mb,
        auto_url_allowlist=auto_url_allowlist,
        batch_size_max=batch_size_max,
        excluded_globs=excluded_globs,
        nonconforming_folders=nonconforming,
        notify_on_empty=notify_on_empty,
    )


def _load_raw_watch_from_yaml(ctx: JobContext) -> dict[str, Any] | None:
    """Read `CONFIG_DIR/vault.yaml::raw_watch` if no inline override.

    Mirror's lifecycle resolves vault config once at boot and stashes it
    on the app dict; the scheduler context can pass that through via
    `ctx.config["raw_watch"]` for hot-reload. When the operator runs the
    job from CLI we fall back to reading the YAML directly so the
    fixture-style "no app, no inline config" path still works.
    """
    app = ctx.app
    if app is not None and hasattr(app, "get"):
        cfg = app.get("vault_config")
        if cfg is not None:
            block = getattr(cfg, "raw_watch", None) or (
                cfg.get("raw_watch") if isinstance(cfg, dict) else None
            )
            if isinstance(block, dict):
                return block
    target = CONFIG_DIR / "vault.yaml"
    if not target.exists():
        return None
    try:
        raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return None
    block = raw.get("raw_watch") if isinstance(raw, dict) else None
    return block if isinstance(block, dict) else None


__all__ = [
    "DEFAULT_BATCH_SIZE_MAX",
    "DEFAULT_EXCLUDED_GLOBS",
    "DEFAULT_MAX_AUTO_SIZE_MB",
    "RawWatchConfig",
    "VaultRawWatchJob",
    "apply_ask_batch",
]

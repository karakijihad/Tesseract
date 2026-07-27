import { useEffect, useMemo, useRef, useState } from 'react';
import { useSessionStore } from '../../stores/session';
import { useUIStore } from '../../stores/ui';
import { sendCommand } from '../../lib/commands';
import { Hint } from '../ui/Hint';
import {
  fetchSessionPreview,
  postSessionDuplicate,
  postSessionRename,
  type SessionPreview,
} from '../../lib/api';

function relativeTime(iso: string): string {
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return '';
  const diff = (Date.now() - then) / 1000;
  if (diff < 60) return 'just now';
  if (diff < 3600) return `${Math.round(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.round(diff / 3600)}h ago`;
  return `${Math.round(diff / 86400)}d ago`;
}

// Stale = older than yesterday-midnight. Mirrors the auto-resume cutoff in
// `websocket.ts` and `prompt.py:DAILY_FILES_TO_LOAD = 2`.
function isStale(startedAt: string | null | undefined): boolean {
  if (!startedAt) return false;
  const ts = Date.parse(startedAt);
  if (Number.isNaN(ts)) return false;
  const cutoff = new Date();
  cutoff.setHours(0, 0, 0, 0);
  cutoff.setDate(cutoff.getDate() - 1);
  return ts < cutoff.getTime();
}

export function SessionDrawer() {
  const open = useUIStore((s) => s.drawerOpen);
  const setOpen = useUIStore((s) => s.setDrawerOpen);
  const sessions = useSessionStore((s) => s.sessions);
  const saveName = useSessionStore((s) => s.saveName);
  const fetchList = useSessionStore((s) => s.fetchList);
  const archive = useSessionStore((s) => s.archive);
  const archiveLoaded = useSessionStore((s) => s.archiveLoaded);
  const fetchArchive = useSessionStore((s) => s.fetchArchive);
  const [archiveOpen, setArchiveOpen] = useState(false);
  const asideRef = useRef<HTMLDivElement>(null);

  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [confirmDelete, setConfirmDelete] = useState(false);
  const selectAllRef = useRef<HTMLInputElement>(null);

  const allIds = useMemo(() => sessions.map((s) => s.session_id), [sessions]);
  const allSelected = allIds.length > 0 && allIds.every((id) => selected.has(id));
  const someSelected = selected.size > 0 && !allSelected;

  useEffect(() => {
    if (selectAllRef.current) selectAllRef.current.indeterminate = someSelected;
  }, [someSelected]);

  const toggleAll = () => {
    setSelected(allSelected ? new Set() : new Set(allIds));
    setConfirmDelete(false);
  };

  useEffect(() => {
    if (!open) return;
    fetchList();
    sendCommand('/sessions');
    asideRef.current?.focus();
  }, [open, fetchList]);

  useEffect(() => {
    if (!open) {
      setSelected(new Set());
      setConfirmDelete(false);
    }
  }, [open]);

  useEffect(() => {
    if (confirmDelete) {
      const id = window.setTimeout(() => setConfirmDelete(false), 4000);
      return () => window.clearTimeout(id);
    }
    return undefined;
  }, [confirmDelete]);

  if (!open) return null;

  const toggleRow = (id: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
    setConfirmDelete(false);
  };

  const loadSelected = () => {
    if (selected.size !== 1) return;
    const id = [...selected][0];
    sendCommand('/load', ` ${id}`);
    setOpen(false);
  };

  const deleteSelected = () => {
    if (selected.size === 0) return;
    if (!confirmDelete) {
      setConfirmDelete(true);
      return;
    }
    for (const id of selected) sendCommand('/delete', ` ${id}`);
    setSelected(new Set());
    setConfirmDelete(false);
  };

  const compactSelected = () => {
    if (selected.size === 0) return;
    for (const id of selected) sendCommand('/compact_file', ` ${id}`);
  };

  const saveCurrent = () => {
    sendCommand('/save');
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    if (e.key === 'Escape') {
      e.preventDefault();
      setOpen(false);
    }
  };

  const canLoad = selected.size === 1;
  const canBatch = selected.size >= 1;
  const deleteLabel = confirmDelete
    ? `Delete ${selected.size} — click again to confirm`
    : `Delete (${selected.size})`;

  return (
    <>
      <button
        type="button"
        className="drawer-scrim"
        onClick={() => setOpen(false)}
        aria-label="Close sessions"
      />
      <aside
        ref={asideRef}
        className="session-drawer"
        role="dialog"
        aria-label="Sessions"
        tabIndex={-1}
        onKeyDown={handleKeyDown}
      >
        <header className="session-drawer-head">
          <span className="session-drawer-title">Sessions</span>
          <Hint label="Save current session (/save)" position="bottom" maxWidth={200}>
            <button
              type="button"
              className="session-drawer-save"
              onClick={saveCurrent}
            >
              Save
            </button>
          </Hint>
          <Hint label="Close (Esc)" position="bottom" maxWidth={120}>
            <button
              type="button"
              className="session-drawer-close"
              onClick={() => setOpen(false)}
              aria-label="Close"
            >
              ×
            </button>
          </Hint>
        </header>
        <div className="session-drawer-actions" role="toolbar" aria-label="Selection actions">
          <Hint label={allSelected ? 'Clear selection' : 'Select all'} position="bottom" maxWidth={160}>
            <label className="session-select-all">
              <input
                ref={selectAllRef}
                type="checkbox"
                className="session-row-check"
                checked={allSelected}
                onChange={toggleAll}
                disabled={allIds.length === 0}
                aria-label={allSelected ? 'Clear selection' : 'Select all sessions'}
              />
              <span className="t-caption">
                {allSelected ? 'None' : someSelected ? `${selected.size}/${allIds.length}` : 'All'}
              </span>
            </label>
          </Hint>
          <Hint label={canLoad ? 'Load selected session' : 'Select one row to load'} position="bottom" maxWidth={220}>
            <button
              type="button"
              className="session-action"
              onClick={loadSelected}
              disabled={!canLoad}
            >
              Load
            </button>
          </Hint>
          <Hint label={canBatch ? 'Delete selected sessions' : 'Select rows to delete'} position="bottom" maxWidth={220}>
            <button
              type="button"
              className={`session-action${confirmDelete ? ' is-confirming' : ''}`}
              onClick={deleteSelected}
              disabled={!canBatch}
            >
              {deleteLabel}
            </button>
          </Hint>
          <Hint label="Batch-compact selected sessions (Phase 8b — pending)" position="bottom" maxWidth={260}>
            <button
              type="button"
              className="session-action"
              onClick={compactSelected}
              disabled={!canBatch}
            >
              Compact
            </button>
          </Hint>
        </div>
        <div className="session-drawer-list">
          {sessions.length === 0 && (
            <div className="session-drawer-empty">No saved sessions yet.</div>
          )}
          {sessions.map((s) => (
            <SessionRow
              key={s.session_id}
              sessionId={s.session_id}
              startedAt={s.started_at}
              endedAt={s.ended_at}
              turnCount={s.turn_count}
              isCurrent={s.session_id === saveName}
              isSelected={selected.has(s.session_id)}
              onToggleSelect={() => toggleRow(s.session_id)}
              onMutated={fetchList}
            />
          ))}
          {/* Phase 1 — archive expandable. Sessions older than 7 days
              are moved here by the daily `sessions_archive` cron. The
              fetch is on-demand to keep the drawer cheap. */}
          <div className="session-archive-section">
            <button
              type="button"
              className="session-archive-toggle"
              onClick={() => {
                if (!archiveOpen && !archiveLoaded) {
                  void fetchArchive();
                }
                setArchiveOpen((v) => !v);
              }}
              aria-expanded={archiveOpen}
            >
              {archiveOpen ? '▾' : '▸'} Archive
              {archiveLoaded && archive.length > 0 ? ` · ${archive.length}` : ''}
            </button>
            {archiveOpen && (
              <div className="session-archive-list">
                {!archiveLoaded && (
                  <div className="session-drawer-empty">Loading…</div>
                )}
                {archiveLoaded && archive.length === 0 && (
                  <div className="session-drawer-empty">Archive is empty.</div>
                )}
                {archive.map((row) => (
                  <div key={row.session_id} className="session-archive-row">
                    <span className="session-archive-bucket t-caption">
                      {row.archived_in}
                    </span>
                    <span className="session-archive-name">
                      {row.session_id}
                    </span>
                    <span className="session-archive-meta t-caption">
                      {row.turn_count} turns · {relativeTime(row.started_at)}
                    </span>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      </aside>
    </>
  );
}

interface SessionRowProps {
  sessionId: string;
  startedAt: string;
  endedAt: string | null;
  turnCount: number;
  isCurrent: boolean;
  isSelected: boolean;
  onToggleSelect: () => void;
  onMutated: () => Promise<void> | void;
}

function SessionRow({
  sessionId,
  startedAt,
  endedAt,
  turnCount,
  isCurrent,
  isSelected,
  onToggleSelect,
  onMutated,
}: SessionRowProps) {
  const stale = isStale(startedAt);
  const [renaming, setRenaming] = useState(false);
  const [renameValue, setRenameValue] = useState(sessionId);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [previewState, setPreviewState] = useState<
    | { kind: 'idle' }
    | { kind: 'loading' }
    | { kind: 'ready'; data: SessionPreview }
    | { kind: 'error'; message: string }
  >({ kind: 'idle' });
  const [previewOpen, setPreviewOpen] = useState(false);
  const renameInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (renaming) renameInputRef.current?.select();
  }, [renaming]);

  const startRename = () => {
    setError(null);
    setRenameValue(sessionId);
    setRenaming(true);
  };

  const cancelRename = () => {
    setRenaming(false);
    setError(null);
  };

  const submitRename = async () => {
    const target = renameValue.trim();
    if (!target || target === sessionId) {
      cancelRename();
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await postSessionRename(sessionId, target);
      setRenaming(false);
      await onMutated();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'rename failed');
    } finally {
      setBusy(false);
    }
  };

  const handleDuplicate = async () => {
    const dest = `${sessionId}-copy`;
    setBusy(true);
    setError(null);
    try {
      await postSessionDuplicate(sessionId, dest);
      await onMutated();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'duplicate failed');
    } finally {
      setBusy(false);
    }
  };

  const togglePreview = async () => {
    if (previewOpen) {
      setPreviewOpen(false);
      return;
    }
    setPreviewOpen(true);
    if (previewState.kind === 'ready') return;
    setPreviewState({ kind: 'loading' });
    try {
      const data = await fetchSessionPreview(sessionId);
      setPreviewState({ kind: 'ready', data });
    } catch (e) {
      setPreviewState({
        kind: 'error',
        message: e instanceof Error ? e.message : 'preview failed',
      });
    }
  };

  return (
    <div
      className={
        'session-row' +
        (isCurrent ? ' is-current' : '') +
        (isSelected ? ' is-selected' : '') +
        (stale ? ' is-stale' : '')
      }
    >
      <input
        type="checkbox"
        className="session-row-check"
        checked={isSelected}
        onChange={onToggleSelect}
        aria-label={`Select ${sessionId}`}
      />
      <div className="session-row-body">
        {renaming ? (
          <div className="session-row-rename">
            <input
              ref={renameInputRef}
              type="text"
              className="session-row-rename-input"
              value={renameValue}
              onChange={(e) => setRenameValue(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  e.preventDefault();
                  void submitRename();
                } else if (e.key === 'Escape') {
                  e.preventDefault();
                  cancelRename();
                }
              }}
              disabled={busy}
              aria-label="New session name"
            />
            <button
              type="button"
              className="session-row-action"
              onClick={() => void submitRename()}
              disabled={busy}
            >
              save
            </button>
            <button
              type="button"
              className="session-row-action"
              onClick={cancelRename}
              disabled={busy}
            >
              cancel
            </button>
          </div>
        ) : (
          <div className="session-row-name">
            {sessionId}
            {stale && (
              <span
                className="session-row-stale"
                title="Older than yesterday — manual /load only"
              >
                stale
              </span>
            )}
          </div>
        )}
        <div className="session-row-meta">
          {turnCount} turns · {relativeTime(endedAt ?? startedAt)}
        </div>
        {error && <div className="session-row-error t-meta">{error}</div>}
        {previewOpen && (
          <div className="session-row-preview" role="region" aria-label="Session preview">
            {previewState.kind === 'loading' && (
              <div className="t-meta">loading…</div>
            )}
            {previewState.kind === 'error' && (
              <div className="session-row-error t-meta">{previewState.message}</div>
            )}
            {previewState.kind === 'ready' && (
              previewState.data.turns.length === 0 ? (
                <div className="t-meta">(no extractable turns)</div>
              ) : (
                <ol className="session-preview-list">
                  {previewState.data.turns.map((t, i) => (
                    <li key={i} className={`session-preview-turn role-${t.role}`}>
                      <span className="session-preview-role t-meta">{t.role}</span>
                      <span className="session-preview-text">{t.text}</span>
                    </li>
                  ))}
                </ol>
              )
            )}
          </div>
        )}
      </div>
      {!renaming && (
        <div className="session-row-actions" role="group" aria-label="Row actions">
          <Hint label={previewOpen ? 'Hide preview' : 'Show first turns'} position="bottom" maxWidth={180}>
            <button
              type="button"
              className="session-row-action"
              onClick={togglePreview}
              disabled={busy}
              aria-pressed={previewOpen}
            >
              {previewOpen ? 'hide' : 'preview'}
            </button>
          </Hint>
          <Hint label="Rename this session file" position="bottom" maxWidth={180}>
            <button
              type="button"
              className="session-row-action"
              onClick={startRename}
              disabled={busy}
            >
              rename
            </button>
          </Hint>
          <Hint label="Copy as <name>-copy" position="bottom" maxWidth={180}>
            <button
              type="button"
              className="session-row-action"
              onClick={() => void handleDuplicate()}
              disabled={busy}
            >
              duplicate
            </button>
          </Hint>
        </div>
      )}
    </div>
  );
}

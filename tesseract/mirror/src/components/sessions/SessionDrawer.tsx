import { useEffect, useMemo, useRef, useState } from 'react';
import { useSessionStore } from '../../stores/session';
import { useConversationStore } from '../../stores/conversation';
import { useUIStore } from '../../stores/ui';
import { sendCommand } from '../../lib/commands';
import { Hint } from '../ui/Hint';
import {
  fetchSessionPreview,
  postSessionArchive,
  postSessionRename,
  type SessionPreview,
} from '../../lib/api';
import { Checkbox } from '../common/Checkbox';
import { Input } from '../common/Input';
import { Button } from '../../components/common/Button';
import { CloseButton } from '../common/CloseButton';
import { Scrim } from '../common/Scrim';
import { Disclosure } from '../common/Disclosure';

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
  // The conversation the operator is IN, not the one they last saved. A title
  // is not unique and was never identity; the active chat id is both.
  const activeChatId = useConversationStore((s) => s.activeChatId);
  const fetchList = useSessionStore((s) => s.fetchList);
  const archive = useSessionStore((s) => s.archive);
  const archiveLoaded = useSessionStore((s) => s.archiveLoaded);
  const fetchArchive = useSessionStore((s) => s.fetchArchive);
  const [archiveOpen, setArchiveOpen] = useState(false);
  const asideRef = useRef<HTMLDivElement>(null);

  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [confirmDelete, setConfirmDelete] = useState(false);
  const selectAllRef = useRef<HTMLInputElement>(null);

  const allIds = useMemo(() => sessions.map((s) => s.chat_id), [sessions]);
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
    // ONE source. This used to also fire `/sessions`, whose reply writes the
    // same store field from a different function — capped at 20 and ordered by
    // last activity, where this one is unbounded and ordered by creation. Two
    // answers racing into one slot meant the row order and the row count
    // changed between openings depending on which landed last.
    fetchList();
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
      <Scrim onClick={() => setOpen(false)} ariaLabel="Close sessions" level="drawer" />
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
            <Button
              onClick={saveCurrent}
            >
              Save
            </Button>
          </Hint>
          <Hint label="Close (Esc)" position="bottom" maxWidth={120}>
            <CloseButton onClick={() => setOpen(false)} ariaLabel="Close" />
          </Hint>
        </header>
        <div className="session-drawer-actions" role="toolbar" aria-label="Selection actions">
          <Hint label={allSelected ? 'Clear selection' : 'Select all'} position="bottom" maxWidth={160}>
            {/* The count is the checkbox's LABEL, not a span beside a bare
                box in a wrapping label — two accessible names competing, with
                `aria-label` silently winning over the word on screen. The
                pill frame is all `session-select-all` owns. */}
            <div className="session-select-all">
              <Checkbox
                inputRef={selectAllRef}
                checked={allSelected}
                onChange={toggleAll}
                disabled={allIds.length === 0}
                label={
                  <span className="t-caption">
                    {allSelected ? 'None' : someSelected ? `${selected.size}/${allIds.length}` : 'All'}
                  </span>
                }
              />
            </div>
          </Hint>
          <Hint label={canLoad ? 'Load selected session' : 'Select one row to load'} position="bottom" maxWidth={220}>
            <Button
              onClick={loadSelected}
              disabled={!canLoad}
            >
              Load
            </Button>
          </Hint>
          <Hint label={canBatch ? 'Delete selected sessions' : 'Select rows to delete'} position="bottom" maxWidth={220}>
            <Button
              tone="danger"
              onClick={deleteSelected}
              disabled={!canBatch}
            >
              {deleteLabel}
            </Button>
          </Hint>
          <Hint label="Batch-compact selected sessions (Phase 8b — pending)" position="bottom" maxWidth={260}>
            <Button
              onClick={compactSelected}
              disabled={!canBatch}
            >
              Compact
            </Button>
          </Hint>
        </div>
        <div className="session-drawer-list">
          {sessions.length === 0 && (
            <div className="session-drawer-empty">No saved sessions yet.</div>
          )}
          {sessions.map((s) => (
            <SessionRow
              key={s.chat_id}
              chatId={s.chat_id}
              title={s.title}
              startedAt={s.started_at}
              endedAt={s.ended_at}
              turnCount={s.turn_count}
              isCurrent={s.chat_id === activeChatId}
              isSelected={selected.has(s.chat_id)}
              onToggleSelect={() => toggleRow(s.chat_id)}
              onMutated={fetchList}
            />
          ))}
          {/* Archive expandable. A conversation lands here when the operator
              archives it or /reset leaves it behind — archiving is a flag on
              the record, so nothing moves and nothing is lost. The fetch is
              on-demand to keep the drawer cheap. */}
          <div className="session-archive-section">
            <Disclosure
              variant="row"
              open={archiveOpen}
              onToggle={() => {
                if (!archiveOpen && !archiveLoaded) {
                  void fetchArchive();
                }
                setArchiveOpen((v) => !v);
              }}
            >
              {archiveOpen ? '▾' : '▸'} Archive
              {archiveLoaded && archive.length > 0 ? ` · ${archive.length}` : ''}
            </Disclosure>
            {archiveOpen && (
              <div className="session-archive-list">
                {!archiveLoaded && (
                  <div className="session-drawer-empty">Loading…</div>
                )}
                {archiveLoaded && archive.length === 0 && (
                  <div className="session-drawer-empty">Archive is empty.</div>
                )}
                {archive.map((row) => (
                  <div key={row.chat_id} className="session-archive-row">
                    <span className="session-archive-name">{row.title}</span>
                    <span className="session-archive-meta t-caption">
                      {row.turn_count} turns ·{' '}
                      {relativeTime(row.ended_at ?? row.started_at)}
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
  chatId: string;
  title: string;
  startedAt: string;
  endedAt: string | null;
  turnCount: number;
  isCurrent: boolean;
  isSelected: boolean;
  onToggleSelect: () => void;
  onMutated: () => Promise<void> | void;
}

function SessionRow({
  chatId,
  title,
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
  const [renameValue, setRenameValue] = useState(title);
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
    setRenameValue(title);
    setRenaming(true);
  };

  const cancelRename = () => {
    setRenaming(false);
    setError(null);
  };

  const submitRename = async () => {
    const target = renameValue.trim();
    if (!target || target === title) {
      cancelRename();
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await postSessionRename(chatId, target);
      setRenaming(false);
      await onMutated();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'rename failed');
    } finally {
      setBusy(false);
    }
  };

  // Archive, not duplicate. A copy would be a second record of one
  // conversation — the thing this whole surface exists to stop — and delete
  // refuses until a chat is archived, so without this control the archive and
  // the delete button were both out of reach from here.
  const handleArchive = async () => {
    setBusy(true);
    setError(null);
    try {
      await postSessionArchive(chatId);
      await onMutated();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'archive failed');
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
      const data = await fetchSessionPreview(chatId);
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
      <Checkbox
        checked={isSelected}
        onChange={onToggleSelect}
        ariaLabel={`Select ${title}`}
      />
      <div className="session-row-body">
        {renaming ? (
          <div className="session-row-rename">
            <Input
              inputRef={renameInputRef}
              className="session-row-rename-input"
              value={renameValue}
              onChange={setRenameValue}
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
              ariaLabel="New conversation title"
            />
            <Button onClick={() => void submitRename()} disabled={busy}>
              save
            </Button>
            <Button onClick={cancelRename} disabled={busy}>
              cancel
            </Button>
          </div>
        ) : (
          <div className="session-row-name">
            {title}
            {stale && (
              <Hint label="Older than yesterday — manual /load only">
                <span
                  className="session-row-stale"
                >
                  stale
                </span>
              </Hint>
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
            <Button
              onClick={togglePreview}
              disabled={busy}
              active={previewOpen}
            >
              {previewOpen ? 'hide' : 'preview'}
            </Button>
          </Hint>
          <Hint label="Rename this conversation" position="bottom" maxWidth={180}>
            <Button onClick={startRename} disabled={busy}>
              rename
            </Button>
          </Hint>
          <Hint label="Move to the archive — delete needs this first" position="bottom" maxWidth={240}>
            <Button onClick={() => void handleArchive()} disabled={busy}>
              archive
            </Button>
          </Hint>
        </div>
      )}
    </div>
  );
}

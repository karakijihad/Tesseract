import { useEffect, useRef, useState } from 'react';
import { useConversationStore } from '../../stores/conversation';
import { useWebSocketStore } from '../../stores/websocket';
import { BACKEND_BASE } from '../../lib/endpoints';

// D5 (locked) — max open chats; the backend auto-archives the oldest past this,
// so the manager disables "+ New chat" at the cap rather than silently dropping
// a chat.
const MAX_OPEN_CHATS = 10;

interface ArchivedRow {
  chat_id: string;
  title: string;
  message_count: number;
}

/**
 * multichat-redesign — dropdown chat manager. Replaces the horizontal
 * ChatTabStrip + separate ChatArchiveMenu with one trigger that opens a panel
 * managing every chat: switch/rename/archive open chats, restore/permanently-
 * delete archived ones. Open-chat mutations go over WS (`chat.*`) and the store
 * updates when the backend echoes the lifecycle envelope (`dispatch.ts`).
 * Permanent delete is REST-only (`DELETE /api/chats/{id}`) — a deleted chat is
 * already archived (not open), so there is no live session to notify.
 */
export function ChatManager() {
  const orderedIds = useConversationStore(s => s.orderedIds);
  const activeChatId = useConversationStore(s => s.activeChatId);
  const chats = useConversationStore(s => s.chats);
  const setChatTitle = useConversationStore(s => s.setChatTitle);
  const send = useWebSocketStore(s => s.sendMessage);

  const [open, setOpen] = useState(false);
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [draft, setDraft] = useState('');
  // Escape sets renamingId=null, unmounting the input and firing its onBlur
  // right after — without this flag that blur would commit the abandoned draft.
  const cancelledRef = useRef(false);

  const [archivedOpen, setArchivedOpen] = useState(false);
  const [archivedRows, setArchivedRows] = useState<ArchivedRow[] | null>(null);
  const [archivedError, setArchivedError] = useState(false);
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null);
  // Per-row delete failure — kept distinct from `archivedError` (list-level GET
  // failure) so a single failed DELETE surfaces on its own row instead of
  // blanking the whole loaded list.
  const [deleteErrorId, setDeleteErrorId] = useState<string | null>(null);

  const rootRef = useRef<HTMLDivElement>(null);

  const atCap = orderedIds.length >= MAX_OPEN_CHATS;
  const activeTitle = (activeChatId && chats.get(activeChatId)?.title) || 'Chats';
  // Aggregate signal for the closed panel: a tool ASK fired on a background chat
  // waits silently until that chat is active — the trigger dot tells the operator
  // to open the manager (per-row badges then say which one).
  const anyApproval = [...chats.values()].some(s => (s.pendingApprovals?.length ?? 0) > 0);

  const closePanel = () => {
    setOpen(false);
    setRenamingId(null);
    setConfirmDeleteId(null);
  };

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) closePanel();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !renamingId) closePanel();
    };
    document.addEventListener('mousedown', onDown);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDown);
      document.removeEventListener('keydown', onKey);
    };
  }, [open, renamingId]);

  const beginRename = (id: string, current: string) => {
    setRenamingId(id);
    setDraft(current);
  };

  const cancelRename = () => {
    cancelledRef.current = true;
    setRenamingId(null);
  };

  const commitRename = (id: string) => {
    if (cancelledRef.current) {
      cancelledRef.current = false;
      return;
    }
    const title = draft.trim();
    setRenamingId(null);
    const current = chats.get(id)?.title ?? '';
    if (!title || title === current) return;
    setChatTitle(id, title); // optimistic — backend echoes chat_renamed
    send('chat.rename', { chat_id: id, title });
  };

  const switchTo = (id: string) => {
    if (id !== activeChatId) send('chat.switch', { chat_id: id });
  };

  const loadArchived = async () => {
    setArchivedRows(null);
    setArchivedError(false);
    try {
      const resp = await fetch(`${BACKEND_BASE}/api/chats?include_archived=true`);
      if (!resp.ok) throw new Error(String(resp.status));
      const data = await resp.json();
      setArchivedRows((data.chats ?? []).filter((c: ArchivedRow & { archived?: boolean }) => c.archived));
    } catch {
      setArchivedError(true);
    }
  };

  const toggleArchived = () => {
    const next = !archivedOpen;
    setArchivedOpen(next);
    setConfirmDeleteId(null);
    setDeleteErrorId(null);
    if (next) void loadArchived();
  };

  const restore = (id: string) => {
    send('chat.restore', { chat_id: id });
    setArchivedRows(rows => rows?.filter(r => r.chat_id !== id) ?? rows);
  };

  const deletePermanently = async (id: string) => {
    setConfirmDeleteId(null);
    setDeleteErrorId(null);
    try {
      const resp = await fetch(`${BACKEND_BASE}/api/chats/${id}`, { method: 'DELETE' });
      if (!resp.ok) throw new Error(String(resp.status));
      setArchivedRows(rows => rows?.filter(r => r.chat_id !== id) ?? rows);
    } catch {
      setDeleteErrorId(id); // row-level — leaves the rest of the list intact
    }
  };

  return (
    <div className="chat-mgr" ref={rootRef}>
      <button
        type="button"
        className="chat-mgr-trigger"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => (open ? closePanel() : setOpen(true))}
      >
        <span className="chat-mgr-caret" aria-hidden="true">▾</span>
        <span className="chat-mgr-active-title" title={activeTitle}>{activeTitle}</span>
        {anyApproval && (
          <span
            className="chat-tab-approval"
            title="A background chat is awaiting approval"
            aria-label="A background chat is awaiting approval"
          />
        )}
        <span className="chat-mgr-count t-meta">{orderedIds.length}/{MAX_OPEN_CHATS}</span>
      </button>

      {open && (
        <div className="chat-mgr-panel" role="menu">
          <button
            type="button"
            className="chat-mgr-new"
            disabled={atCap}
            title={atCap ? `Max ${MAX_OPEN_CHATS} open chats` : 'New chat'}
            onClick={() => send('chat.create', {})}
          >
            + New chat
          </button>

          <div className="chat-mgr-list">
            {orderedIds.length === 0 && <div className="chat-mgr-empty t-meta">No open chats</div>}
            {orderedIds.map(id => {
              const slice = chats.get(id);
              const title = slice?.title || 'Chat';
              const isActive = id === activeChatId;
              return (
                <div key={id} className={`chat-mgr-row${isActive ? ' is-active' : ''}`} role="menuitem">
                  {renamingId === id ? (
                    <input
                      className="chat-mgr-rename"
                      autoFocus
                      value={draft}
                      onChange={e => setDraft(e.target.value)}
                      onBlur={() => commitRename(id)}
                      onKeyDown={e => {
                        if (e.key === 'Enter') commitRename(id);
                        else if (e.key === 'Escape') cancelRename();
                      }}
                    />
                  ) : (
                    <>
                      <button
                        type="button"
                        className="chat-mgr-row-label"
                        title={title}
                        onClick={() => switchTo(id)}
                        onDoubleClick={() => beginRename(id, title)}
                      >
                        {isActive && <span className="chat-mgr-active-dot" aria-hidden="true" />}
                        <span className="chat-mgr-row-title">{title}</span>
                        {slice?.isStreaming && <span className="chat-tab-pulse" aria-hidden="true" />}
                        {(slice?.pendingApprovals?.length ?? 0) > 0 && (
                          <span className="chat-tab-approval" title="Awaiting approval" aria-label="Awaiting approval" />
                        )}
                      </button>
                      <div className="chat-mgr-row-actions">
                        <button
                          type="button"
                          className="chat-mgr-icon t-meta"
                          aria-label="Rename chat"
                          title="Rename"
                          onClick={() => beginRename(id, title)}
                        >
                          ✎
                        </button>
                        <button
                          type="button"
                          className="chat-mgr-icon t-meta"
                          aria-label="Archive chat"
                          title="Archive (recoverable)"
                          onClick={() => send('chat.archive', { chat_id: id })}
                        >
                          ✕
                        </button>
                      </div>
                    </>
                  )}
                </div>
              );
            })}
          </div>

          <div className="chat-mgr-archived">
            <button
              type="button"
              className="chat-mgr-archived-header t-meta"
              aria-expanded={archivedOpen}
              onClick={toggleArchived}
            >
              <span>Archived{archivedRows ? ` (${archivedRows.length})` : ''}</span>
              <span aria-hidden="true">{archivedOpen ? '▾' : '▸'}</span>
            </button>
            {archivedOpen && (
              <div className="chat-mgr-archived-list">
                {archivedError && <div className="chat-mgr-empty t-meta">Couldn’t load archived chats</div>}
                {!archivedError && archivedRows === null && <div className="chat-mgr-empty t-meta">Loading…</div>}
                {!archivedError && archivedRows?.length === 0 && (
                  <div className="chat-mgr-empty t-meta">No archived chats</div>
                )}
                {!archivedError && archivedRows?.map(r => (
                  <div key={r.chat_id} className="chat-mgr-archived-row">
                    <span className="chat-mgr-archived-title" title={r.title}>{r.title || 'Chat'}</span>
                    {confirmDeleteId === r.chat_id ? (
                      <span className="chat-mgr-confirm">
                        <span className="chat-mgr-confirm-label t-meta">Delete forever?</span>
                        <button
                          type="button"
                          className="chat-mgr-confirm-yes"
                          onClick={() => deletePermanently(r.chat_id)}
                        >
                          Delete
                        </button>
                        <button type="button" className="chat-mgr-icon t-meta" onClick={() => setConfirmDeleteId(null)}>
                          Cancel
                        </button>
                      </span>
                    ) : (
                      <span className="chat-mgr-archived-actions">
                        {deleteErrorId === r.chat_id && (
                          <span className="chat-mgr-delete-error t-meta">Couldn’t delete</span>
                        )}
                        <button type="button" className="chat-mgr-restore" onClick={() => restore(r.chat_id)}>
                          Restore
                        </button>
                        <button
                          type="button"
                          className="chat-mgr-icon t-meta"
                          aria-label="Delete permanently"
                          title="Delete permanently"
                          onClick={() => { setDeleteErrorId(null); setConfirmDeleteId(r.chat_id); }}
                        >
                          🗑
                        </button>
                      </span>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

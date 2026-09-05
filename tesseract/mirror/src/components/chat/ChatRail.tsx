import { useEffect, useMemo, useRef, useState } from 'react';

import './ChatRail.css';

import { useConversationStore } from '../../stores/conversation';
import { useSessionStore } from '../../stores/session';
import { useUIStore } from '../../stores/ui';
import { useRowSelection } from '../../hooks/useRowSelection';
import { useWebSocketStore } from '../../stores/websocket';
import { BACKEND_BASE } from '../../lib/endpoints';
import type { SessionMeta } from '../../lib/types';
import { Row, RowActions } from '../common/Row';
import { BulkBar } from '../common/BulkBar';
import { Checkbox } from '../common/Checkbox';
import { Tabs } from '../common/Tabs';
import { Button } from '../common/Button';
import { IconButton } from '../common/IconButton';
import { Hint } from '../ui/Hint';

type RailTab = 'history' | 'archive';

// Rows arrive newest-created first, so cutting where the label changes is the
// whole grouping.
//
// A heading that names ONE day carries that day's date, because the rows
// under it no longer do: the label is what the conversation was about, and
// dropping the stamp from it is what left the date with nowhere to live. A
// heading spanning several days stays coarse, since it cannot name one.
function dayLabel(ms: number): string {
  const then = new Date(ms);
  if (Number.isNaN(then.getTime())) return 'Undated';
  const midnight = new Date();
  midnight.setHours(0, 0, 0, 0);
  const days = Math.floor((midnight.getTime() - then.setHours(0, 0, 0, 0)) / 86400000);
  const short = new Date(ms).toLocaleDateString(undefined, { day: 'numeric', month: 'short' });
  if (days <= 0) return `Today · ${short}`;
  if (days === 1) return `Yesterday · ${short}`;
  if (days < 7) return 'Earlier this week';
  if (days < 30) return 'Earlier this month';
  return new Date(ms).toLocaleDateString(undefined, { month: 'long', year: 'numeric' });
}

function timeLabel(ms: number): string {
  const t = new Date(ms);
  if (Number.isNaN(t.getTime())) return '';
  return t.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
}

/** When the conversation was last used, in epoch ms.
 *
 * A row is filed under the day it was last used, not the day it was born, so
 * picking an old chat up again brings it back to today.
 *
 * The live slice wins when it is ahead of what the row says, because the
 * record only reaches disk on the autosave timer: without it a chat continued
 * right now would sit under its old heading for up to a minute. A chat this
 * connection does not hold has no slice, and the row's own stamp is all there
 * is.
 *
 * NaN when neither source says anything, because that is what `dayLabel` and
 * `timeLabel` already answer "Undated" and "" to. Zero would have been the
 * first day of 1970, which is a heading rather than an admission.
 */
function activityMs(row: SessionMeta, slice?: { messages: { timestamp: number }[] }): number {
  const known: number[] = [];
  const last = slice?.messages.length ? slice.messages[slice.messages.length - 1] : null;
  if (last && !Number.isNaN(last.timestamp)) known.push(last.timestamp);
  const stamped = Date.parse(row.last_active_at || row.created_at);
  if (!Number.isNaN(stamped)) known.push(stamped);
  return known.length ? Math.max(...known) : NaN;
}

/** The same answer, made safe to subtract.
 *
 * A record with no parseable date anywhere gives `activityMs` NaN, and a
 * comparator that returns NaN leaves that row's position up to the engine.
 * It sorts last instead, which is where an undated row belongs, and the
 * heading still reads "Undated" because that path keeps the NaN.
 */
function sortKey(row: SessionMeta, chats: Map<string, { messages: { timestamp: number }[] }>): number {
  const ms = activityMs(row, chats.get(row.chat_id));
  return Number.isNaN(ms) ? -Infinity : ms;
}

/** The exact stamp: the row's hover, and the open conversation's header. */
export function fullStamp(iso: string): string {
  const t = new Date(iso);
  if (Number.isNaN(t.getTime())) return '';
  return t.toLocaleString(undefined, {
    day: 'numeric', month: 'long', year: 'numeric', hour: '2-digit', minute: '2-digit',
  });
}

/** What the row says the conversation was about.
 *
 * The cascade is decided on the server: `snippet` arrives only when the title
 * is still the stamp the chat was born with, so an operator rename wins by
 * this being empty and a chat nobody typed into falls back to the stamp.
 */
export function rowLabel(row: SessionMeta): string {
  return row.snippet?.trim() || row.title;
}

/** The same snippet the server computes, for a chat it has not written yet.
 *
 * `role` already separates what the operator typed from what the runtime
 * injected, so no sentence nobody typed can become a conversation's name.
 * Bounded at the length the server bounds its own at.
 */
function firstOperatorText(messages: { role: string; content: string }[]): string {
  for (const msg of messages) {
    if (msg.role !== 'user') continue;
    const text = msg.content.trim().replace(/\s+/g, ' ');
    if (text) return text.slice(0, 120);
  }
  return '';
}

const ArchiveIcon = () => (
  <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
    <path d="M3 8h18v12H3zM1 4h22v4H1zM10 12h4" />
  </svg>
);

const RestoreIcon = () => (
  <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true">
    <path d="M3 12a9 9 0 1 0 3-6.7M3 4v5h5" />
  </svg>
);

const SelectIcon = () => (
  <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true">
    <path d="M4 5h16v16H4zM8 12l3 3 5-6" />
  </svg>
);

const DeleteIcon = () => (
  <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
    <path d="M4 7h16M9 7V5h6v2M6 7l1 13h10l1-13" />
  </svg>
);

/** Resolve once the chat's membership of the open set matches `want`, or false
 *  if it never does. A socket verb has no reply to await, so this is how a
 *  caller learns the envelope landed rather than the verb being refused. */
function openSetReaches(
  chatId: string,
  want: boolean,
  signal: AbortSignal,
  ms = 4000,
): Promise<boolean> {
  const has = () => useConversationStore.getState().chats.has(chatId);
  if (has() === want) return Promise.resolve(true);
  return new Promise(resolve => {
    const stop = (ok: boolean) => {
      clearTimeout(timer);
      unsub();
      signal.removeEventListener('abort', onAbort);
      resolve(ok);
    };
    const onAbort = () => stop(false);
    const timer = setTimeout(() => stop(false), ms);
    const unsub = useConversationStore.subscribe(state => {
      if (state.chats.has(chatId) === want) stop(true);
    });
    signal.addEventListener('abort', onAbort);
  });
}

const leftTheOpenSet = (id: string, signal: AbortSignal) => openSetReaches(id, false, signal);
const joinedTheOpenSet = (id: string, signal: AbortSignal) => openSetReaches(id, true, signal);

const GearIcon = () => (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
    <circle cx="12" cy="12" r="3.1" />
    <path d="M19.4 15a1.6 1.6 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.6 1.6 0 0 0-1.8-.3 1.6 1.6 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1A1.6 1.6 0 0 0 9 19.4a1.6 1.6 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.6 1.6 0 0 0 .3-1.8 1.6 1.6 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1A1.6 1.6 0 0 0 4.6 9a1.6 1.6 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.6 1.6 0 0 0 1.8.3H9a1.6 1.6 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.6 1.6 0 0 0 1 1.5 1.6 1.6 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.6 1.6 0 0 0-.3 1.8V9a1.6 1.6 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.6 1.6 0 0 0-1.5 1z" />
  </svg>
);

interface ChatSettings {
  chats_at_once: number;
  archive_after_days: number;
  chats_at_once_range: [number, number];
  archive_after_days_range: [number, number];
}

function Stepper({ label, value, range, unit, busy, onChange }: {
  label: string;
  value: number;
  range: [number, number];
  unit?: string;
  busy?: boolean;
  onChange: (next: number) => void;
}) {
  const [lo, hi] = range;
  return (
    <>
      <span className="chat-rail__pref-label t-caption">{label}</span>
      <span className="chat-rail__stepper">
        <IconButton
          ariaLabel={`Fewer: ${label}`}
          disabled={busy || value <= lo}
          onClick={() => onChange(value - 1)}
        >
          −
        </IconButton>
        <span className="chat-rail__pref-value t-caption">{value}</span>
        <IconButton
          ariaLabel={`More: ${label}`}
          disabled={busy || value >= hi}
          onClick={() => onChange(value + 1)}
        >
          +
        </IconButton>
        {unit && <span className="chat-rail__pref-unit t-caption">{unit}</span>}
      </span>
    </>
  );
}

const PlusIcon = () => (
  <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true">
    <path d="M12 5v14M5 12h14" />
  </svg>
);

/** New chat, history and archive in one place.
 *
 * Lists the chats on disk rather than `orderedIds`, which holds only what this
 * connection has open. Live state is overlaid for the rows that are also open.
 */
export function ChatRail() {
  const open = useUIStore(s => s.chatRailOpen);

  const sessions = useSessionStore(s => s.sessions);
  const archive = useSessionStore(s => s.archive);
  const fetchList = useSessionStore(s => s.fetchList);
  const fetchArchive = useSessionStore(s => s.fetchArchive);

  const chats = useConversationStore(s => s.chats);
  const activeChatId = useConversationStore(s => s.activeChatId);
  const send = useWebSocketStore(s => s.sendMessage);

  const [tab, setTab] = useState<RailTab>('history');
  const [gearOpen, setGearOpen] = useState(false);
  const [settings, setSettings] = useState<ChatSettings | null>(null);
  const [settingsError, setSettingsError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  // The last reply the backend confirmed, which is what a failure rolls back to.
  const confirmedRef = useRef<ChatSettings | null>(null);
  // Aborted on unmount, so a wait outlives neither the rail nor its timeout.
  // The controller is MADE in the effect, not in the ref initialiser: under
  // StrictMode the first mount's cleanup aborts it, and a ref survives that
  // remount, so an initialiser-built controller would be dead for the rest of
  // the session and every wait would stall its full timeout and report a
  // refusal that never happened.
  const alive = useRef<AbortController>(new AbortController());
  useEffect(() => {
    alive.current = new AbortController();
    const controller = alive.current;
    return () => controller.abort();
  }, []);
  const [confirmId, setConfirmId] = useState<string | null>(null);
  const [rowError, setRowError] = useState<{ id: string; text: string } | null>(null);

  useEffect(() => {
    if (!open) return;
    if (tab === 'history') void fetchList();
    else void fetchArchive();
  }, [open, tab, fetchList, fetchArchive]);

  // A pending "delete forever?" must not survive into a list it was not
  // aimed at.
  useEffect(() => {
    setConfirmId(null);
    setRowError(null);
  }, [tab]);

  useEffect(() => {
    if (!gearOpen) return;
    void (async () => {
      try {
        const resp = await fetch(`${BACKEND_BASE}/api/chats/settings`);
        if (!resp.ok) throw new Error(String(resp.status));
        const loaded = await resp.json();
        confirmedRef.current = loaded;
        setSettings(loaded);
        setSettingsError(null);
      } catch {
        setSettingsError('Could not read these settings.');
      }
    })();
  }, [gearOpen]);

  // Optimistic, then corrected by what the backend read back off disk. The
  // rollback comes from the last CONFIRMED reply rather than from the render
  // closure, which on a second click still held the first click's optimistic
  // value and would restore a number the file never had.
  const saveSetting = async (key: 'chats_at_once' | 'archive_after_days', value: number) => {
    if (saving) return;
    setSaving(true);
    setSettings(s => (s ? { ...s, [key]: value } : s));
    try {
      const resp = await fetch(`${BACKEND_BASE}/api/chats/settings`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ [key]: value }),
      });
      if (!resp.ok) throw new Error(String(resp.status));
      const applied = await resp.json();
      confirmedRef.current = applied;
      setSettings(applied);
      setSettingsError(null);
    } catch {
      setSettings(confirmedRef.current);
      setSettingsError('Could not save that. The file is unchanged.');
    } finally {
      setSaving(false);
    }
  };

  const runningCount = useMemo(
    () => [...chats.values()].filter(s => s.isStreaming).length,
    [chats],
  );

  // Chats this connection holds that the library has not written yet.
  //
  // A chat earns a file once it holds something: `chat_store._is_disposable`
  // refuses a blank one, and the autosave that writes it afterwards runs on
  // an interval. Between creating a chat and its first turn reaching disk the
  // rail counted it as running and did not list it, so the count and the list
  // disagreed about the conversation the operator was typing in.
  //
  // These rows come from the same store the count does, which is what makes
  // the two agree. A disk row for the same id always wins: it carries the
  // snippet, the turn tally and the model, and an open slice carries none of
  // the three. An archived chat leaves the open set, so nothing here can
  // resurrect one into History.
  //
  // A chat with nothing in it is NOT listed. A connection seeds a blank chat,
  // and so does pressing New chat, and neither is a conversation yet: listing
  // them put a fresh timestamp under Today for something the operator had not
  // said a word in. One message is the bar, not one operator message — a turn
  // the runtime started is still something the conversation holds, and it is
  // also the only way a chat can be streaming, which is what keeps this list
  // and the running count agreeing.
  //
  // The snippet is read from the live messages by the same rule the server
  // uses, because the record reaches disk on the autosave timer: without it
  // the row a first turn just earned went on showing the birth stamp for up
  // to a minute.
  const unwritten = useMemo(() => {
    const onDisk = new Set(sessions.map(s => s.chat_id));
    return [...chats.entries()]
      .filter(([id, slice]) => !onDisk.has(id) && slice.createdAt && slice.messages.length > 0)
      .map(([id, slice]): SessionMeta => ({
        chat_id: id,
        title: slice.title,
        snippet: firstOperatorText(slice.messages),
        created_at: slice.createdAt,
        started_at: slice.createdAt,
        ended_at: null,
        // No `last_active_at`: the slice this row was built from is the same
        // one `activityMs` reads, so stamping it here would be the identical
        // number derived a second way.
        turn_count: 0,
        model: '',
      }));
  }, [chats, sessions]);

  // Most recently used first. Ties return 0 rather than a side: two chats can
  // carry the same stamp, and a comparator that puts each of them first is
  // free to reorder rows that were already in the right order.
  const rows = tab === 'history'
    ? [...sessions.filter(s => !s.archived), ...unwritten]
        .sort((a, b) => sortKey(b, chats) - sortKey(a, chats))
    : archive;

  const refresh = () => {
    void fetchList();
    void fetchArchive();
  };

  // Selecting several at once is a MODE, not a permanent column. The rail is
  // 264px wide and its everyday job is one click to open a conversation;
  // taxing every row with a checkbox for an occasional bulk action is the
  // wrong trade. Leaving the mode clears the selection, so a decision cannot
  // stay armed behind a closed drawer.
  const [selecting, setSelecting] = useState(false);
  const [bulkBusy, setBulkBusy] = useState(false);
  const [bulkNote, setBulkNote] = useState<string | null>(null);
  const sel = useRowSelection(rows.map(r => r.chat_id));
  const { clear } = sel;

  useEffect(() => {
    if (!selecting) {
      clear();
      setBulkNote(null);
    }
  }, [selecting, clear]);

  // Switching tabs changes what the verbs even are — Archive on one side,
  // Restore on the other — so a selection made under the old pair must not
  // survive into the new one.
  useEffect(() => {
    clear();
    setBulkNote(null);
  }, [tab, clear]);

  // SEQUENTIAL, and that is the difference from the inbox's bulk run.
  //
  // The inbox fires every request together because the backend takes a
  // per-event lock, so its rows are independent. These are not: archiving
  // walks the OPEN SET, and the server refuses to archive the last open chat
  // (`last_open_chat`). Fired together, which row hits that guard is a race,
  // and the operator gets a different survivor each time they try. One at a
  // time makes the refusal land on a predictable row, and a rail bulk is a
  // handful of chats rather than fifty.
  //
  // `quiet` on each call because the per-row error line keys on ONE id and
  // the list refetch is worth doing once, not once per chat.
  const runBulk = async (verb: 'archive' | 'restore' | 'delete') => {
    const targets = rows.map(r => r.chat_id).filter(id => sel.selected.has(id));
    if (targets.length === 0 || bulkBusy) return;
    setBulkBusy(true);
    setRowError(null);
    setBulkNote(null);
    const failed = new Set<string>();
    try {
      for (const id of targets) {
        const ok = verb === 'archive'
          ? await archiveChat(id, true)
          : verb === 'restore'
            ? await restoreChat(id, true)
            : await deleteChat(id, tab === 'history', true);
        if (!ok) failed.add(id);
      }
    } finally {
      sel.settle(targets, failed);
      refresh();
      setBulkBusy(false);
      if (failed.size > 0) {
        // Named in the operator's terms: what did not happen, and how many.
        // The ones that failed are still ticked, so the retry is one click.
        setBulkNote(
          `${failed.size} of ${targets.length} could not be ${
            verb === 'delete' ? 'deleted' : verb === 'archive' ? 'archived' : 'restored'
          }. They are still selected.`,
        );
      }
    }
  };

  // Only history rows open on a click. An archived chat is reached with
  // Restore, which un-archives deliberately; a plain click must not.
  const switchTo = (id: string) => {
    if (tab === 'archive' || id === activeChatId) return;
    send('chat.switch', { chat_id: id });
  };

  // Which transport depends on whether THIS connection has the chat open.
  //
  // Open here: the socket verb, because the envelope is what moves
  // `activeChatId`, the audio and the checklist. REST changes the server and
  // tells nobody, so the store kept pointing at a chat that was no longer open
  // and the next turn ran against a different one.
  //
  // Not open here: REST. Most rows are like this — the rail lists the chats on
  // disk, and the session holds only the few it has opened. There is no store
  // state to keep in step, and the socket verb has no chat to act on.
  const archiveChat = async (id: string, quiet = false): Promise<boolean> => {
    setRowError(null);
    if (chats.has(id)) {
      send('chat.archive', { chat_id: id });
      if (await leftTheOpenSet(id, alive.current.signal)) {
        if (!quiet) refresh();
        return true;
      }
      // No row message: `chat_archive_failed` toasted the moment it arrived,
      // and this wait can only speak after its timeout, so a second line would
      // be the same news four seconds late. It is also how an unmount reports
      // itself, and an unmounted rail has nobody to tell.
      return false;
    }
    const resp = await fetch(`${BACKEND_BASE}/api/chats/${id}/archive`, { method: 'POST' });
    if (!resp.ok) {
      const why = await resp.json().catch(() => ({}));
      if (!quiet) {
        setRowError({ id, text: why.error === 'chat_busy'
          ? 'This chat is taking a turn. Try again when it finishes.'
          : 'Could not archive this chat.' });
      }
      return false;
    }
    if (!quiet) refresh();
    return true;
  };

  // Restore always pulls the chat INTO the open set, so it always needs the
  // envelope that carries its history.
  const restoreChat = async (id: string, quiet = false): Promise<boolean> => {
    setRowError(null);
    send('chat.restore', { chat_id: id });
    // The envelope moves the conversation store; these two lists come from the
    // session store, which nothing tells. A refusal is NOT reported here:
    // `chat_restore_failed` already toasts the moment it arrives, and this
    // wait can only speak after its timeout, so a second message would say the
    // same thing four seconds late.
    if (!(await joinedTheOpenSet(id, alive.current.signal))) return false;
    if (!quiet) refresh();
    return true;
  };

  // The route refuses a hard delete on a chat that was never archived; it
  // wants an explicit second step. The confirm is that step, so archive first
  // rather than relaxing the guard.
  const deleteChat = async (id: string, fromHistory: boolean, quiet = false): Promise<boolean> => {
    setConfirmId(null);
    if (!quiet) setRowError(null);
    // archiveChat has already set the row's message, and it names the reason.
    if (fromHistory && !(await archiveChat(id, quiet))) return false;
    const resp = await fetch(`${BACKEND_BASE}/api/chats/${id}`, { method: 'DELETE' });
    if (!resp.ok) {
      if (!quiet) {
        setRowError({ id, text: fromHistory
          ? 'Could not delete this chat. It has been archived instead.'
          : 'Could not delete this chat.' });
        refresh();
      }
      return false;
    }
    if (!quiet) refresh();
    return true;
  };

  if (!open) return null;

  let lastDay = '';

  return (
    <nav className="chat-rail" aria-label="Conversations">
      <div className="chat-rail__new">
        <Button onClick={() => send('chat.create', {})}>
          <PlusIcon />
          New chat
        </Button>
      </div>

      <div className="chat-rail__running t-caption">
        <span
          className={`chat-rail__pip${runningCount > 0 ? ' chat-rail__pip--live' : ''}`}
          aria-hidden="true"
        />
        {runningCount} {runningCount === 1 ? 'chat running' : 'chats running'}
        <span className="chat-rail__gear">
          <Hint label={selecting ? 'Stop selecting' : 'Select several'}>
            <IconButton
              ariaLabel={selecting ? 'Stop selecting' : 'Select several conversations'}
              active={selecting}
              onClick={() => setSelecting(v => !v)}
            >
              <SelectIcon />
            </IconButton>
          </Hint>
          <Hint label="Conversation settings">
            <IconButton
              ariaLabel="Conversation settings"
              active={gearOpen}
              onClick={() => setGearOpen(o => !o)}
            >
              <GearIcon />
            </IconButton>
          </Hint>
        </span>
      </div>

      {gearOpen && (
        <div className="chat-rail__prefs">
          {settingsError && <div className="chat-rail__prefs-error t-caption">{settingsError}</div>}
          {settings && (
            <div className="chat-rail__prefs-grid">
              <Stepper
                label="Chats at once"
                value={settings.chats_at_once}
                range={settings.chats_at_once_range}
                busy={saving}
                onChange={v => void saveSetting('chats_at_once', v)}
              />
              <Stepper
                label="Archive after"
                value={settings.archive_after_days}
                range={settings.archive_after_days_range}
                busy={saving}
                unit="days"
                onChange={v => void saveSetting('archive_after_days', v)}
              />
            </div>
          )}
        </div>
      )}

      <Tabs
        items={[
          { key: 'history', label: 'History' },
          { key: 'archive', label: 'Archive' },
        ] as const}
        active={tab}
        onSelect={setTab}
        label="Conversations"
        fill
      />

      {selecting && (
        <div className="chat-rail__select-all">
          <Checkbox
            checked={sel.allVisibleSelected}
            onChange={sel.toggleAll}
            inputRef={sel.selectAllRef}
            disabled={bulkBusy || rows.length === 0}
            label={sel.selected.size > 0 ? `${sel.selected.size} selected` : 'Select all'}
          />
        </div>
      )}

      {selecting && (
        <BulkBar count={sel.selected.size} onClear={sel.clear} busy={bulkBusy}>
          {tab === 'history' ? (
            <Button
              onClick={() => void runBulk('archive')}
              disabled={bulkBusy}
            >
              Archive {sel.selected.size}
            </Button>
          ) : (
            <Button
              onClick={() => void runBulk('restore')}
              disabled={bulkBusy}
            >
              Restore {sel.selected.size}
            </Button>
          )}
          <Button
            tone="danger"
            disabled={bulkBusy}
            onClick={() => {
              const n = sel.selected.size;
              if (n === 0) return;
              // Delete is refused on a chat that was never archived, so a
              // bulk delete from History archives each one first, exactly as
              // the single-row Delete already does. The confirm is the
              // operator's second step, so it says what it is about to do.
              const also = tab === 'history' ? ' They will be archived first.' : '';
              if (!window.confirm(
                `Delete ${n} conversation${n === 1 ? '' : 's'} forever?${also}`,
              )) return;
              void runBulk('delete');
            }}
          >
            Delete {sel.selected.size}
          </Button>
        </BulkBar>
      )}

      {bulkNote && <div className="chat-rail__row-error t-caption">{bulkNote}</div>}

      <div className="chat-rail__list">
        {rows.length === 0 && (
          <div className="chat-rail__empty t-caption">
            {tab === 'history' ? 'No conversations yet.' : 'Nothing archived.'}
          </div>
        )}
        {rows.map((row: SessionMeta) => {
          const slice = chats.get(row.chat_id);
          const isActive = row.chat_id === activeChatId;
          const name = rowLabel(row);
          const active = activityMs(row, slice);
          const day = dayLabel(active);
          const heading = tab === 'history' && day !== lastDay ? day : null;
          if (heading) lastDay = day;
          return (
            <div key={row.chat_id}>
              {heading && <div className="chat-rail__day t-caption">{heading}</div>}
              <div className="chat-rail__line">
              {selecting && (
                /* Outside the Row, not inside it: a checkbox nested in
                   something that is itself activatable cannot be ticked
                   without also opening the conversation. */
                <span className="chat-rail__select">
                  <Checkbox
                    checked={sel.isSelected(row.chat_id)}
                    onChange={next => sel.toggle(row.chat_id, next)}
                    disabled={bulkBusy}
                    ariaLabel={`select ${name}`}
                  />
                </span>
              )}
              <Row
                className="chat-rail__row"
                onClick={() => switchTo(row.chat_id)}
                ariaLabel={name}
              >
                <span
                  className={`chat-rail__dot${slice?.isStreaming ? ' chat-rail__dot--live' : ''}`}
                  aria-hidden="true"
                />
                {/* The hint says the label in full, because the label is what
                    truncates. The date is not repeated here: the day heading
                    above carries it and the open conversation shows the exact
                    stamp. */}
                <Hint label={name}>
                  <span className={`chat-rail__title t-caption${isActive ? ' is-current' : ''}`}>
                    {name}
                  </span>
                </Hint>
                {(slice?.pendingApprovals?.length ?? 0) > 0 && (
                  <Hint label="Awaiting approval">
                    <span className="chat-rail__flag" aria-label="Awaiting approval" />
                  </Hint>
                )}
                <span className="chat-rail__time t-caption">{timeLabel(active)}</span>
                <RowActions className="chat-rail__acts">
                  {tab === 'history' ? (
                    <Hint label="Archive">
                      <IconButton
                        ariaLabel={`Archive ${name}`}
                        onClick={() => void archiveChat(row.chat_id)}
                      >
                        <ArchiveIcon />
                      </IconButton>
                    </Hint>
                  ) : (
                    <Hint label="Restore to history">
                      <IconButton
                        ariaLabel={`Restore ${name}`}
                        onClick={() => void restoreChat(row.chat_id)}
                      >
                        <RestoreIcon />
                      </IconButton>
                    </Hint>
                  )}
                  <Hint label="Delete permanently">
                    <IconButton
                      ariaLabel={`Delete ${name}`}
                      onClick={() => { setRowError(null); setConfirmId(row.chat_id); }}
                    >
                      <DeleteIcon />
                    </IconButton>
                  </Hint>
                </RowActions>
                {isActive && <span className="chat-rail__here" aria-hidden="true" />}
              </Row>
              </div>
              {confirmId === row.chat_id && (
                <div className="chat-rail__confirm">
                  <span className="t-caption">Delete forever?</span>
                  <Button tone="danger" onClick={() => void deleteChat(row.chat_id, tab === 'history')}>
                    Delete
                  </Button>
                  <Button onClick={() => setConfirmId(null)}>Cancel</Button>
                </div>
              )}
              {rowError?.id === row.chat_id && (
                <div className="chat-rail__row-error t-caption">{rowError.text}</div>
              )}
            </div>
          );
        })}
      </div>
    </nav>
  );
}

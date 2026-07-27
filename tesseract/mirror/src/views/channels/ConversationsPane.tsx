/* MO-9-12 Conversations pane — left rail of allowed users + right
 * chronological message list. Read-only: reply happens through the chat
 * tab; the phase doc §3 records reply-from-here as out-of-scope until
 * the multi-user milestone revisits it.
 *
 * Reads from the per-channel conversation store via
 * `GET /api/channels/{name}/users/{user_id}/conversation`. The store
 * keeps a `conversationByUser` cache keyed by `${channel}:${user_id}` so
 * tab switches don't re-issue GETs.
 *
 * 2026-05-16 audit follow-up: pane polls the selected user's
 * conversation every `CONV_POLL_MS` while mounted so Telegram replies
 * land without a manual Refresh. Render order flipped to newest-first
 * and the visible slice is capped at `CONV_DISPLAY_CAP` (mirrors the
 * retention cap surfaced in the hint line). */
import { useEffect, useMemo } from 'react';
import { linkifyText } from '../../lib/linkify';
import {
  useChannelsStore,
  selectUsersForChannel,
  selectConversation,
  type ConversationRow,
} from '../../stores/channels';

interface ConversationsPaneProps {
  channel: string;
}

interface RetentionHintProps {
  count: number;
  cap: number;
}

// Poll cadence for the active conversation. 5s strikes a balance: fast
// enough for a phone-side reply to surface within a glance, slow enough
// to not hammer the backend during long idle reading. The cleanup hook
// on the selected-user effect cancels the interval when the pane
// unmounts or the operator switches users.
const CONV_POLL_MS = 5000;

// Hard cap on rows rendered in the pane. Mirrors the retention window
// (`channels.yaml::telegram.max_turns_in_context`, default 20) so the
// operator sees exactly the slice TARS still has in his context —
// older rows persist in the JSONL but aren't rendered here. Bumping
// this cap risks a long-scroll list that obscures recent replies.
const CONV_DISPLAY_CAP = 20;

function _fmtTs(iso: string): string {
  if (!iso) return '';
  try {
    const dt = new Date(iso);
    if (Number.isNaN(dt.getTime())) return iso;
    return dt.toLocaleString();
  } catch {
    return iso;
  }
}

function RetentionHint({ count, cap }: RetentionHintProps) {
  return (
    <div className="channel-conv-retention t-meta" data-testid="channel-conv-retention">
      Last {Math.min(count, cap)} turn{cap === 1 ? '' : 's'} retained in TARS&apos;s
      context (max: {cap}); older messages persist here but are out of his prompt.
    </div>
  );
}

export function ConversationsPane({ channel }: ConversationsPaneProps) {
  const users = useChannelsStore((s) => selectUsersForChannel(s, channel));
  const fetchUsers = useChannelsStore((s) => s.fetchUsers);
  const fetchConversation = useChannelsStore((s) => s.fetchConversation);
  const selectUser = useChannelsStore((s) => s.selectUser);
  const selectedUserId = useChannelsStore(
    (s) => s.selectedUserIdByChannel[channel] ?? null,
  );
  const rows = useChannelsStore((s) =>
    selectConversation(s, channel, selectedUserId),
  );
  const pending = useChannelsStore((s) => s.pending);

  const allowed = useMemo(
    () => users.filter((u) => u.state === 'allowed'),
    [users],
  );

  useEffect(() => {
    void fetchUsers(channel);
  }, [channel, fetchUsers]);

  // Auto-pick the first allowed user when the operator opens the pane
  // with nothing selected — saves a click on the common single-user path.
  useEffect(() => {
    if (selectedUserId) return;
    if (allowed.length === 0) return;
    selectUser(channel, allowed[0].user_id);
  }, [allowed, channel, selectUser, selectedUserId]);

  useEffect(() => {
    if (!selectedUserId) return;
    // Initial fetch is fire-and-forget so the first paint isn't blocked
    // on the network. Subsequent polls run on the interval below so a
    // Telegram reply that lands while the operator is looking at the
    // pane shows up without a manual Refresh.
    void fetchConversation(channel, selectedUserId);
    const id = window.setInterval(() => {
      void fetchConversation(channel, selectedUserId);
    }, CONV_POLL_MS);
    return () => {
      window.clearInterval(id);
    };
  }, [channel, fetchConversation, selectedUserId]);

  const convBusy = selectedUserId
    ? Boolean(pending[`${channel}:conv:${selectedUserId}`])
    : false;

  // Retention cap mirrors `channels.yaml::telegram.max_turns_in_context`
  // (default 20). Phase doc §2 names it explicitly; we hard-code the
  // default rather than wire a /config call because the value is
  // operator-static and the misalignment cost (a wrong number in a hint
  // line) is small. If the cap changes per-channel later, surface it on
  // the channel snapshot. Kept in lockstep with ``CONV_DISPLAY_CAP``.
  const retentionCap = CONV_DISPLAY_CAP;

  return (
    <div className="channel-conv" data-testid="channel-conv-pane">
      <aside className="channel-conv-rail" aria-label="allowed users">
        {allowed.length === 0 ? (
          <div className="channels-empty">No approved users yet.</div>
        ) : (
          allowed.map((u) => (
            <button
              key={u.user_id}
              type="button"
              className={`channels-rail-btn${
                u.user_id === selectedUserId ? ' is-active' : ''
              }`}
              onClick={() => selectUser(channel, u.user_id)}
              data-testid={`channel-conv-user:${u.user_id}`}
              aria-current={u.user_id === selectedUserId ? 'page' : undefined}
            >
              <span className="channel-conv-rail-name">{u.display_name}</span>
              <span className="channel-conv-rail-id t-meta">{u.user_id}</span>
            </button>
          ))
        )}
      </aside>

      <div className="channel-conv-body">
        {!selectedUserId && (
          <div className="channels-empty">Select a user to read their history.</div>
        )}
        {selectedUserId && (
          <>
            <RetentionHint count={rows.length} cap={retentionCap} />
            {convBusy && rows.length === 0 ? (
              <div className="channel-conv-empty">loading…</div>
            ) : rows.length === 0 ? (
              <div className="channel-conv-empty">No messages on file.</div>
            ) : (
              <ConversationList rows={rows} />
            )}
          </>
        )}
      </div>
    </div>
  );
}

function ConversationList({ rows }: { rows: readonly ConversationRow[] }) {
  // 2026-05-16: render newest-first and cap at ``CONV_DISPLAY_CAP`` so
  // the freshest reply is always at the top of the pane and old rows
  // beyond the retention window stop competing for visual space. The
  // backend ``tail`` already returns newest-first (see
  // ``ConversationStore.tail``), so we simply take the leading slice.
  const ordered = useMemo(
    () => rows.slice(0, CONV_DISPLAY_CAP),
    [rows],
  );
  return (
    <div className="channel-conv-list" data-testid="channel-conv-list">
      {ordered.map((row, idx) => (
        <div
          key={`${row.ts}:${idx}`}
          className={`channel-conv-row channel-conv-row-${row.direction}`}
          data-testid={`channel-conv-row:${row.direction}`}
        >
          <div className="channel-conv-row-meta">
            <span className="channel-conv-row-ts t-meta">{_fmtTs(row.ts)}</span>
            <span className="channel-conv-row-direction t-meta">
              {row.direction === 'inbound' ? '→ tars' : '← tars'}
            </span>
          </div>
          <span className="channel-conv-row-body">{linkifyText(row.body)}</span>
        </div>
      ))}
    </div>
  );
}

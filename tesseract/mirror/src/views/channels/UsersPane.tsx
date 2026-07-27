/* MO-9-12 Users pane — allowlist + pending + blocked tables.
 *
 * The operator approves a pending row by opening the ApprovalModal; the
 * tier dropdown defaults to "operator" (the only enabled tier today —
 * friend is ghosted with a tooltip until the multi-user milestone lands).
 * Revoke / Block fire ASK round-trips without a modal because they carry
 * no operator-supplied fields. The pane re-pulls the user list after every
 * approved mutation so the row moves between tables without a Refresh.
 */
import { useEffect, useState } from 'react';
import {
  useChannelsStore,
  selectUsersForChannel,
  type ChannelUser,
} from '../../stores/channels';
import { useToastStore } from '../../stores/toasts';
import { useWebSocketStore } from '../../stores/websocket';
import { ApprovalModal } from './ApprovalModal';

interface UsersPaneProps {
  channel: string;
}

function _fmtSeen(iso: string): string {
  if (!iso) return '—';
  try {
    const dt = new Date(iso);
    if (Number.isNaN(dt.getTime())) return iso;
    return dt.toLocaleString();
  } catch {
    return iso;
  }
}

export function UsersPane({ channel }: UsersPaneProps) {
  const users = useChannelsStore((s) => selectUsersForChannel(s, channel));
  const fetchUsers = useChannelsStore((s) => s.fetchUsers);
  const revokeUser = useChannelsStore((s) => s.revokeUser);
  const blockUser = useChannelsStore((s) => s.blockUser);
  const pending = useChannelsStore((s) => s.pending);
  const error = useChannelsStore((s) => s.error);
  const sessionId = useWebSocketStore((s) => s.sessionId);
  const push = useToastStore((s) => s.push);
  const [pendingApproval, setPendingApproval] = useState<ChannelUser | null>(null);

  useEffect(() => {
    void fetchUsers(channel);
  }, [channel, fetchUsers]);

  const _requireSession = (): string | null => {
    if (!sessionId) {
      push('Channels: no operator session — open chat first', 'warning');
      return null;
    }
    return sessionId;
  };

  const _onRevoke = async (user: ChannelUser) => {
    const sid = _requireSession();
    if (!sid) return;
    try {
      const result = await revokeUser(channel, user.user_id, sid);
      if (result.approved) {
        push(`${user.display_name || user.user_id} revoked`, 'info');
      } else {
        push(`Revoke denied: ${result.output}`, 'warning');
      }
    } catch (err) {
      push(
        `Revoke failed: ${err instanceof Error ? err.message : String(err)}`,
        'error',
      );
    }
  };

  const _onBlock = async (user: ChannelUser) => {
    const sid = _requireSession();
    if (!sid) return;
    try {
      const result = await blockUser(channel, user.user_id, sid);
      if (result.approved) {
        push(`${user.display_name || user.user_id} blocked`, 'info');
      } else {
        push(`Block denied: ${result.output}`, 'warning');
      }
    } catch (err) {
      push(
        `Block failed: ${err instanceof Error ? err.message : String(err)}`,
        'error',
      );
    }
  };

  const allowed = users.filter((u) => u.state === 'allowed');
  const pendingRows = users.filter((u) => u.state === 'pending');
  const blocked = users.filter((u) => u.state === 'blocked');
  const usersLoading = Boolean(pending[`${channel}:users`]);

  return (
    <div data-testid="channel-users-pane">
      <div className="channel-users-head">
        <span className="channel-users-meta t-meta">
          {allowed.length} allowed · {pendingRows.length} pending · {blocked.length} blocked
        </span>
        <button
          type="button"
          className="channels-view-btn"
          onClick={() => void fetchUsers(channel)}
          disabled={usersLoading}
          data-testid="channel-users-refresh"
          aria-label="refresh user list"
        >
          {usersLoading ? 'loading…' : 'refresh'}
        </button>
      </div>

      <section className="channel-users-section" data-testid="channel-users-pending">
        <h3 className="channel-users-title">Pending</h3>
        {pendingRows.length === 0 ? (
          <div className="channel-users-empty">No pending requests.</div>
        ) : (
          <UserTable
            rows={pendingRows}
            actions={(row) => (
              <>
                <button
                  type="button"
                  className="channel-user-btn channel-user-btn-primary"
                  onClick={() => setPendingApproval(row)}
                  data-testid={`channel-user-approve:${row.user_id}`}
                  aria-label={`approve ${row.user_id}`}
                  disabled={Boolean(pending[`${channel}:approve:${row.user_id}`])}
                >
                  approve
                </button>
                <button
                  type="button"
                  className="channel-user-btn"
                  onClick={() => void _onBlock(row)}
                  data-testid={`channel-user-block:${row.user_id}`}
                  aria-label={`block ${row.user_id}`}
                  disabled={Boolean(pending[`${channel}:block:${row.user_id}`])}
                >
                  block
                </button>
              </>
            )}
          />
        )}
      </section>

      <section className="channel-users-section" data-testid="channel-users-allowed">
        <h3 className="channel-users-title">Allowlist</h3>
        {allowed.length === 0 ? (
          <div className="channel-users-empty">No allowed users yet.</div>
        ) : (
          <UserTable
            rows={allowed}
            actions={(row) => (
              <>
                <button
                  type="button"
                  className="channel-user-btn"
                  onClick={() => void _onRevoke(row)}
                  data-testid={`channel-user-revoke:${row.user_id}`}
                  aria-label={`revoke ${row.user_id}`}
                  disabled={Boolean(pending[`${channel}:revoke:${row.user_id}`])}
                >
                  revoke
                </button>
                <button
                  type="button"
                  className="channel-user-btn"
                  onClick={() => void _onBlock(row)}
                  data-testid={`channel-user-block:${row.user_id}`}
                  aria-label={`block ${row.user_id}`}
                  disabled={Boolean(pending[`${channel}:block:${row.user_id}`])}
                >
                  block
                </button>
              </>
            )}
          />
        )}
      </section>

      {blocked.length > 0 && (
        <section className="channel-users-section" data-testid="channel-users-blocked">
          <h3 className="channel-users-title">Blocked</h3>
          <UserTable
            rows={blocked}
            actions={(row) => (
              <button
                type="button"
                className="channel-user-btn"
                onClick={() => void _onRevoke(row)}
                data-testid={`channel-user-revoke:${row.user_id}`}
                aria-label={`unblock ${row.user_id}`}
                disabled={Boolean(pending[`${channel}:revoke:${row.user_id}`])}
              >
                unblock
              </button>
            )}
          />
        </section>
      )}

      {error && (
        <div className="channel-error" data-testid="channel-users-error">
          {error}
        </div>
      )}

      {pendingApproval && (
        <ApprovalModal
          channel={channel}
          user={pendingApproval}
          onClose={() => setPendingApproval(null)}
        />
      )}
    </div>
  );
}

interface UserTableProps {
  rows: readonly ChannelUser[];
  actions: (row: ChannelUser) => React.ReactNode;
}

function UserTable({ rows, actions }: UserTableProps) {
  return (
    <table className="channel-users-table">
      <thead>
        <tr>
          <th>user id</th>
          <th>display</th>
          <th>tier</th>
          <th>ttl</th>
          <th>last seen</th>
          <th>msgs</th>
          <th>actions</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => (
          <tr key={row.user_id} data-testid={`channel-user-row:${row.user_id}`}>
            <td className="channel-users-id">{row.user_id}</td>
            <td>{row.display_name}</td>
            <td>{row.tier}</td>
            <td className="t-meta">{row.ttl_iso ?? '—'}</td>
            <td className="t-meta">{_fmtSeen(row.last_seen)}</td>
            <td>{row.messages_total}</td>
            <td className="channel-users-actions">{actions(row)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

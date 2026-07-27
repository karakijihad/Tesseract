/* MO-9-12 Approval modal — operator-facing form for approving a pending chat.
 *
 * Tier dropdown ships with `operator` enabled and `friend` ghosted; TTL
 * date picker is rendered but disabled. Both fields exist in the POST
 * payload so the backend contract is the multi-user-milestone shape even
 * though enforcement is deferred (phase doc §7.3 / GOVERNANCE rule 14).
 * The default tier is `operator` so the common case — operator approving
 * their own second Telegram chat — is a single-click flow.
 */
import { useEffect, useState } from 'react';
import {
  useChannelsStore,
  type ChannelUser,
  type ChannelUserTier,
} from '../../stores/channels';
import { useToastStore } from '../../stores/toasts';
import { useWebSocketStore } from '../../stores/websocket';

interface ApprovalModalProps {
  channel: string;
  user: ChannelUser;
  onClose: () => void;
}

export function ApprovalModal({ channel, user, onClose }: ApprovalModalProps) {
  const approveUser = useChannelsStore((s) => s.approveUser);
  const pending = useChannelsStore((s) => s.pending);
  const sessionId = useWebSocketStore((s) => s.sessionId);
  const push = useToastStore((s) => s.push);

  const [displayName, setDisplayName] = useState<string>(user.display_name || '');
  const [tier, setTier] = useState<ChannelUserTier>('operator');
  const [ttlIso, setTtlIso] = useState<string>('');
  const busy = Boolean(pending[`${channel}:approve:${user.user_id}`]);

  // Close on Escape — modal is operator-facing so the existing keyboard
  // affordance pattern (Esc to dismiss) carries through from settings.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const _onConfirm = async () => {
    if (!sessionId) {
      push('Channels: no operator session — open chat first', 'warning');
      return;
    }
    try {
      const result = await approveUser(
        channel,
        {
          user_id: user.user_id,
          tier,
          ttl_iso: ttlIso ? ttlIso : null,
          display_name: displayName ? displayName : null,
        },
        sessionId,
      );
      if (result.approved) {
        push(`${user.user_id} approved as ${tier}`, 'info');
        onClose();
      } else {
        push(`Approve denied: ${result.output}`, 'warning');
      }
    } catch (err) {
      push(
        `Approve failed: ${err instanceof Error ? err.message : String(err)}`,
        'error',
      );
    }
  };

  return (
    <div
      className="channel-modal-overlay"
      role="dialog"
      aria-modal="true"
      aria-label="approve channel user"
      data-testid="channel-approval-modal"
      onClick={(e) => {
        // Backdrop click dismisses. The dialog body uses stopPropagation
        // so clicks inside the form don't bubble back here.
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        className="channel-modal"
        onClick={(e) => e.stopPropagation()}
        data-testid="channel-approval-modal-body"
      >
        <header className="channel-modal-head">
          <span className="channel-modal-title">Approve channel user</span>
          <button
            type="button"
            className="channel-modal-close"
            onClick={onClose}
            aria-label="close approval modal"
            data-testid="channel-approval-cancel"
          >
            ×
          </button>
        </header>

        <div className="channel-modal-meta t-meta">
          {channel} · user_id {user.user_id}
        </div>

        <label className="channel-modal-field">
          <span className="channel-modal-label">display name</span>
          <input
            type="text"
            value={displayName}
            onChange={(e) => setDisplayName(e.target.value)}
            placeholder={user.display_name || 'name shown in records'}
            data-testid="channel-approval-display-name"
            className="channel-modal-input"
          />
        </label>

        <label className="channel-modal-field">
          <span className="channel-modal-label">tier</span>
          <select
            value={tier}
            onChange={(e) => setTier(e.target.value as ChannelUserTier)}
            data-testid="channel-approval-tier"
            className="channel-modal-input"
          >
            <option value="operator">operator</option>
            <option value="friend" disabled title="available in multi-user milestone">
              friend (multi-user milestone)
            </option>
          </select>
          <span className="channel-modal-hint t-meta">
            tier enforcement ships with the multi-user milestone
          </span>
        </label>

        <label className="channel-modal-field">
          <span className="channel-modal-label">TTL (UTC date)</span>
          {/* Native date picker so enabling the field in the multi-user
              milestone needs zero form changes; today it's disabled and
              the value still ships in the POST payload (inert). */}
          <input
            type="date"
            value={ttlIso}
            onChange={(e) => setTtlIso(e.target.value)}
            placeholder="no expiry"
            disabled
            title="available in multi-user milestone"
            data-testid="channel-approval-ttl"
            className="channel-modal-input"
          />
          <span className="channel-modal-hint t-meta">
            TTL enforcement ships with the multi-user milestone
          </span>
        </label>

        <div className="channel-modal-actions">
          <button
            type="button"
            className="channel-user-btn"
            onClick={onClose}
            disabled={busy}
          >
            cancel
          </button>
          <button
            type="button"
            className="channel-user-btn channel-user-btn-primary"
            onClick={() => void _onConfirm()}
            disabled={busy}
            data-testid="channel-approval-confirm"
          >
            {busy ? 'approving…' : 'approve'}
          </button>
        </div>
      </div>
    </div>
  );
}

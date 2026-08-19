/* MO-9-12 Approval modal — operator-facing form for approving a pending chat.
 *
 * Tier dropdown ships with `operator` enabled and `friend` ghosted; TTL
 * date picker is rendered but disabled. Both fields exist in the POST
 * payload so the backend contract is the multi-user-milestone shape even
 * though enforcement is deferred (phase doc §7.3 / GOVERNANCE rule 14).
 * The default tier is `operator` so the common case — operator approving
 * their own second Telegram chat — is a single-click flow.
 */
import { Select } from '../../components/common/Select';
import { useState } from 'react';
import {
  useChannelsStore,
  refusalToast,
  type ChannelUser,
  type ChannelUserTier,
} from '../../stores/channels';
import { useToastStore } from '../../stores/toasts';
import { useWebSocketStore } from '../../stores/websocket';
import { Hint } from '../../components/ui/Hint';
import { Input } from '../../components/common/Input';
import { CloseButton } from '../../components/common/CloseButton';
import { Button } from '../../components/common/Button';
import { Modal } from '../../components/common/Modal';

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
        push(refusalToast('Approve', result), 'warning');
      }
    } catch (err) {
      push(
        `Approve failed: ${err instanceof Error ? err.message : String(err)}`,
        'error',
      );
    }
  };

  return (
    <Modal
      onClose={onClose}
      ariaLabel="approve channel user"
      className="channel-modal"
      testId="channel-approval-modal"
    >
        <header className="channel-modal-head">
          <span className="channel-modal-title">Approve channel user</span>
          <CloseButton
            onClick={onClose}
            ariaLabel="close approval modal"
            testId="channel-approval-cancel"
          />
        </header>

        <div className="channel-modal-meta t-meta">
          {channel} · user_id {user.user_id}
        </div>

        <label className="channel-modal-field">
          <span className="channel-modal-label">display name</span>
          <Input
            value={displayName}
            onChange={setDisplayName}
            placeholder={user.display_name || 'name shown in records'}
            testId="channel-approval-display-name"
            className="channel-modal-input"
          />
        </label>

        <label className="channel-modal-field">
          <span className="channel-modal-label">tier</span>
          <Select
            value={tier}
            options={[
              { value: 'operator', label: 'operator' },
              {
                value: 'friend',
                label: 'friend (multi-user milestone)',
                disabled: true,
              },
            ]}
            onChange={(v) => setTier(v as ChannelUserTier)}
            ariaLabel="Approve as tier"
            testId="channel-approval-tier"
          />
          <span className="channel-modal-hint t-meta">
            tier enforcement ships with the multi-user milestone
          </span>
        </label>

        <label className="channel-modal-field">
          <span className="channel-modal-label">TTL (UTC date)</span>
          {/* Native date picker so enabling the field in the multi-user
              milestone needs zero form changes; today it's disabled and
              the value still ships in the POST payload (inert). */}
          <Hint label="available in multi-user milestone">
            <Input
              type="date"
              value={ttlIso}
              onChange={setTtlIso}
              placeholder="no expiry"
              disabled
              testId="channel-approval-ttl"
              className="channel-modal-input"
            />
          </Hint>
          <span className="channel-modal-hint t-meta">
            TTL enforcement ships with the multi-user milestone
          </span>
        </label>

        <div className="channel-modal-actions">
          <Button onClick={onClose} disabled={busy}>
            cancel
          </Button>
          <Button
            tone="primary"
            onClick={() => void _onConfirm()}
            disabled={busy}
            testId="channel-approval-confirm"
          >
            {busy ? 'approving…' : 'approve'}
          </Button>
        </div>
    </Modal>
  );
}

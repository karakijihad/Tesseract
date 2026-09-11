/* MO-9-12 Approval modal — operator-facing form for approving a pending chat.
 *
 * There is nothing to choose but the name. Approving puts the chat on the
 * allowlist and being on it is the whole permission, so the tier dropdown
 * that used to sit here is gone rather than ghosted: a control offering one
 * value is a question with one answer. The TTL picker stays disabled until
 * expiry is enforced.
 */
import { useState } from 'react';
import {
  useChannelsStore,
  refusalToast,
  type ChannelUser,
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
  const [ttlIso, setTtlIso] = useState<string>('');
  const busy = Boolean(pending[`${channel}:approve:${user.user_id}`]);

  const _onConfirm = async () => {
    if (!sessionId) {
      push('Channels: no session yet. Open chat first.', 'warning');
      return;
    }
    try {
      const result = await approveUser(
        channel,
        {
          user_id: user.user_id,
          ttl_iso: ttlIso ? ttlIso : null,
          display_name: displayName ? displayName : null,
        },
        sessionId,
      );
      if (result.approved) {
        push(`${user.user_id} approved`, 'info');
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

import { useEffect, useRef, useState } from 'react';
import type { ApprovalRequest } from '../../lib/types';
import { useConversationStore } from '../../stores/conversation';

interface Props {
  approval: ApprovalRequest;
  isPrimary?: boolean;
}

type Verdict = 'approved' | 'denied';

export function ApprovalCard({ approval, isPrimary = false }: Props) {
  const { call_id, name, input, reason } = approval;
  const wrapperRef = useRef<HTMLDivElement>(null);
  const [verdict, setVerdict] = useState<Verdict | null>(null);

  useEffect(() => {
    if (isPrimary) wrapperRef.current?.focus();
  }, [isPrimary]);

  const resolve = (approved: boolean) => {
    if (verdict !== null) return;
    setVerdict(approved ? 'approved' : 'denied');
    useConversationStore.getState().resolveApproval(null, call_id, approved);
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    if (verdict !== null) return;
    if (e.key === 'y' || e.key === 'Y') {
      e.preventDefault();
      resolve(true);
    } else if (e.key === 'n' || e.key === 'N') {
      e.preventDefault();
      resolve(false);
    }
  };

  const keepFocus = (e: React.MouseEvent) => e.preventDefault();
  const resolved = verdict !== null;

  return (
    <div
      ref={wrapperRef}
      className="approval-card"
      tabIndex={0}
      role="group"
      aria-live="polite"
      aria-label={`Approval required for ${name}`}
      onKeyDown={handleKeyDown}
    >
      <div className="approval-card-header">approval required</div>
      <div className="approval-card-tool">{name}</div>
      <pre className="approval-card-summary">{JSON.stringify(input, null, 2)}</pre>
      {reason && (
        <div style={{ fontSize: 10, color: 'var(--text-dim)', marginBottom: 10 }}>reason: {reason}</div>
      )}
      <div className="approval-card-actions">
        <button
          className="approval-btn approve"
          onClick={() => resolve(true)}
          onMouseDown={keepFocus}
          disabled={resolved}
        >
          {verdict === 'approved' ? 'Approved' : 'Approve (y)'}
        </button>
        <button
          className="approval-btn deny"
          onClick={() => resolve(false)}
          onMouseDown={keepFocus}
          disabled={resolved}
        >
          {verdict === 'denied' ? 'Denied' : 'Deny (n)'}
        </button>
      </div>
    </div>
  );
}

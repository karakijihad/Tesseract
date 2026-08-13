import { useState } from 'react';
import { useEntityName } from '../../hooks/useEntityName';
import type { WorkspaceComment } from '../../stores/workspace';
import { useWorkspaceStore } from '../../stores/workspace';
import { Markdown } from '../../components/common/Markdown';

interface Props {
  event_id: string;
  comments: WorkspaceComment[];
}

function formatTs(ts: string): string {
  try {
    return new Date(ts).toLocaleString([], {
      hour12: false,
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    });
  } catch {
    return ts;
  }
}

export function CommentThread({ event_id, comments }: Props) {
  const entityName = useEntityName();
  const [draft, setDraft] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const submit = useWorkspaceStore((s) => s.comment);
  const pending = useWorkspaceStore((s) => s.pendingThreads[event_id]);

  const onSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    const body = draft.trim();
    if (!body || busy) return;
    setBusy(true);
    setError(null);
    try {
      await submit(event_id, body);
      setDraft('');
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const indicatorLabel =
    pending?.state === 'thinking'
      ? `${entityName} is thinking…`
      : pending?.state === 'queued'
        ? `${entityName} queued — waiting for current turn to finish…`
        : null;

  return (
    <div className="workspace-thread">
      {comments.length === 0 && !indicatorLabel ? (
        <p className="workspace-thread-empty t-caption">No comments yet.</p>
      ) : (
        <ul className="workspace-thread-list">
          {comments.map((c) => (
            <li
              key={c.comment_id}
              className={`workspace-comment workspace-comment--${c.author}`}
            >
              <div className="workspace-comment-head">
                <span className="workspace-comment-author t-meta">{c.author}</span>
                <span className="workspace-comment-ts t-meta">{formatTs(c.ts)}</span>
              </div>
              <div className="workspace-comment-body">
                <Markdown>{c.body}</Markdown>
              </div>
            </li>
          ))}
          {indicatorLabel && (
            <li
              className={`workspace-comment workspace-comment--agent workspace-comment--pending workspace-comment--pending-${pending?.state}`}
              aria-live="polite"
            >
              <div className="workspace-comment-head">
                <span className="workspace-comment-author t-meta">{entityName}</span>
                <span className="workspace-comment-ts t-meta workspace-thread-pending-dots">
                  <span /><span /><span />
                </span>
              </div>
              <div className="workspace-comment-body t-meta">{indicatorLabel}</div>
            </li>
          )}
        </ul>
      )}
      <form className="workspace-thread-form" onSubmit={onSubmit}>
        <textarea
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder={`Comment — ${entityName} will see this on the next turn`}
          rows={2}
          disabled={busy}
        />
        <div className="workspace-thread-form-row">
          {error && <span className="workspace-thread-error t-caption">{error}</span>}
          <button type="submit" disabled={busy || !draft.trim()}>
            {busy ? 'Sending…' : 'Send'}
          </button>
        </div>
      </form>
    </div>
  );
}

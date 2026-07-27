import { useEffect, useRef, useState } from 'react';
import { postOperatorPost, type OperatorPostInput } from '../../lib/api';

interface Props {
  source?: OperatorPostInput['source'];
  buttonLabel?: string;
}

export function NewThreadButton({ source = 'button', buttonLabel = 'New' }: Props) {
  const [open, setOpen] = useState(false);
  const [title, setTitle] = useState('');
  const [body, setBody] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const titleRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    if (open) {
      setError(null);
      setTimeout(() => titleRef.current?.focus(), 0);
    }
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        setOpen(false);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open]);

  const submit = async () => {
    const trimmedBody = body.trim();
    if (!trimmedBody || busy) return;
    setBusy(true);
    setError(null);
    try {
      await postOperatorPost({ title: title.trim(), body: trimmedBody, source });
      setTitle('');
      setBody('');
      setOpen(false);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  if (!open) {
    return (
      <button
        type="button"
        className="workspace-new-thread-trigger"
        onClick={() => setOpen(true)}
      >
        {buttonLabel}
      </button>
    );
  }

  return (
    <div
      className="workspace-modal-backdrop"
      onClick={(e) => {
        if (e.target === e.currentTarget) setOpen(false);
      }}
    >
      <div
        className="workspace-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="new-thread-title"
        tabIndex={-1}
      >
        <h2 id="new-thread-title" className="t-head workspace-modal-title">
          New workspace thread
        </h2>
        <input
          ref={titleRef}
          className="workspace-modal-input"
          type="text"
          placeholder="Title (optional)"
          value={title}
          maxLength={200}
          onChange={(e) => setTitle(e.target.value)}
          disabled={busy}
        />
        <textarea
          className="workspace-modal-textarea"
          placeholder="Body — what should TARS know?"
          value={body}
          maxLength={4000}
          rows={6}
          onChange={(e) => setBody(e.target.value)}
          disabled={busy}
        />
        <div className="workspace-modal-row">
          {error && <span className="workspace-modal-error t-caption">{error}</span>}
          <button
            type="button"
            className="workspace-modal-cancel"
            onClick={() => setOpen(false)}
            disabled={busy}
          >
            Cancel
          </button>
          <button
            type="button"
            className="workspace-modal-send"
            onClick={() => void submit()}
            disabled={busy || !body.trim()}
          >
            {busy ? 'Sending…' : 'Post'}
          </button>
        </div>
      </div>
    </div>
  );
}

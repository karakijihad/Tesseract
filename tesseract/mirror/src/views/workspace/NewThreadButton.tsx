import { useEffect, useRef, useState } from 'react';
import { useEntityName } from '../../hooks/useEntityName';
import { postOperatorPost, type OperatorPostInput } from '../../lib/api';
import { Input } from '../../components/common/Input';
import { Textarea } from '../../components/common/Textarea';
import { Button } from '../../components/common/Button';
import { Modal } from '../../components/common/Modal';

interface Props {
  source?: OperatorPostInput['source'];
  buttonLabel?: string;
}

export function NewThreadButton({ source = 'button', buttonLabel = 'New' }: Props) {
  const entityName = useEntityName();
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
      <Button tone="primary" onClick={() => setOpen(true)}>
        {buttonLabel}
      </Button>
    );
  }

  return (
    <Modal
      onClose={() => setOpen(false)}
      ariaLabel="new workspace thread"
      ariaLabelledBy="new-thread-title"
      className="workspace-modal"
    >
        <h2 id="new-thread-title" className="t-head workspace-modal-title">
          New workspace thread
        </h2>
        <Input
          inputRef={titleRef}
          className="workspace-modal-input"
          placeholder="Title (optional)"
          value={title}
          maxLength={200}
          onChange={setTitle}
          disabled={busy}
        />
        <Textarea
          className="workspace-modal-textarea"
          placeholder={`Body — what should ${entityName} know?`}
          value={body}
          maxLength={4000}
          rows={6}
          onChange={setBody}
          disabled={busy}
        />
        <div className="workspace-modal-row">
          {error && <span className="workspace-modal-error t-caption">{error}</span>}
          <Button onClick={() => setOpen(false)} disabled={busy}>
            Cancel
          </Button>
          <Button
            tone="primary"
            onClick={() => void submit()}
            disabled={busy || !body.trim()}
          >
            {busy ? 'Sending…' : 'Post'}
          </Button>
        </div>
    </Modal>
  );
}

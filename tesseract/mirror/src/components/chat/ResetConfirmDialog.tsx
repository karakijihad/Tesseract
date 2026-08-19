import { useEffect, useRef } from 'react';
import { useResetDialogStore } from '../../stores/resetDialog';
import { sendCommand } from '../../lib/commands';
import './ResetConfirmDialog.css';
import { Button } from '../common/Button';
import { Modal } from '../common/Modal';

export function ResetConfirmDialog() {
  const open = useResetDialogStore((s) => s.open);
  const closeDialog = useResetDialogStore((s) => s.closeDialog);
  const reflectBtnRef = useRef<HTMLButtonElement>(null);

  // Escape and the scrim belong to `Modal`; what is left here is where focus
  // lands, which is this dialog's own choice — the reflecting option, not the
  // destructive one beside it.
  useEffect(() => {
    if (open) reflectBtnRef.current?.focus();
  }, [open]);

  if (!open) return null;

  const onReflect = () => {
    sendCommand('/reset reflect');
    closeDialog();
  };
  const onClear = () => {
    sendCommand('/reset clear');
    closeDialog();
  };

  return (
    <Modal
      onClose={closeDialog}
      ariaLabel="reset this conversation"
      ariaLabelledBy="reset-dialog-title"
      className="reset-dialog"
    >
        <h2 id="reset-dialog-title" className="reset-dialog-title">
          Reset this conversation
        </h2>
        <p className="reset-dialog-body t-meta">
          Reflect on the session before clearing? <strong>Reflect &amp; clear</strong>{' '}
          autosaves the transcript and runs reflection in the background.{' '}
          <strong>Just clear</strong> wipes the chat with zero side effects — no save, no
          reflection.
        </p>
        <div className="reset-dialog-actions">
          <Button ref={reflectBtnRef} tone="primary" onClick={onReflect}>
            Reflect &amp; clear
          </Button>
          <Button onClick={onClear}>Just clear</Button>
          <Button onClick={closeDialog}>Cancel</Button>
        </div>
    </Modal>
  );
}

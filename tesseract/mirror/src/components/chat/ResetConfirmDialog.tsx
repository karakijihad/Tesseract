import { useEffect, useRef } from 'react';
import { useResetDialogStore } from '../../stores/resetDialog';
import { sendCommand } from '../../lib/commands';
import './ResetConfirmDialog.css';
import { Button } from '../common/Button';
import { Modal } from '../common/Modal';

/**
 * Three answers, and the same three a channel offers.
 *
 * Each label IS its consequence rather than its gesture, so none of them needs
 * a Hint; the line under each one says what it means for the work. Visible
 * text rather than a popover because three options are being compared, and a
 * comparison nobody can see at once is not one.
 *
 * Handing over is the only one that stays in this chat, and that is the point
 * rather than an inconsistency: the work continues, so it continues here, with
 * the package in front of it. The other two are the operator finished with
 * this conversation, so a fresh chat is the right place to land.
 */
export function ResetConfirmDialog() {
  const open = useResetDialogStore((s) => s.open);
  const closeDialog = useResetDialogStore((s) => s.closeDialog);
  const handoffBtnRef = useRef<HTMLButtonElement>(null);

  // Escape and the scrim belong to `Modal`; what is left here is where focus
  // lands, which is this dialog's own choice — the answer that keeps the most,
  // never the destructive one beside it.
  useEffect(() => {
    if (open) handoffBtnRef.current?.focus();
  }, [open]);

  if (!open) return null;

  const answer = (arg: string) => () => {
    sendCommand(`/reset ${arg}`);
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
        What should be kept when this conversation goes?
      </p>
      <div className="reset-dialog-options">
        <div className="reset-dialog-option">
          <Button ref={handoffBtnRef} tone="primary" onClick={answer('handoff')}>
            Hand over and clear
          </Button>
          <p className="reset-dialog-hint t-meta">
            I wrap up first and say where the work stood, then this conversation
            empties and carries on here with that in front of it.
          </p>
        </div>
        <div className="reset-dialog-option">
          <Button onClick={answer('reflect')}>Clear, and keep what it taught</Button>
          <p className="reset-dialog-hint t-meta">
            The transcript is archived and a fresh chat opens. Nothing carries
            over, but I still write down what I learned.
          </p>
        </div>
        <div className="reset-dialog-option">
          <Button onClick={answer('clear')}>Just clear</Button>
          <p className="reset-dialog-hint t-meta">
            Gone, with nothing kept. No save, no reflection.
          </p>
        </div>
      </div>
      <div className="reset-dialog-actions">
        <Button onClick={closeDialog}>Cancel</Button>
      </div>
    </Modal>
  );
}

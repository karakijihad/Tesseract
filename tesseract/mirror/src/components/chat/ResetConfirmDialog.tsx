import { useEffect, useRef } from 'react';
import { useResetDialogStore } from '../../stores/resetDialog';
import { sendCommand } from '../../lib/commands';
import './ResetConfirmDialog.css';

export function ResetConfirmDialog() {
  const open = useResetDialogStore((s) => s.open);
  const closeDialog = useResetDialogStore((s) => s.closeDialog);
  const reflectBtnRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!open) return;
    reflectBtnRef.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        closeDialog();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, closeDialog]);

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
    <>
      <button
        type="button"
        className="reset-dialog-scrim"
        onClick={closeDialog}
        aria-label="Cancel reset"
      />
      <div
        className="reset-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="reset-dialog-title"
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
          <button
            ref={reflectBtnRef}
            type="button"
            className="reset-dialog-btn reset-dialog-btn-primary"
            onClick={onReflect}
          >
            Reflect &amp; clear
          </button>
          <button
            type="button"
            className="reset-dialog-btn"
            onClick={onClear}
          >
            Just clear
          </button>
          <button
            type="button"
            className="reset-dialog-btn reset-dialog-btn-cancel"
            onClick={closeDialog}
          >
            Cancel
          </button>
        </div>
      </div>
    </>
  );
}

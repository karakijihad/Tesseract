import { useEffect, useRef, type ReactNode } from 'react';
import './ExpandOverlay.css';
import { CloseButton } from './CloseButton';
import { Scrim } from './Scrim';

interface Props {
  open: boolean;
  onClose: () => void;
  title?: string;
  children: ReactNode;
  actions?: ReactNode;
}

const TITLE_ID = 'expand-overlay-title';

export function ExpandOverlay({ open, onClose, title, children, actions }: Props) {
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const previouslyFocused = useRef<HTMLElement | null>(null);
  // Latest-onClose ref so the focus-trap effect only re-runs when `open`
  // toggles. Without this, an inline-arrow `onClose` from the parent would
  // change identity on every render, tearing down the keydown listener and
  // snapping focus back via cleanup while the overlay is still visible.
  const onCloseRef = useRef(onClose);
  useEffect(() => { onCloseRef.current = onClose; }, [onClose]);

  useEffect(() => {
    if (!open) return;
    previouslyFocused.current = document.activeElement as HTMLElement | null;
    const node = dialogRef.current;
    if (!node) return;
    const focusable = node.querySelector<HTMLElement>(
      'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])',
    );
    focusable?.focus();

    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        onCloseRef.current();
        return;
      }
      if (e.key !== 'Tab') return;
      const all = node.querySelectorAll<HTMLElement>(
        'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
      );
      if (all.length === 0) return;
      const first = all[0];
      const last = all[all.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    };
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('keydown', onKey);
      previouslyFocused.current?.focus?.();
    };
  }, [open]);

  if (!open) return null;

  const labelProps = title
    ? { 'aria-labelledby': TITLE_ID }
    : { 'aria-label': 'Expanded preview' };

  return (
    <>
      <Scrim onClick={onClose} ariaLabel="Close overlay" level="panel" />
      <div
        ref={dialogRef}
        className="expand-overlay-panel"
        role="dialog"
        aria-modal="true"
        {...labelProps}
        tabIndex={-1}
      >
        <header className="expand-overlay-header">
          {title && <h2 id={TITLE_ID} className="expand-overlay-title">{title}</h2>}
          <div className="expand-overlay-actions">
            {actions}
            <CloseButton onClick={onClose} ariaLabel="Close (Esc)" />
          </div>
        </header>
        <div className="expand-overlay-body">{children}</div>
      </div>
    </>
  );
}

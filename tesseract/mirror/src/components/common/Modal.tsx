import { useEffect } from "react";
import type { ReactNode } from "react";

import { Scrim } from "./Scrim";

interface ModalProps {
  children: ReactNode;
  onClose: () => void;
  /** Names the dialog, and by extension the scrim that dismisses it. */
  ariaLabel: string;
  /** When the heading inside the panel already names it. */
  ariaLabelledBy?: string;
  /** The panel's own SIZE — a width cap, a max-height, a grid. Never its
   *  ground or its border: those are the modal's. */
  className?: string;
  testId?: string;
}

/** A panel over a dimmed app, and the one way out of it.
 *
 * Four surfaces drew this themselves — the agenda detail, the worker detail,
 * the channel approval and the new-thread sheet — and each got a different
 * subset of it right. All four dimmed with a hand-rolled backdrop `div` that
 * no keyboard could reach, all four then needed an
 * `onClick={(e) => e.stopPropagation()}` on the panel to stop the backdrop
 * closing under it, and only two of them closed on Escape.
 *
 * The scrim is the app's `Scrim`, so the dimming and the stacking order are
 * decided where every other overlay's are. The panel sits on its own layer
 * above it, which is why nothing here has to swallow a click.
 */
export function Modal({
  children,
  onClose,
  ariaLabel,
  ariaLabelledBy,
  className,
  testId,
}: ModalProps) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <>
      <Scrim onClick={onClose} ariaLabel={`Close ${ariaLabel}`} />
      <div className="modal-layer">
        <div
          className={className ? `modal ${className}` : "modal"}
          role="dialog"
          aria-modal="true"
          aria-label={ariaLabelledBy ? undefined : ariaLabel}
          aria-labelledby={ariaLabelledBy}
          data-testid={testId}
        >
          {children}
        </div>
      </div>
    </>
  );
}

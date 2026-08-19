import type { ReactNode } from "react";

/** The four things you can do to a turn from a composer. Colour follows the
 *  verb, not the surface — a redirect is amber wherever you are. */
export type ComposerVerb = "attach" | "send" | "steer" | "stop";

interface ComposerButtonProps {
  verb: ComposerVerb;
  children: ReactNode;
  onClick: () => void;
  ariaLabel: string;
  disabled?: boolean;
  testId?: string;
}

/** A verb at the end of a composer.
 *
 * There are two composers — the chat one and the HUD's ask-from-anywhere bar
 * — and they had two sets of these. The chat composer filled them (accent to
 * send, amber to redirect, red to stop); the HUD drew the same four as flat
 * transparent squares, so stopping a turn from the HUD looked like nothing in
 * particular. The colour belongs to the verb: `hud-chat.css` said as much in
 * a comment about redirect, and then only that one matched.
 */
export function ComposerButton({
  verb,
  children,
  onClick,
  ariaLabel,
  disabled = false,
  testId,
}: ComposerButtonProps) {
  return (
    <button
      type="button"
      className={`composer-btn composer-btn--${verb}`}
      onClick={onClick}
      disabled={disabled}
      aria-label={ariaLabel}
      data-testid={testId}
    >
      {children}
    </button>
  );
}

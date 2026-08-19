import type { Ref } from "react";

interface TextareaProps {
  value: string;
  onChange: (next: string) => void;
  placeholder?: string;
  disabled?: boolean;
  /** Layout only — the height or flex this surface needs. Never the field's
   *  own look, for the reason `Input`'s says the same thing. */
  className?: string;
  id?: string;
  ariaLabel?: string;
  maxLength?: number;
  rows?: number;
  spellCheck?: boolean;
  autoFocus?: boolean;
  testId?: string;
  inputRef?: Ref<HTMLTextAreaElement>;
  onKeyDown?: (e: React.KeyboardEvent<HTMLTextAreaElement>) => void;
  onBlur?: () => void;
}

/** The app's one multi-line field — `Input`'s sibling, and the same story.
 *
 * Three of the nine textareas in the tree had NO styling reaching them at
 * all: `workspace-modal-textarea` was a class name with no rule anywhere, and
 * the workspace comment box and the agenda note box carried no class, so all
 * three rendered as raw white browser boxes in a near-black app. The rest
 * disagreed about padding and line-height the way the inputs did.
 *
 * The chat composer is deliberately not this — it is its own shape, for the
 * same reason `Button` excludes the HUD.
 */
export function Textarea({
  value,
  onChange,
  placeholder,
  disabled = false,
  className,
  id,
  ariaLabel,
  maxLength,
  rows,
  spellCheck,
  autoFocus,
  testId,
  inputRef,
  onKeyDown,
  onBlur,
}: TextareaProps) {
  return (
    <textarea
      ref={inputRef}
      id={id}
      className={className ? `textarea ${className}` : "textarea"}
      value={value}
      placeholder={placeholder}
      disabled={disabled}
      aria-label={ariaLabel}
      maxLength={maxLength}
      rows={rows}
      spellCheck={spellCheck}
      autoFocus={autoFocus}
      data-testid={testId}
      onKeyDown={onKeyDown}
      onBlur={onBlur}
      onChange={(e) => onChange(e.target.value)}
    />
  );
}

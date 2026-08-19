import type { Ref } from "react";

type InputType = "text" | "number" | "search" | "password" | "date";

interface InputProps {
  value: string;
  onChange: (next: string) => void;
  type?: InputType;
  placeholder?: string;
  disabled?: boolean;
  /** Layout only — the width or flex this surface needs. Never the field's
   *  own look: a caller restyling padding, background, border or focus here
   *  is the divergence this component exists to end. */
  className?: string;
  id?: string;
  ariaLabel?: string;
  maxLength?: number;
  /** A bound on the value. A number on `type="number"`, an ISO `YYYY-MM-DD`
   *  string on `type="date"` — the platform's own two spellings for one idea. */
  min?: number | string;
  max?: number | string;
  step?: number;
  autoFocus?: boolean;
  spellCheck?: boolean;
  /** `"off"` on a secret, so the browser does not offer to remember it. */
  autoComplete?: string;
  /** Flags a value the surface has judged invalid — the field's own look does
   *  not change, so the surface still says why beside it. */
  ariaInvalid?: boolean;
  testId?: string;
  inputRef?: Ref<HTMLInputElement>;
  onKeyDown?: (e: React.KeyboardEvent<HTMLInputElement>) => void;
  onBlur?: () => void;
}

/** The app's one text field.
 *
 * Eleven surfaces had answered this privately, and they disagreed on almost
 * everything: five paddings, four backgrounds, four border colours, one raw
 * `2px` radius where the rest used the token, and two different ideas of what
 * focus looks like — an accent outline in Identity, an accent border
 * everywhere else. The terminal's reached for `--ink-100` rather than
 * `--text-primary`, which is a different token family for the same ink.
 *
 * A caller may still pass `className` for its WIDTH — the identity name field
 * caps at 320px, the terminal search sits at 180 — because where a field goes
 * belongs to the surface. What it looks like does not.
 */
export function Input({
  value,
  onChange,
  type = "text",
  placeholder,
  disabled = false,
  className,
  id,
  ariaLabel,
  maxLength,
  min,
  max,
  step,
  autoFocus,
  spellCheck,
  autoComplete,
  ariaInvalid,
  testId,
  inputRef,
  onKeyDown,
  onBlur,
}: InputProps) {
  return (
    <input
      ref={inputRef}
      id={id}
      type={type}
      className={className ? `input ${className}` : "input"}
      value={value}
      placeholder={placeholder}
      disabled={disabled}
      aria-label={ariaLabel}
      maxLength={maxLength}
      min={min}
      max={max}
      step={step}
      autoFocus={autoFocus}
      spellCheck={spellCheck}
      autoComplete={autoComplete}
      aria-invalid={ariaInvalid}
      data-testid={testId}
      onKeyDown={onKeyDown}
      onBlur={onBlur}
      onChange={(e) => onChange(e.target.value)}
    />
  );
}

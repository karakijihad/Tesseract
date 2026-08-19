import type { ReactNode } from "react";

export type NoteTone = "info" | "warn" | "bad";

interface NoteProps {
  children: ReactNode;
  /** `info` explains, `warn` cautions, `bad` reports a failure. */
  tone?: NoteTone;
  /** PLACEMENT ONLY — where the aside sits in its parent's layout
   *  (`flex-shrink: 0`, a margin). What it LOOKS like belongs here. */
  className?: string;
}

const GLYPH: Record<NoteTone, string> = {
  info: "!",
  warn: "!",
  bad: "×",
};

/** The app's one aside — an explanation, a caution, or a failure.
 *
 * Hint text used to be a bare `.t-meta` div: 10px uppercase prose with nothing
 * marking it as an aside, so a paragraph of explanation read as part of the
 * control it sat under. A marker and a rule give it somewhere to begin and
 * end, and sentence case gives it back its punctuation — uppercase was
 * swallowing the difference between a note and a label.
 */
export function Note({ children, tone = "info", className }: NoteProps) {
  return (
    <div
      className={`note note--${tone}${className ? ` ${className}` : ""}`}
      role={tone === "info" ? undefined : "status"}
    >
      <span className="note__marker" aria-hidden="true">
        {GLYPH[tone]}
      </span>
      <div className="note__body">{children}</div>
    </div>
  );
}

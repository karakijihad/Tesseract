// A proportion bar: a track, and either one fill or several laid end to end.
//
// It lived inside `ToolUsageChart` until the playbook gauge beside it needed
// the same bar, which is the moment a convention has to become a component.
// The caller owns the number and the layout around it; this owns how a bar
// looks, so the two charts cannot answer that differently.
//
// Segments were added for the day panel, where one bar has to carry how a
// tool's calls went rather than how many there were: mostly clean, two the
// model got wrong, one nobody can check. A second stacked-bar component would
// have been a second answer to how wide, how tall and how rounded a bar is,
// which is the whole reason this file exists.

import './Meter.css';

/** What a fill MEANS, never which colour it is. The tones are the app's
 *  semantic set and a surface picks by meaning; `Meter.css` decides the
 *  paint, and Appearance can move all of them at once because every one
 *  resolves through a token. */
export type MeterTone = 'ok' | 'bad' | 'warn' | 'info' | 'unverified' | 'quiet';

export interface MeterSegment {
  /** How much, in whatever the caller is counting. Shares are worked out
   *  here so two segments cannot be scaled against different totals. */
  value: number;
  tone: MeterTone;
  /** What this part is, in words. Not decoration: a bar is the one reading a
   *  screen reader cannot take, so the segments compose the bar's own
   *  accessible summary. */
  label: string;
}

export function Meter({
  /** How full, 0 to 1. Values outside that are clamped rather than refused:
   *  a chart is not the place to fail on arithmetic, and a bar past its own
   *  track reads as a bug in the panel rather than in the number. */
  value,
  /** Paints the single fill as trouble rather than as volume. */
  tone,
  /** Several fills end to end, sized by share of their own total. When this
   *  is given, `value` is ignored: a bar cannot be both one reading and
   *  several. */
  segments,
  /** Names the whole bar for assistive tech. With `segments` the parts are
   *  appended, so one string reads as "how file_read went: 85 worked, 2
   *  failed". */
  ariaLabel,
}: {
  value?: number;
  tone?: 'bad';
  segments?: readonly MeterSegment[];
  ariaLabel?: string;
}) {
  if (segments) {
    const held = segments.filter((s) => s.value > 0);
    const total = held.reduce((sum, s) => sum + s.value, 0);
    const said = held.map((s) => `${s.value} ${s.label}`).join(', ');
    return (
      <span
        className="meter"
        role="img"
        aria-label={ariaLabel ? `${ariaLabel}: ${said}` : said}
      >
        {held.map((seg, i) => (
          <span
            key={`${seg.tone}-${i}`}
            className={`meter__fill meter__fill--${seg.tone}`}
            style={{ width: `${(seg.value / (total || 1)) * 100}%` }}
          />
        ))}
      </span>
    );
  }
  const ratio = Number.isFinite(value) ? Math.min(1, Math.max(0, value ?? 0)) : 0;
  return (
    <span className="meter" aria-label={ariaLabel}>
      <span
        className={`meter__fill${tone === 'bad' ? ' meter__fill--bad' : ''}`}
        style={{ width: `${ratio * 100}%` }}
      />
    </span>
  );
}

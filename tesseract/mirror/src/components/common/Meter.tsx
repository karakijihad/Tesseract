// A proportion bar: a track, and a fill across some part of it.
//
// It lived inside `ToolUsageChart` until the playbook gauge beside it needed
// the same bar, which is the moment a convention has to become a component.
// The caller owns the number and the layout around it; this owns how a bar
// looks, so the two charts cannot answer that differently.

import './Meter.css';

export function Meter({
  /** How full, 0 to 1. Values outside that are clamped rather than refused:
   *  a chart is not the place to fail on arithmetic, and a bar past its own
   *  track reads as a bug in the panel rather than in the number. */
  value,
  /** Paints the fill as trouble rather than as volume. */
  tone,
}: {
  value: number;
  tone?: 'bad';
}) {
  const ratio = Number.isFinite(value) ? Math.min(1, Math.max(0, value)) : 0;
  return (
    <span className="meter">
      <span
        className={`meter__fill${tone === 'bad' ? ' meter__fill--bad' : ''}`}
        style={{ width: `${ratio * 100}%` }}
      />
    </span>
  );
}

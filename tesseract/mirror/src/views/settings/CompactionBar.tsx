import { Range } from "../../components/common/Range";
import { measureBar, type CompactionFacts } from "../../lib/compaction";

interface Props {
  facts: CompactionFacts;
  ratio: number;
  disabled: boolean;
  onRatio: (next: number) => void;
  onRatioCommit: () => void;
}

/** For reading. Whole percents, because nobody wants three decimals in a
 *  sentence. */
const pct = (n: number) => `${Math.round(n * 100)}%`;

/** For drawing, which is a different job. A band rounded to a whole percent
 *  disappears when it is worth less than half of one, so any band that exists
 *  gets at least a visible sliver. */
const band = (n: number) => ({
  width: `${(n * 100).toFixed(3)}%`,
  minWidth: n > 0 ? "var(--space-3)" : undefined,
});

/** A figure the reader can compare, or the app's no-value mark. */
const tok = (n: number | null | undefined) =>
  n == null ? "—" : Math.round(n).toLocaleString("en-US");

/** One bar for the whole setting.
 *
 *  The instructions are hatched, because nothing can shrink them. The part the
 *  operator is choosing is lit, and the slider under it cannot go left of
 *  where the hatching ends. Everything past the fill is window the
 *  conversation is wrapped up before reaching.
 *
 *  There used to be three hatched bands and a summary that ate the middle of
 *  the conversation. There is no summary: the boundary clears what was said,
 *  writes down where the work had got to, and hands that to a fresh context.
 *
 *  The bands come from `measureBar`, which starts from what the runtime
 *  measured. Nothing here recomputes a floor of its own. */
export function CompactionBar({
  facts,
  ratio,
  disabled,
  onRatio,
  onRatioCommit,
}: Props) {
  const bar = measureBar(facts, ratio);

  return (
    <div className="compact-bar">
      <div className="compact-bar__head">
        <span className="compact-bar__label">
          Wrap this conversation up when it reaches
        </span>
        <span className="compact-bar__split">
          <span className="compact-bar__stat t-label">
            <b>{pct(bar.systemRatio)}</b> reserved
          </span>
          <span className="compact-bar__stat t-label is-yours">
            <b>{pct(bar.yoursRatio)}</b> yours
          </span>
        </span>
      </div>

      <div
        className={`compact-bar__track${bar.measured ? "" : " is-unmeasured"}`}
      >
        <span
          className="compact-bar__seg compact-bar__seg--system"
          style={band(bar.systemRatio)}
        />
        <span
          className="compact-bar__seg compact-bar__seg--yours"
          style={band(bar.yoursRatio)}
        />
        {bar.defaultRatio != null && (
          <span
            className="compact-bar__mark"
            style={{ left: pct(bar.defaultRatio) }}
            aria-hidden="true"
          />
        )}
      </div>

      <Range
        min={bar.minRatio}
        max={bar.maxRatio}
        step={0.01}
        value={ratio}
        onChange={onRatio}
        onCommit={onRatioCommit}
        disabled={disabled}
        ariaLabel="wrap this conversation up when it reaches this share of the window"
      />

      <p className={`compact-bar__verdict is-${bar.verdict.tone}`}>
        {bar.verdict.text}
      </p>

      <ul className="compact-bar__legend">
        <li className="compact-bar__key">
          <span className="compact-bar__swatch compact-bar__swatch--system" />
          <span className="compact-bar__key-name">System instructions</span>
          <span className="compact-bar__key-value">
            {tok(facts.systemTokens)}
          </span>
          <span className="compact-bar__key-why">
            Sent on every turn. Nothing can shrink it.
          </span>
        </li>
        <li className="compact-bar__key">
          <span className="compact-bar__swatch compact-bar__swatch--yours" />
          <span className="compact-bar__key-name">Room to talk</span>
          <span className="compact-bar__key-value">{tok(bar.roomTokens)}</span>
          <span className="compact-bar__key-why">
            What is left for the conversation before it is wrapped up and a
            fresh one carries the work on.
          </span>
        </li>
        {bar.defaultRatio != null && (
          <li className="compact-bar__key">
            <span className="compact-bar__swatch compact-bar__swatch--mark" />
            <span className="compact-bar__key-name">Where a fresh install starts</span>
            <span className="compact-bar__key-value">
              {pct(bar.defaultRatio)}
            </span>
            <span className="compact-bar__key-why">
              The dashed mark on the bar. Reset puts you back here.
            </span>
          </li>
        )}
      </ul>

      {!bar.measured && (
        <p className="compact-bar__verdict t-meta">
          The reserved part appears once a conversation has run a turn. Until
          then the instructions have not been measured.
        </p>
      )}
    </div>
  );
}

import { Input } from "../../components/common/Input";
import { Range } from "../../components/common/Range";
import { measureBar, type CompactionFacts } from "../../lib/compaction";

interface Props {
  facts: CompactionFacts;
  ratio: number;
  turns: string;
  turnsMin: number;
  turnsMax: number;
  disabled: boolean;
  onRatio: (next: number) => void;
  onRatioCommit: () => void;
  onTurns: (next: string) => void;
  onTurnsCommit: () => void;
}

/** For reading. Whole percents, because nobody wants three decimals in a
 *  sentence. */
const pct = (n: number) => `${Math.round(n * 100)}%`;

/** For drawing, which is a different job. A band rounded to a whole percent
 *  disappears when it is worth less than half of one: breathing room against a
 *  1,050,000 window is routinely 0.4%, which rendered as nothing at all while
 *  the legend beside it named four thousand tokens. Any band that exists gets
 *  at least a visible sliver. */
const band = (n: number) => ({
  width: `${(n * 100).toFixed(3)}%`,
  minWidth: n > 0 ? "var(--space-3)" : undefined,
});

/** A figure the reader can compare, or the app's no-value mark. */
const tok = (n: number | null | undefined) =>
  n == null ? "—" : Math.round(n).toLocaleString("en-US");

/** One bar for the whole setting.
 *
 *  The parts a summary cannot touch are hatched, the part the operator is
 *  choosing is lit. Striped is spoken for, solid is yours, and the slider
 *  under it cannot go left of where the stripes end. Everything past the fill
 *  is window the conversation is summarised before reaching.
 *
 *  Each hatched band has its own colour rather than its own shade of grey:
 *  the reading is "which of these is eating my window", and two greys behind
 *  the same hatch could not answer it.
 *
 *  The bands come from `measureBar`, which starts from what the runtime
 *  measured. Nothing here recomputes a floor of its own. */
export function CompactionBar({
  facts,
  ratio,
  turns,
  turnsMin,
  turnsMax,
  disabled,
  onRatio,
  onRatioCommit,
  onTurns,
  onTurnsCommit,
}: Props) {
  const bar = measureBar(facts, ratio, parseInt(turns, 10) || 1);
  const reserved = bar.systemRatio + bar.lockedRatio;

  return (
    <div className="compact-bar">
      <div className="compact-bar__head">
        <span className="compact-bar__label">
          Compact when the conversation reaches
        </span>
        <span className="compact-bar__split">
          <span className="compact-bar__stat t-label">
            <b>{pct(reserved)}</b> reserved
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
          className="compact-bar__seg compact-bar__seg--kept"
          style={band(bar.keptRatio)}
        />
        <span
          className="compact-bar__seg compact-bar__seg--margin"
          style={band(bar.marginRatio)}
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
        ariaLabel="compact when the conversation reaches this share of the window"
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
            Sent on every turn. Summarising cannot shrink it.
          </span>
        </li>
        <li className="compact-bar__key">
          <span className="compact-bar__swatch compact-bar__swatch--kept" />
          <span className="compact-bar__key-name">Kept word for word</span>
          <span className="compact-bar__key-value">
            {bar.measured ? tok(bar.keptTokens) : tok(null)}
          </span>
          <span className="compact-bar__key-why">
            How the conversation opened, plus your last {turns} turns. These
            survive every summary intact.
          </span>
        </li>
        <li className="compact-bar__key">
          <span className="compact-bar__swatch compact-bar__swatch--margin" />
          <span className="compact-bar__key-name">Breathing room</span>
          <span className="compact-bar__key-value">
            {bar.measured ? tok(bar.lockedTokens - bar.keptTokens) : tok(null)}
          </span>
          <span className="compact-bar__key-why">
            Held back so one summary lasts a while. Without it the next turn
            would summarise again. Keep fewer turns and this shrinks too.
          </span>
        </li>
        <li className="compact-bar__key">
          <span className="compact-bar__swatch compact-bar__swatch--yours" />
          <span className="compact-bar__key-name">Room to talk</span>
          <span className="compact-bar__key-value">{tok(bar.roomTokens)}</span>
          <span className="compact-bar__key-why">
            What is left for the conversation before the next summary.
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

      <div className="compact-bar__turns">
        <span className="compact-bar__label">Turns kept word for word</span>
        <Input
          type="number"
          min={turnsMin}
          max={turnsMax}
          step={1}
          value={turns}
          onChange={onTurns}
          onBlur={onTurnsCommit}
          onKeyDown={(e) => {
            if (e.key === "Enter") (e.target as HTMLInputElement).blur();
          }}
          disabled={disabled}
          className="compact-bar__turns-input"
          ariaLabel="turns kept word for word"
        />
        <span className="t-meta">
          A turn is one thing you said and everything that followed before you
          said the next thing, so its tool calls come with it. Fewer are kept
          when they do not fit.
        </span>
      </div>

      {!bar.measured && (
        <p className="compact-bar__verdict t-meta">
          The reserved parts appear once a conversation has run a turn. Until
          then there is no head anchor and no tail to measure.
        </p>
      )}
    </div>
  );
}

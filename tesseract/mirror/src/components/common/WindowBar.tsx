import type { ReactNode } from "react";

import { measureBar, type CompactionFacts } from "../../lib/compaction";
import "./WindowBar.css";

interface Props {
  facts: CompactionFacts;
  ratio: number;
  /** A control laid between the track and the verdict. Settings puts its
   *  slider here; a read-only reading (the HUD) passes nothing. */
  children?: ReactNode;
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

/** How much of the window is reserved, how much is the conversation's, and
 *  where the conversation sits right now: one reading of the boundary,
 *  drawn once and read by Settings and the HUD alike.
 *
 *  The instructions are hatched, because nothing can shrink them. The part
 *  the operator is choosing (or the conversation is spending) is lit, and
 *  the dark remainder is window the conversation folds before it reaches.
 *
 *  Everything here comes from `measureBar`, which starts from what the
 *  runtime measured. Nothing here recomputes a floor of its own.
 *
 *  This is the display half only: Settings wraps its slider around it as
 *  `children`, and a live reading (the HUD) passes none. */
export function WindowBar({ facts, ratio, children }: Props) {
  const bar = measureBar(facts, ratio);
  const reservedLabel = facts.toolsTokens ? "Instructions and tools" : "System instructions";
  const reservedTokens = (facts.systemTokens ?? 0) + (facts.toolsTokens ?? 0);

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
        {bar.usedRatio != null && (
          <span
            className="compact-bar__mark compact-bar__mark--live"
            style={{ left: pct(bar.usedRatio) }}
            aria-hidden="true"
          />
        )}
      </div>

      {children}

      <p className={`compact-bar__verdict is-${bar.verdict.tone}`}>
        {bar.verdict.text}
      </p>

      <ul className="compact-bar__legend">
        <li className="compact-bar__key">
          <span className="compact-bar__swatch compact-bar__swatch--system" />
          <span className="compact-bar__key-name">{reservedLabel}</span>
          <span className="compact-bar__key-value">{tok(reservedTokens)}</span>
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
        {bar.usedRatio != null && (
          <li className="compact-bar__key">
            <span className="compact-bar__swatch compact-bar__swatch--live" />
            <span className="compact-bar__key-name">This conversation now</span>
            <span className="compact-bar__key-value">
              {tok(facts.conversationTokens)}
            </span>
            <span className="compact-bar__key-why">
              How much has been said so far, measured after the last turn.
            </span>
          </li>
        )}
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

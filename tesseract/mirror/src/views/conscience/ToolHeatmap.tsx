import { useMemo, useState, type CSSProperties } from 'react';

import { Segmented } from '../../components/common/Segmented';
import type { ToolHeatmap as ToolHeatmapData } from '../../stores/conscience';

const WINDOWS = [7, 30, 90];

/** Every tool call as a tool-by-day grid.
 *
 * The window totals next door cannot show that a tool was used hard for three
 * days and then never again, which is the shape that says a habit changed and
 * switching it off is safe. A day it was leaned on is one cell, so raw calls
 * here rather than the session ranking the totals use.
 *
 * Only the tools with a call in the window are drawn. The ones with none are
 * counted underneath instead: a hundred empty rows push the ten that say
 * something off the screen.
 *
 * **Every number here is readable three ways**, because a shade is the one
 * way that is not: a total on each row, a sentence when a row is focused or a
 * square is hovered, and the whole grid as a table for a screen reader.
 */
export function ToolHeatmap({
  data,
  days,
  loading,
  onWindow,
}: {
  data: ToolHeatmapData;
  days: number;
  loading: boolean;
  onWindow: (days: number) => void;
}) {
  const [reading, setReading] = useState<string | null>(null);

  const { used, quiet, max } = useMemo(() => {
    const totals = data.tools.map((t) => ({
      ...t,
      total: t.counts.reduce((a, b) => a + b, 0),
    }));
    return {
      used: totals.filter((t) => t.total > 0),
      quiet: totals.filter((t) => t.total === 0).length,
      max: Math.max(1, ...totals.flatMap((t) => t.counts)),
    };
  }, [data]);

  const dates = data.dates;
  const middle = dates[Math.floor(dates.length / 2)];

  return (
    <figure className="tool-heatmap">
      <div className="tool-heatmap__head">
        <p className="tool-heatmap__count">
          <strong>{used.length}</strong>{' '}
          {used.length === 1 ? 'tool' : 'tools'} called in the last {days} days
        </p>
        <Segmented
          label="Heatmap window"
          value={days}
          onSelect={onWindow}
          items={WINDOWS.map((w) => ({
            key: w,
            label: `${w}d`,
            hint: `One column per day, over the last ${w} days`,
            disabled: loading,
          }))}
        />
      </div>

      {used.length === 0 ? (
        <p className="t-meta">
          No tool was called in this window. The ledger writes one row per call,
          so this fills in as the assistant works.
        </p>
      ) : (
        <>
          <Scale max={max} />

          {/* The readout sits at the TOP, beside the scale that explains the
              shade. A caption under a grid of thirty rows is below the fold
              exactly when a pointer is in the grid, which is the one moment
              it has something to say. */}
          <p className="tool-heatmap__readout t-meta" aria-live="polite">
            {reading ?? 'Hover a square, or tab to a row, to read it.'}
          </p>

          <div
            className="tool-heatmap__grid"
            onMouseLeave={() => setReading(null)}
          >
            {used.map((row) => (
              <div
                key={row.tool}
                className="tool-heatmap__row"
                // Focusable by row, not by square. The reading a person needs
                // without a pointer is "how much, and when was the peak",
                // and a window of ninety days would otherwise put ninety tab
                // stops in front of them for one tool.
                tabIndex={0}
                aria-label={describeRow(row.tool, row.counts, dates, row.total, days)}
                onFocus={() =>
                  setReading(
                    describeRow(row.tool, row.counts, dates, row.total, days),
                  )
                }
                onBlur={() => setReading(null)}
              >
                <span className="tool-heatmap__name">{row.tool}</span>
                <span
                  className="tool-heatmap__cells"
                  aria-hidden="true"
                  style={{ '--heatmap-columns': dates.length } as CSSProperties}
                >
                  {row.counts.map((count, i) => (
                    <span
                      key={dates[i]}
                      className="tool-heatmap__cell"
                      style={
                        { '--heatmap-fill': intensity(count, max) } as CSSProperties
                      }
                      onMouseEnter={() =>
                        setReading(
                          `${row.tool} · ${dates[i]} · ${count} ${count === 1 ? 'call' : 'calls'}`,
                        )
                      }
                    />
                  ))}
                </span>
                <span className="tool-heatmap__total t-meta" aria-hidden="true">
                  {row.total}
                </span>
              </div>
            ))}
          </div>

          {/* Three labels rather than one per column: at ninety days every
              third date would overlap its neighbour, and the caption says
              which day a square is. */}
          <div className="tool-heatmap__axis t-meta" aria-hidden="true">
            <span />
            <span className="tool-heatmap__dates">
              <span>{dates[0]}</span>
              <span>{middle}</span>
              <span>{dates[dates.length - 1]}</span>
            </span>
            <span />
          </div>
        </>
      )}

      {quiet > 0 && (
        <figcaption className="tool-heatmap__caption t-meta">
          {quiet} more {quiet === 1 ? 'tool was' : 'tools were'} never called in
          this window, so they are not drawn.
        </figcaption>
      )}

      {/* The same figures as text. A shade is never the only way to read a
          chart, and a screen reader gets numbers rather than a wall of
          squares. */}
      <table className="visually-hidden">
        <caption>Tool calls per day over the last {days} days</caption>
        <thead>
          <tr>
            <th scope="col">Tool</th>
            {dates.map((date) => (
              <th key={date} scope="col">
                {date}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {used.map((row) => (
            <tr key={row.tool}>
              <th scope="row">{row.tool}</th>
              {row.counts.map((count, i) => (
                <td key={dates[i]}>{count}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </figure>
  );
}

/** What each shade is worth, in calls.
 *
 * Five steps rather than a smooth bar, because the scale is logarithmic and a
 * smooth gradient would read as linear: the eye would take the middle of the
 * bar for half the busiest day, when it is closer to its square root. Each
 * step carries the count it stands for, so the legend is the scale rather
 * than a decoration of it.
 */
function Scale({ max }: { max: number }) {
  const steps = useMemo(() => {
    const seen = new Set<number>();
    // Even in log space, which is the space the cells are shaded in.
    return [0.2, 0.4, 0.6, 0.8, 1]
      .map((at) => Math.max(1, Math.round(max ** at)))
      .filter((count) => !seen.has(count) && seen.add(count));
  }, [max]);

  return (
    <div className="tool-heatmap__scale t-meta">
      <span>fewer</span>
      <span className="tool-heatmap__swatches">
        {steps.map((count) => (
          <span key={count} className="tool-heatmap__swatch-step">
            <span
              className="tool-heatmap__cell"
              style={{ '--heatmap-fill': intensity(count, max) } as CSSProperties}
            />
            <span>{count}</span>
          </span>
        ))}
      </span>
      <span>calls in one day</span>
    </div>
  );
}

/** One row as a sentence: how much, over how long, and when the peak was.
 *
 * The peak is the half a total cannot carry. Two tools with sixty calls each
 * are the same row until one of them turns out to have spent them all on one
 * afternoon three weeks ago.
 */
function describeRow(
  tool: string,
  counts: number[],
  dates: string[],
  total: number,
  days: number,
): string {
  const peak = counts.reduce((best, n, i) => (n > counts[best] ? i : best), 0);
  return `${tool} · ${total} ${total === 1 ? 'call' : 'calls'} over ${days} days · busiest ${dates[peak]} with ${counts[peak]}`;
}

/** How dark one cell is, 0 to 1.
 *
 * Logarithmic, not linear: one loop calling a tool four hundred times in an
 * afternoon would otherwise flatten every other day in the grid to the same
 * near-empty shade, and the busy day is not the reading this panel is for.
 * A single call lands at a floor rather than at nearly nothing, so "once" and
 * "never" are different colours.
 */
function intensity(count: number, max: number): number {
  if (count <= 0) return 0;
  return 0.2 + 0.8 * (Math.log(count + 1) / Math.log(max + 1));
}

// Two weeks of one number, in Health.
//
// A room that shows only the current value cannot answer "is this getting
// worse", and that is usually the question. These are the three numbers Health
// is asked about, each over a fortnight.
//
// **One plot at a time.** Three small charts side by side is a wall; a tab
// gives each one room and a sentence saying what it is for and what its shape
// means. Single series each, so the title names it and no legend is needed,
// and the value under the pointer goes to the readout rather than a number
// sitting on every bar.
//
// **This file writes nothing about the runtime.** Every headline, unit and
// paragraph is the payload's. What it owns is the drawing: the bars, the hit
// targets over them, and the readout they feed.

import { useState } from 'react';
import { Note } from '../../components/common/Note';
import { Band } from '../../components/common/StateStrip';
import { Tabs } from '../../components/common/Tabs';
import type { HistorySeries } from '../../lib/api';

/** The drawing box. A viewBox rather than pixels, so the bars stretch to
 *  whatever width the room has and the geometry below stays arithmetic. */
const W = 200;
const H = 74;
/** Surface between two bars, and the floor a zero still draws, so an empty day
 *  reads as a measured zero rather than as a gap in the record. */
const GAP = 2;
const ZERO_H = 2;

function bars(values: number[], tone: string) {
  const max = Math.max(...values, 1);
  const width = (W - GAP * (values.length - 1)) / values.length;
  const cell = width + GAP;
  return values.map((v, i) => {
    const height = v === 0 ? ZERO_H : Math.max(3, (v / max) * (H - 6));
    return {
      v,
      i,
      x: i * cell,
      y: H - height,
      width,
      height,
      // The whole column, gap included, and the last takes the remainder so
      // the fourteen tile the chart exactly. This is what the READOUT follows,
      // so every day is reachable however short its bar: the smallest bar on
      // the errors chart is under two pixels tall and six of fourteen are
      // zero. It carries no cursor, so the chart does not read as one large
      // clickable surface.
      colX: i * cell,
      colWidth: i === values.length - 1 ? W - i * cell : cell,
      // A zero is furniture whatever the series means, so it never takes the
      // series' colour. Everything else does, and the tone is the backend's.
      cls: v === 0 ? 'plot__bar plot__bar--zero' : `plot__bar plot__bar--${tone}`,
    };
  });
}

function OnePlot({ series }: { series: HistorySeries }): React.ReactElement {
  const [under, setUnder] = useState<number | null>(null);

  if (series.state === 'not_instrumented') {
    // The phase's rule, applied to a chart. A flat line at zero and "nothing
    // was written down" look identical, and only one of them is good news.
    // The sentence is the backend's, like every other one here.
    return <Note>{series.cannotSay}</Note>;
  }

  const last = series.values[series.values.length - 1];
  const shown = under === null ? last : series.values[under];
  const when =
    under === null
      ? 'today'
      : `on ${series.days[under]}`;

  return (
    <div className="plot">
      <div className="plot__head">
        <span className="plot__t">{series.headline}</span>
        <span className="plot__v t-meta">{`${shown.toLocaleString()}${series.unit} ${when}`}</span>
      </div>

      <svg
        viewBox={`0 0 ${W} ${H}`}
        preserveAspectRatio="none"
        role="img"
        aria-label={`${series.headline}, ${series.values.length} days, most recent on the right`}
        onMouseLeave={() => setUnder(null)}
      >
        {bars(series.values, series.tone).map((bar) => (
          // Two rects, and which is in FRONT is the whole design. The column
          // is behind and carries no cursor, so the readout answers anywhere
          // in the day's column and the chart still does not read as one
          // clickable surface. The bar is in front and carries the pointer,
          // so the cursor changes at the edge of its own box and nowhere
          // else. Both name the same day, so which one the pointer lands on
          // does not matter.
          <g key={bar.i}>
            <rect
              className="plot__col"
              x={bar.colX}
              y={0}
              width={bar.colWidth}
              height={H}
              onMouseEnter={() => setUnder(bar.i)}
            />
            <rect
              className={bar.cls}
              x={bar.x}
              y={bar.y}
              width={bar.width}
              height={bar.height}
              rx={Math.min(2, bar.width / 2)}
              onMouseEnter={() => setUnder(bar.i)}
            />
          </g>
        ))}
        <line className="plot__base" x1={0} y1={H} x2={W} y2={H} />
      </svg>

      <div className="plot__foot t-meta">
        <span>{`${series.values.length} days ago`}</span>
        <span>today</span>
      </div>

      {/* Every value, for a reader who cannot point at a bar. The chart is one
          series and the hover reaches one value at a time, so this is the only
          way the whole shape is available without a mouse. */}
      <table className="visually-hidden">
        <caption>{series.headline}</caption>
        <tbody>
          {series.values.map((v, i) => (
            <tr key={series.days[i]}>
              <th scope="row">{series.days[i]}</th>
              <td>{`${v}${series.unit}`}</td>
            </tr>
          ))}
        </tbody>
      </table>

      <p className="plot__why t-meta">{series.why}</p>
      {/* The left of the chart is not a quiet fortnight, it is the end of the
          record. Saying so is the difference between a chart and a claim, and
          the backend is what says it: composing this out of `reaches` and a
          length here would be the surface describing the record again. */}
      {series.shortSay && <p className="plot__why t-meta">{series.shortSay}</p>}
    </div>
  );
}

export function HistoryPlots({
  series,
  label,
}: {
  series: HistorySeries[];
  /** The band over the tabs: the payload's group title. Required, because a
   *  default written here would be this file's own words about the runtime. */
  label: string;
}): React.ReactElement {
  const [open, setOpen] = useState(0);
  if (series.length === 0) return <></>;
  const showing = series[Math.min(open, series.length - 1)];

  return (
    <div className="autonomy-group">
      <Band label={label} count={series.length} />
      <Tabs
        items={series.map((s, i) => ({ key: String(i), label: s.title }))}
        active={String(Math.min(open, series.length - 1))}
        onSelect={(key) => setOpen(Number(key))}
        label="Which history to show"
      />
      <OnePlot key={showing.key} series={showing} />
    </div>
  );
}

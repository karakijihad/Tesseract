import { useMemo, useState } from 'react';

import type { DriftReport } from '../../stores/conscience';

const STATUS = [
  { key: 'bad', label: 'Bad' },
  { key: 'warn', label: 'Warn' },
  { key: 'ok', label: 'Ok' },
] as const;

function shortTime(ts: string): string {
  try {
    const d = new Date(ts);
    return d.toLocaleDateString([], { month: 'short', day: 'numeric' });
  } catch {
    return '';
  }
}

/** Signal counts per scrape, oldest to newest.
 *
 * A stacked column per report rather than a line per status: the quantity is a
 * small integer count and the three parts sum to "how many signals were
 * checked", which a stack says and three lines do not. Status colour is the
 * app's reserved ok/warn/bad — never a series palette — and it is never the
 * only encoding: every column carries its numbers in the hover, and the
 * table below it is the same data in text.
 */
export function DriftHistoryChart({ history }: { history: DriftReport[] }) {
  // Oldest → newest reads left to right, which is the only direction a time
  // axis is allowed to run.
  const rows = useMemo(
    () =>
      [...history]
        .sort((a, b) => a.timestamp.localeCompare(b.timestamp))
        .map((h) => ({
          ts: h.timestamp,
          ok: h.summary.ok,
          warn: h.summary.warn,
          bad: h.summary.bad,
          total: h.summary.ok + h.summary.warn + h.summary.bad,
        })),
    [history],
  );

  const max = Math.max(1, ...rows.map((r) => r.total));
  const [hovered, setHovered] = useState<number | null>(null);
  const read = hovered !== null ? rows[hovered] : null;

  return (
    <figure className="drift-chart">
      <div className="drift-chart__plot" role="img" aria-label={`Signal counts across the last ${rows.length} scrapes`}>
        {/* Two recessive gridlines — the ceiling and its half. More would be
            chrome competing with three-unit columns. */}
        <span className="drift-chart__grid drift-chart__grid--top" aria-hidden="true" />
        <span className="drift-chart__grid drift-chart__grid--mid" aria-hidden="true" />
        <span className="drift-chart__tick t-meta" aria-hidden="true">
          {max}
        </span>

        <div className="drift-chart__cols">
          {rows.map((r, i) => (
            <div
              key={`${r.ts}-${i}`}
              className={`drift-chart__col${hovered === i ? ' is-read' : ''}`}
              tabIndex={0}
              aria-label={`${new Date(r.ts).toLocaleString()} — ${r.ok} ok, ${r.warn} warn, ${r.bad} bad`}
              onMouseEnter={() => setHovered(i)}
              onMouseLeave={() => setHovered((h) => (h === i ? null : h))}
              onFocus={() => setHovered(i)}
              onBlur={() => setHovered((h) => (h === i ? null : h))}
            >
              {STATUS.map(({ key }) => {
                const value = r[key];
                if (value === 0) return null;
                return (
                  <span
                    key={key}
                    className={`drift-chart__seg drift-chart__seg--${key}`}
                    style={{ height: `${(value / max) * 100}%` }}
                  />
                );
              })}
            </div>
          ))}
        </div>
      </div>

      <div className="drift-chart__axis t-meta" aria-hidden="true">
        <span>{rows.length ? shortTime(rows[0].ts) : ''}</span>
        <span>{rows.length ? shortTime(rows[rows.length - 1].ts) : ''}</span>
      </div>

      {/* The readout takes the legend's place while a column is hovered — a
          popover over a 120px plot covers the axis it is explaining, and one
          outside the card explains nothing at all. */}
      <figcaption className="drift-chart__legend" aria-live="polite">
        {read ? (
          <>
            <span className="drift-chart__read t-meta">
              {new Date(read.ts).toLocaleString()}
            </span>
            {STATUS.map(({ key, label }) => (
              <span key={key} className="drift-chart__key t-meta">
                <span
                  className={`drift-chart__dot drift-chart__seg--${key}`}
                  aria-hidden="true"
                />
                {label} {read[key]}
              </span>
            ))}
          </>
        ) : (
          STATUS.map(({ key, label }) => (
            <span key={key} className="drift-chart__key t-meta">
              <span
                className={`drift-chart__dot drift-chart__seg--${key}`}
                aria-hidden="true"
              />
              {label}
            </span>
          ))
        )}
      </figcaption>

      {/* The same numbers as text — colour is never the only way to read a
          chart, and a screen reader gets the figures rather than a shape. */}
      <table className="visually-hidden">
        <caption>Drift signals per scrape</caption>
        <thead>
          <tr>
            <th scope="col">Scrape</th>
            <th scope="col">Ok</th>
            <th scope="col">Warn</th>
            <th scope="col">Bad</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={`${r.ts}-row-${i}`}>
              <th scope="row">{new Date(r.ts).toLocaleString()}</th>
              <td>{r.ok}</td>
              <td>{r.warn}</td>
              <td>{r.bad}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </figure>
  );
}

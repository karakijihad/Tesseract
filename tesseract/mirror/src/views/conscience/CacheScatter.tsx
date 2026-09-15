import { useMemo, useState } from 'react';

import { Segmented } from '../../components/common/Segmented';
import type { CacheCall } from '../../stores/conscience';

/** Every model call in the window, one dot per call, plotted by when it
 *  happened and how much of its input came back from cache.
 *
 * **A call, not a turn.** `CacheReport`'s own lists are turn averages, and a
 * single cold-start call sitting inside an otherwise hot turn is averaged
 * away and never seen. A gap in the day where the runtime sat idle, then a
 * hollow run of dots when it started back up, is the shape this chart exists
 * to show and a turn-level reading cannot.
 *
 * **Hollow is its own thing, not a zero.** `hit_rate: null` means the
 * provider reported no cached count at all, which is a different fact from a
 * reported miss. Drawing it at 0% would be exactly the confusion the whole
 * cache instrument is built to avoid, so those calls sit in their own lane
 * above the percentage scale, hollow rather than filled.
 *
 * **Every call carries a role, and one filter, not a second colour.** Band
 * colour and the hollow mark already do the work a shape or a third palette
 * would only compete with. Whether a dot answered you or ran on its own is a
 * single yes/no, so it gets the control built for exactly one setting at a
 * time: which calls are shown, never a second thing to decode on the dot
 * itself.
 */

const W = 320;
const H = 130;
const PAD_LEFT = 24;
const PAD_RIGHT = 8;
const PLOT_TOP = 34;
const PLOT_BOTTOM = H - 14;
const PLOT_H = PLOT_BOTTOM - PLOT_TOP;
const UNREPORTED_Y = 14;
const DIVIDER_Y = 26;

/** The one role that answers the operator directly. Every other role ran on
 *  its own, outside any conversation. */
const CHAT_ROLE = 'chat_brain';

type Band = 'hot' | 'warm' | 'cold' | 'unreported';
type RoleFilter = 'all' | 'chat' | 'background';

const ROLE_FILTERS: { key: RoleFilter; label: string; hint: string }[] = [
  { key: 'all', label: 'All calls', hint: 'Every model call in this window' },
  { key: 'chat', label: 'Chat', hint: 'Only calls that answered you directly' },
  {
    key: 'background',
    label: 'Background',
    hint: 'Only calls the runtime made on its own, outside any conversation',
  },
];

function bandOf(call: CacheCall): Band {
  if (call.hit_rate === null) return 'unreported';
  const pct = call.hit_rate * 100;
  if (pct >= 50) return 'hot';
  if (pct > 0) return 'warm';
  return 'cold';
}

function formatWhen(ts: string): string {
  const parsed = new Date(ts);
  if (Number.isNaN(parsed.getTime())) return ts;
  return parsed.toLocaleString([], {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  });
}

function formatPct(rate: number | null): string {
  if (rate === null) return 'not reported';
  return `${Math.round(rate * 100)}% from cache`;
}

function formatRole(role: string): string {
  return role || 'no role';
}

function matchesFilter(call: CacheCall, filter: RoleFilter): boolean {
  if (filter === 'all') return true;
  const isChat = call.role === CHAT_ROLE;
  return filter === 'chat' ? isChat : !isChat;
}

interface Point {
  call: CacheCall;
  x: number;
  y: number;
  band: Band;
}

function layout(calls: CacheCall[]): Point[] {
  if (calls.length === 0) return [];
  const times = calls.map((c) => new Date(c.ts).getTime());
  const minT = Math.min(...times);
  const maxT = Math.max(...times);
  const span = maxT - minT || 1;
  const innerW = W - PAD_LEFT - PAD_RIGHT;
  return calls.map((call, i) => {
    const band = bandOf(call);
    const x = PAD_LEFT + ((times[i] - minT) / span) * innerW;
    const y =
      band === 'unreported'
        ? UNREPORTED_Y
        : PLOT_BOTTOM - (call.hit_rate as number) * PLOT_H;
    return { call, x, y, band };
  });
}

const LEGEND: { band: Band; label: string }[] = [
  { band: 'hot', label: 'Hot, 50% or more from cache' },
  { band: 'warm', label: 'Warm, some of it from cache' },
  { band: 'cold', label: 'Cold, a measured 0%' },
  { band: 'unreported', label: 'Not reported by the provider' },
];

export function CacheScatter({ calls }: { calls: CacheCall[] }) {
  const [hovered, setHovered] = useState<number | null>(null);
  const [roleFilter, setRoleFilter] = useState<RoleFilter>('all');

  const shown = useMemo(
    () => calls.filter((c) => matchesFilter(c, roleFilter)),
    [calls, roleFilter],
  );
  const points = useMemo(() => layout(shown), [shown]);
  const read = hovered !== null ? (points[hovered] ?? null) : null;

  return (
    <figure className="cache-scatter">
      <p className="cache-scatter__lead t-meta">
        Every model call in this window, plotted by time and by how much of
        its input the cache served. A run of hollow dots is calls the
        provider said nothing about, not calls that missed.
      </p>

      {calls.length === 0 ? (
        <p className="cache-scatter__empty t-meta">
          No model calls in this window yet, so there is nothing to plot.
        </p>
      ) : (
        <>
          <Segmented
            className="cache-scatter__filter"
            label="Which calls to show"
            value={roleFilter}
            onSelect={(key) => {
              setRoleFilter(key);
              setHovered(null);
            }}
            items={ROLE_FILTERS}
          />

          {shown.length === 0 ? (
            <p className="cache-scatter__empty t-meta">
              No calls in this window match that filter.
            </p>
          ) : (
            <>
              <div className="cache-scatter__plot-wrap">
                <svg
                  viewBox={`0 0 ${W} ${H}`}
                  role="img"
                  aria-label={`${shown.length} model calls plotted by time and cache hit rate`}
                  className="cache-scatter__plot"
                  onMouseLeave={() => setHovered(null)}
                >
                  <line
                    className="cache-scatter__divider"
                    x1={PAD_LEFT}
                    x2={W - PAD_RIGHT}
                    y1={DIVIDER_Y}
                    y2={DIVIDER_Y}
                  />
                  <line
                    className="cache-scatter__grid"
                    x1={PAD_LEFT}
                    x2={W - PAD_RIGHT}
                    y1={PLOT_TOP}
                    y2={PLOT_TOP}
                  />
                  <line
                    className="cache-scatter__grid"
                    x1={PAD_LEFT}
                    x2={W - PAD_RIGHT}
                    y1={PLOT_TOP + PLOT_H / 2}
                    y2={PLOT_TOP + PLOT_H / 2}
                  />
                  <line
                    className="cache-scatter__base"
                    x1={PAD_LEFT}
                    x2={W - PAD_RIGHT}
                    y1={PLOT_BOTTOM}
                    y2={PLOT_BOTTOM}
                  />

                  <text className="cache-scatter__tick" x={0} y={UNREPORTED_Y + 2.5}>
                    n/a
                  </text>
                  <text className="cache-scatter__tick" x={0} y={PLOT_TOP + 2.5}>
                    100%
                  </text>
                  <text className="cache-scatter__tick" x={0} y={PLOT_TOP + PLOT_H / 2 + 2.5}>
                    50%
                  </text>
                  <text className="cache-scatter__tick" x={0} y={PLOT_BOTTOM + 2.5}>
                    0%
                  </text>

                  {points.map((p, i) => (
                    <g key={`${p.call.turn_id}-${p.call.ts}-${i}`}>
                      {/* The hit target: bigger than the mark, and the one
                          that owns focus and hover. The visible dot
                          underneath it is decorative and never intercepts a
                          pointer, so the two always agree on which call is
                          being read. */}
                      <circle
                        className="cache-scatter__hit"
                        cx={p.x}
                        cy={p.y}
                        r={6}
                        tabIndex={0}
                        aria-label={`${formatWhen(p.call.ts)}, ${p.call.model}, ${formatRole(p.call.role)}, ${p.call.input_tokens.toLocaleString()} tokens in, ${formatPct(p.call.hit_rate)}, $${p.call.cost_usd.toFixed(4)}`}
                        onMouseEnter={() => setHovered(i)}
                        onMouseLeave={() => setHovered((h) => (h === i ? null : h))}
                        onFocus={() => setHovered(i)}
                        onBlur={() => setHovered((h) => (h === i ? null : h))}
                      />
                      <circle
                        className={`cache-scatter__dot cache-scatter__dot--${p.band}${hovered === i ? ' is-read' : ''}`}
                        cx={p.x}
                        cy={p.y}
                        r={hovered === i ? 3.6 : 2.4}
                      />
                    </g>
                  ))}
                </svg>
              </div>

              <div className="cache-scatter__axis t-meta" aria-hidden="true">
                <span>{formatWhen(shown[0].ts)}</span>
                <span>{formatWhen(shown[shown.length - 1].ts)}</span>
              </div>

              {/* The legend by default; the reading for whichever dot has
                  the pointer or the focus once one does. Never both at
                  once, the way the drift history chart already does it
                  below this room. */}
              <figcaption className="cache-scatter__legend" aria-live="polite">
                {read ? (
                  <>
                    <span className="cache-scatter__read-time t-meta">
                      {formatWhen(read.call.ts)}
                    </span>
                    <span className="cache-scatter__read t-meta">{read.call.model}</span>
                    <span className="cache-scatter__read t-meta">
                      {formatRole(read.call.role)}
                    </span>
                    <span className="cache-scatter__read t-meta">
                      {read.call.input_tokens.toLocaleString()} in
                    </span>
                    <span className="cache-scatter__read t-meta">
                      {read.call.cached_tokens === null
                        ? 'not reported'
                        : `${read.call.cached_tokens.toLocaleString()} cached`}
                    </span>
                    <span className="cache-scatter__read t-meta">
                      {formatPct(read.call.hit_rate)}
                    </span>
                    <span className="cache-scatter__read t-meta">
                      ${read.call.cost_usd.toFixed(4)}
                    </span>
                  </>
                ) : (
                  LEGEND.map(({ band, label }) => (
                    <span key={band} className="cache-scatter__key t-meta">
                      <span
                        className={`cache-scatter__swatch cache-scatter__swatch--${band}`}
                        aria-hidden="true"
                      />
                      {label}
                    </span>
                  ))
                )}
              </figcaption>
            </>
          )}

          {/* The same figures as text, for every call the filter admits. A
              dot's position is never the only way to read this chart, and a
              screen reader gets the numbers rather than a shape. */}
          <table className="visually-hidden">
            <caption>Model calls by time and cache hit rate</caption>
            <thead>
              <tr>
                <th scope="col">Time</th>
                <th scope="col">Model</th>
                <th scope="col">Role</th>
                <th scope="col">Input tokens</th>
                <th scope="col">Cached tokens</th>
                <th scope="col">Hit rate</th>
                <th scope="col">Cost</th>
              </tr>
            </thead>
            <tbody>
              {shown.map((c, i) => (
                <tr key={`${c.turn_id}-${c.ts}-row-${i}`}>
                  <th scope="row">{formatWhen(c.ts)}</th>
                  <td>{c.model}</td>
                  <td>{formatRole(c.role)}</td>
                  <td>{c.input_tokens}</td>
                  <td>{c.cached_tokens ?? 'not reported'}</td>
                  <td>{formatPct(c.hit_rate)}</td>
                  <td>${c.cost_usd.toFixed(4)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </figure>
  );
}

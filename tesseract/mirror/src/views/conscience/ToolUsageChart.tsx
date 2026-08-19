import { useMemo, useState } from 'react';

import { Disclosure } from '../../components/common/Disclosure';
import { Segmented } from '../../components/common/Segmented';
import type { ToolUsageWindow } from '../../stores/conscience';

/** Which tools actually get used, and — the half that matters — which do not.
 *
 * A horizontal bar per used tool rather than a column chart: the label is a
 * tool name, names are long, and a vertical axis of rotated `browser_snapshot`
 * is a chart nobody reads. Length is distinct sessions, which is the rank the
 * ledger is kept in; calls ride alongside as text because the two disagree
 * often and interestingly — one loop calling a tool four hundred times is one
 * session's worth of evidence.
 *
 * **The unused list is not an afterthought.** The working set is a dial, and
 * the reason to look at this panel is to find a tool you were sure got used
 * and see a zero next to it. Ranking only what was used would hide exactly
 * that, so the count leads and the names are one disclosure away.
 *
 * A readout, not a control. `_CORE_TOOL_NAMES` lives in the sealed app tree,
 * so nothing here can change what loads — IS-11 moves it into config, and a
 * disabled knob in the meantime would promise something the build cannot do.
 */
export function ToolUsageChart({
  windows,
  total,
  roster,
}: {
  windows: ToolUsageWindow[];
  total: number;
  roster: boolean;
}) {
  const [days, setDays] = useState<number>(windows[0]?.days ?? 7);
  const [showUnused, setShowUnused] = useState(false);
  const active = windows.find((w) => w.days === days) ?? windows[0];

  const { used, unused } = useMemo(() => {
    const rows = active?.tools ?? [];
    return {
      used: rows.filter((r) => r.calls > 0),
      unused: rows.filter((r) => r.calls === 0).map((r) => r.tool),
    };
  }, [active]);

  const max = Math.max(1, ...used.map((r) => r.sessions));

  if (!active) return null;

  return (
    <figure className="tool-usage">
      <div className="tool-usage__head">
        <p className="tool-usage__count">
          <strong>{used.length}</strong> of {total} tools used
        </p>
        {windows.length > 1 && (
          <Segmented
            label="Usage window"
            value={days}
            onSelect={setDays}
            items={windows.map((w) => ({
              key: w.days,
              label: `${w.days}d`,
              hint: `Tool calls in the last ${w.days} days`,
            }))}
          />
        )}
      </div>

      {used.length === 0 ? (
        <p className="t-meta">
          Nothing has been called in this window. The ledger records one row per
          tool call, so this is either a quiet fortnight or a very new install.
        </p>
      ) : (
        <ol
          className="tool-usage__bars"
          role="img"
          aria-label={`${used.length} tools used in the last ${days} days, ranked by how many separate sessions called each`}
        >
          {used.map((r) => (
            <li key={r.tool} className="tool-usage__row">
              <span className="tool-usage__name">{r.tool}</span>
              <span className="tool-usage__track">
                <span
                  className="tool-usage__bar"
                  style={{ width: `${(r.sessions / max) * 100}%` }}
                />
              </span>
              <span className="tool-usage__figures t-meta">
                {r.sessions} {r.sessions === 1 ? 'session' : 'sessions'} ·{' '}
                {r.calls} {r.calls === 1 ? 'call' : 'calls'}
              </span>
            </li>
          ))}
        </ol>
      )}

      <figcaption className="tool-usage__caption t-meta">
        Ranked by how many separate sessions called each tool, never by raw
        calls. {roster ? '' : 'The registry was unavailable, so this lists only what the ledger holds — a tool absent here may exist and simply never have been called. '}
      </figcaption>

      {roster && unused.length > 0 && (
        <>
          <Disclosure
            variant="row"
            open={showUnused}
            onToggle={() => setShowUnused((v) => !v)}
            ariaControls="tool-usage-unused"
          >
            {unused.length} never called in this window
          </Disclosure>
          {showUnused && (
            <div id="tool-usage-unused">
              <p className="t-meta tool-usage__unused-lead">
                The interesting half. A tool here is a demotion candidate —
                unless it is one you were sure got used, which is the reading
                this panel exists for.
              </p>
              <ul className="tool-usage__unused">
                {unused.map((name) => (
                  <li key={name} className="t-meta">
                    {name}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </>
      )}

      {/* The same figures as text — a bar length is never the only way to read
          a chart, and a screen reader gets numbers rather than a shape. */}
      <table className="visually-hidden">
        <caption>Tool usage over the last {days} days</caption>
        <thead>
          <tr>
            <th scope="col">Tool</th>
            <th scope="col">Sessions</th>
            <th scope="col">Calls</th>
          </tr>
        </thead>
        <tbody>
          {(active.tools ?? []).map((r) => (
            <tr key={`${r.tool}-row`}>
              <th scope="row">{r.tool}</th>
              <td>{r.sessions}</td>
              <td>{r.calls}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </figure>
  );
}

// AU-10 — Outbound notifications mute + rate panel.
//
// Renders the eight notification categories with: an exempt chip for the
// three categories that bypass the rate cap, the current sliding-window
// usage, and a toggle that flips the runtime mute (the YAML mute list
// stays hand-edited). One-off polled fetch on mount + after each toggle;
// no WS envelope yet (mute state changes are rare, this is fine).

import { useCallback, useEffect, useState } from 'react';
import { useWebSocketStore } from '../../stores/websocket';
import { useToastStore } from '../../stores/toasts';
import {
  ApiError,
  getNotificationsConfig,
  getNotificationsRates,
  postNotificationMute,
  type NotificationsConfig,
  type NotificationsRatesRow,
} from '../../lib/api';

const CHANNEL = 'telegram';

interface Row {
  category: string;
  exempt: boolean;
  muted: boolean;
  mutedByYaml: boolean;
  used: number;
  cap: number;
}

export function NotificationsPane(): React.ReactElement {
  const [rows, setRows] = useState<Row[]>([]);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<Set<string>>(new Set());

  const loadAll = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [cfg, rates] = await Promise.all([
        getNotificationsConfig(),
        getNotificationsRates(),
      ]);
      setRows(mergeRows(cfg, rates.rows));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadAll();
  }, [loadAll]);

  const onToggle = useCallback(
    async (category: string, nextMuted: boolean) => {
      const sid = useWebSocketStore.getState().sessionId;
      if (!sid) {
        useToastStore.getState().push('No session id — connect first.', 'error');
        return;
      }
      setBusy((b) => new Set(b).add(category));
      try {
        await postNotificationMute({
          session_id: sid,
          channel: CHANNEL,
          category,
          muted: nextMuted,
        });
        await loadAll();
      } catch (err) {
        const detail = err instanceof ApiError ? err.message : String(err);
        useToastStore.getState().push(`Mute toggle failed: ${detail}`, 'error');
      } finally {
        setBusy((b) => {
          const next = new Set(b);
          next.delete(category);
          return next;
        });
      }
    },
    [loadAll],
  );

  return (
    <div className="runtime-block notifications-pane">
      <div className="runtime-block__title">
        Notifications
        <span className="t-meta" style={{ marginLeft: 8 }}>
          Telegram · sliding 1h window
        </span>
      </div>
      {loading && <p className="t-meta">Loading…</p>}
      {error && !loading && <p className="t-meta">Failed: {error}</p>}
      {!loading && !error && rows.length === 0 && (
        <p className="t-meta">No categories.</p>
      )}
      {!loading && !error && rows.length > 0 && (
        <ul className="notifications-pane__list">
          {rows.map((row) => (
            <li key={row.category} className="notifications-pane__row">
              <span className="notifications-pane__cat">{row.category}</span>
              {row.exempt && (
                <span className="t-meta" title="bypasses rate cap">
                  exempt
                </span>
              )}
              {!row.exempt && (
                <span className="t-meta">
                  {row.used}/{row.cap}
                </span>
              )}
              {(() => {
                const on = !row.muted;
                const locked = busy.has(row.category) || row.mutedByYaml;
                const label = row.mutedByYaml
                  ? 'muted in YAML'
                  : on ? 'on' : 'off';
                return (
                  <button
                    type="button"
                    className={
                      'notifications-pane__toggle' +
                      (on ? ' is-on' : ' is-off') +
                      (locked ? ' is-locked' : '')
                    }
                    aria-pressed={on}
                    aria-label={`${on ? 'mute' : 'unmute'} ${row.category}`}
                    disabled={locked}
                    onClick={() => void onToggle(row.category, on)}
                  >
                    <span
                      className="notifications-pane__toggle-box"
                      aria-hidden="true"
                    >
                      {on && (
                        <svg
                          viewBox="0 0 12 12"
                          width="10"
                          height="10"
                          fill="none"
                          stroke="currentColor"
                          strokeWidth="2"
                          strokeLinecap="round"
                          strokeLinejoin="round"
                        >
                          <polyline points="2.5 6.5 5 9 9.5 3.5" />
                        </svg>
                      )}
                    </span>
                    <span className="notifications-pane__toggle-label">
                      {label}
                    </span>
                  </button>
                );
              })()}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}


function mergeRows(
  cfg: NotificationsConfig,
  rates: NotificationsRatesRow[],
): Row[] {
  const channel = cfg.channels.find((c) => c.name === CHANNEL);
  const mutedRuntime = new Set<string>(channel?.muted_runtime ?? []);
  const mutedYaml = new Set<string>(channel?.muted_yaml ?? []);
  const ratesByCat = new Map<string, NotificationsRatesRow>();
  for (const r of rates) {
    if (r.channel === CHANNEL) ratesByCat.set(r.category, r);
  }
  return cfg.categories.map((c) => {
    const rate = ratesByCat.get(c.category);
    return {
      category: c.category,
      exempt: c.exempt,
      mutedByYaml: mutedYaml.has(c.category),
      muted: mutedRuntime.has(c.category) || mutedYaml.has(c.category),
      used: rate?.used_last_hour ?? 0,
      cap: rate?.cap_per_hour ?? 0,
    };
  });
}

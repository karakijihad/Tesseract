// AU-10 — Outbound notifications mute + rate panel.
//
// Renders the eight notification categories with: an exempt chip for the
// three categories that bypass the rate cap, the current sliding-window
// usage, and a toggle that flips the runtime mute (the YAML mute list
// stays hand-edited). One-off polled fetch on mount + after each toggle;
// no WS envelope yet (mute state changes are rare, this is fine).

import { Block } from '../../components/common/Block';
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
import { Hint } from '../../components/ui/Hint';
import { Checkbox } from '../../components/common/Checkbox';

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
    <Block title={null} meta="Telegram · sliding 1h window">
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
                <Hint label="bypasses rate cap">
                  <span className="t-meta">
                    exempt
                  </span>
                </Hint>
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
                // This was a `button` wrapping a `span` drawn to look like a
                // checkbox, with its own tick path and its own on/off colours
                // — a control the keyboard could reach only as a button and a
                // screen reader never heard as checked. It is the app's box.
                return (
                  <Checkbox
                    checked={on}
                    disabled={locked}
                    onChange={() => void onToggle(row.category, on)}
                    label={label}
                    tone="state"
                  />
                );
              })()}
            </li>
          ))}
        </ul>
      )}
    </Block>
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

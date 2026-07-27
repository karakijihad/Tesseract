import { useEffect } from 'react';
import { BACKEND_BASE } from '../../../lib/endpoints';
import { useObservationsStore } from '../../../stores/observations';
import { useObserverStore } from '../../../stores/observer';
import { formatRelative } from '../../../lib/time';
import { Hint } from '../../ui/Hint';

// Stats chip is display-only; 30s is fresh enough and keeps the backend quiet.
const POLL_INTERVAL_MS = 30_000;
const BAR_FULL_AT_TOKENS = 10_000;

function breakerColor(state: string): string {
  if (state === 'red') return 'var(--bad)';
  if (state === 'yellow') return 'var(--warn)';
  return 'var(--ok)';
}

export function ObserverStatsChip() {
  const stats = useObservationsStore(s => s.stats);
  const setStats = useObservationsStore(s => s.setStats);
  const armState = useObserverStore(s => s.state);

  useEffect(() => {
    if (armState === 'off') return;

    let cancelled = false;

    const poll = async () => {
      try {
        const res = await fetch(`${BACKEND_BASE}/api/observer/stats`);
        if (!res.ok) return;
        const data = await res.json();
        if (!cancelled) setStats(data);
      } catch {
        /* transient network blip — next tick retries */
      }
    };

    poll();
    const id = window.setInterval(poll, POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [armState, setStats]);

  const pct = Math.min(100, (stats.tokens_used_total / BAR_FULL_AT_TOKENS) * 100);

  return (
    <div className="observer-stats-chip">
      <div className="observer-stats-bar" role="progressbar"
           aria-valuenow={stats.tokens_used_total} aria-valuemin={0}>
        <div className="observer-stats-bar-fill" style={{ width: `${pct}%` }} />
      </div>
      <div className="observer-stats-row t-caption">
        <span className="observer-stats-metric">
          <Hint label={`circuit breaker: ${stats.circuit_breaker_state}`} position="top" maxWidth={180}>
            <span
              className="observer-stats-dot"
              style={{ background: breakerColor(stats.circuit_breaker_state) }}
            />
          </Hint>
          {stats.fires_total} obs
        </span>
        <span className="observer-stats-metric">
          {stats.tokens_used_total.toLocaleString()} tok
        </span>
        <span className="observer-stats-metric observer-stats-last-fired">
          {formatRelative(stats.last_fired_at, 'never')}
        </span>
      </div>
    </div>
  );
}

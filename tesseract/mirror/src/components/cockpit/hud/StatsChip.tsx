import { useSessionStore } from '../../../stores/session';
import { sendCommand } from '../../../lib/commands';
import { Hint } from '../../ui/Hint';

function formatTokens(n: number): string {
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
  return `${n}`;
}

function colorBand(ratio: number): 'ok' | 'warn' | 'bad' {
  if (ratio < 0.60) return 'ok';
  if (ratio < 0.85) return 'warn';
  return 'bad';
}

export function StatsChip() {
  const stats = useSessionStore((s) => s.latestStats);

  if (!stats) {
    return (
      <Hint label="No stats yet — click to request" position="top" maxWidth={240}>
        <button
          type="button"
          className="hud-stats hud-stats--empty"
          onClick={() => sendCommand('/stats')}
          aria-label="No stats yet"
        >
          <span className="hud-stats-text">Turns 0 · —</span>
          <span className="hud-stats-bar hud-stats-bar--empty" aria-hidden="true" />
        </button>
      </Hint>
    );
  }

  const threshold = stats.compact_threshold_tokens;
  const totalRatio = threshold > 0 ? Math.min(stats.tokens / threshold, 1) : 0;
  const systemRatio = threshold > 0 ? Math.min(stats.system_tokens / threshold, 1) : 0;
  const conversationRatio = Math.max(totalRatio - systemRatio, 0);
  const band = colorBand(totalRatio);
  const label =
    `Turns ${stats.turns} · ${formatTokens(stats.tokens)} of ${formatTokens(threshold)} ` +
    `(${formatTokens(stats.system_tokens)} manifest · ${formatTokens(stats.tokens - stats.system_tokens)} chat)`;

  return (
    <Hint label={label} position="top" maxWidth={320}>
      <button
        type="button"
        className={`hud-stats hud-stats--${band}`}
        onClick={() => sendCommand('/stats')}
        aria-label={label}
      >
        <span className="hud-stats-text">
          Turns {stats.turns} · {formatTokens(stats.tokens)}
        </span>
        <span className="hud-stats-bar" aria-hidden="true">
          <span
            className="hud-stats-fill hud-stats-fill--system"
            style={{ width: `${Math.round(systemRatio * 100)}%` }}
          />
          <span
            className="hud-stats-fill hud-stats-fill--chat"
            style={{ width: `${Math.round(conversationRatio * 100)}%` }}
          />
        </span>
      </button>
    </Hint>
  );
}

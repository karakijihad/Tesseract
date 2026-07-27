/* MO-9-11 Status pane — bridge state, stats grid, Restart, Online/Offline toggle.
 *
 * The Online/Offline toggle drives `state.py::save_status` via the
 * ASK-gated REST endpoint. The Telegram bridge re-reads `status.json`
 * on every inbound message tick, so no bridge restart is required for
 * the flip to take effect end-to-end (see phase doc §1 mech-2 wiring). */
import { useChannelsStore, type ChannelRow, type TelegramOverride } from '../../stores/channels';
import { useToastStore } from '../../stores/toasts';
import { useWebSocketStore } from '../../stores/websocket';

interface StatusPaneProps {
  channel: ChannelRow;
}

function _fmtPoll(iso: string | null): string {
  if (!iso) return 'never';
  try {
    const dt = new Date(iso);
    if (Number.isNaN(dt.getTime())) return iso;
    return dt.toLocaleTimeString();
  } catch {
    return iso;
  }
}

function _overrideLabel(value: TelegramOverride | undefined | null): string {
  if (value === 'online') return 'online (forced)';
  if (value === 'offline') return 'offline (queueing)';
  return 'follow bridge';
}

export function StatusPane({ channel }: StatusPaneProps) {
  const snap = channel.status_snapshot;
  const restartChannel = useChannelsStore((s) => s.restartChannel);
  const setTelegramOverride = useChannelsStore((s) => s.setTelegramOverride);
  const pending = useChannelsStore((s) => s.pending);
  const error = useChannelsStore((s) => s.error);
  const sessionId = useWebSocketStore((s) => s.sessionId);
  const push = useToastStore((s) => s.push);

  const isTelegram = channel.name === 'telegram';
  const currentOverride = (channel.extras?.override ?? null) as TelegramOverride;
  const restartBusy = Boolean(pending[channel.name]);
  const overrideBusy = Boolean(pending['telegram:override']);

  const _onRestart = async () => {
    if (!sessionId) {
      push('Channels: no operator session — open chat first', 'warning');
      return;
    }
    try {
      const result = await restartChannel(channel.name, sessionId);
      if (result.approved) {
        push(`${channel.name} restarted`, 'info');
      } else {
        push(`${channel.name} restart denied: ${result.output}`, 'warning');
      }
    } catch (err) {
      push(
        `${channel.name} restart failed: ${
          err instanceof Error ? err.message : String(err)
        }`,
        'error',
      );
    }
  };

  const _onSetOverride = async (next: TelegramOverride) => {
    if (!sessionId) {
      push('Channels: no operator session — open chat first', 'warning');
      return;
    }
    if (next === currentOverride) return;
    try {
      const result = await setTelegramOverride(next, sessionId);
      if (result.approved) {
        push(`Telegram override set to ${_overrideLabel(next)}`, 'info');
      } else {
        push(`Telegram override denied: ${result.output}`, 'warning');
      }
    } catch (err) {
      push(
        `Telegram override failed: ${
          err instanceof Error ? err.message : String(err)
        }`,
        'error',
      );
    }
  };

  return (
    <div data-testid="channel-status-pane">
      <section className="channel-status-section">
        <span className="channel-status-section-label">Bridge</span>
        <div className="channel-state-row">
          <span
            className={`channel-state-badge state-${snap.bridge_state}`}
            data-testid="channel-state-badge"
          >
            {snap.bridge_state}
          </span>
          <span className="channel-state-poll t-meta">
            last poll {_fmtPoll(snap.last_poll_at)}
          </span>
        </div>
      </section>

      <section className="channel-status-section">
        <span className="channel-status-section-label">Counters (24h)</span>
        <div className="channel-stats-grid">
          <div className="channel-stat">
            <span className="channel-stat-label">errors</span>
            <span className="channel-stat-value">{snap.error_count_24h}</span>
          </div>
          <div className="channel-stat">
            <span className="channel-stat-label">in</span>
            <span className="channel-stat-value">{snap.messages_in_24h}</span>
          </div>
          <div className="channel-stat">
            <span className="channel-stat-label">out</span>
            <span className="channel-stat-value">{snap.messages_out_24h}</span>
          </div>
          <div className="channel-stat">
            <span className="channel-stat-label">allowed</span>
            <span className="channel-stat-value">{snap.allowed_count}</span>
          </div>
          <div className="channel-stat">
            <span className="channel-stat-label">pending</span>
            <span className="channel-stat-value">{snap.pending_count}</span>
          </div>
        </div>
      </section>

      <section className="channel-status-section">
        <span className="channel-status-section-label">Actions</span>
        <div className="channel-actions">
          <button
            type="button"
            className="channel-restart-btn"
            onClick={_onRestart}
            disabled={restartBusy}
            data-testid="channel-restart-btn"
            aria-label={`restart ${channel.name}`}
          >
            {restartBusy ? 'restarting…' : 'restart bridge'}
          </button>

          {isTelegram && (
            <div
              className="channel-override-toggle"
              role="radiogroup"
              aria-label="telegram availability override"
            >
              <button
                type="button"
                role="radio"
                aria-checked={currentOverride === null}
                className={`channel-override-segment${
                  currentOverride === null ? ' is-active' : ''
                }`}
                onClick={() => void _onSetOverride(null)}
                disabled={overrideBusy}
                data-testid="channel-override-follow"
              >
                follow
              </button>
              <button
                type="button"
                role="radio"
                aria-checked={currentOverride === 'online'}
                className={`channel-override-segment${
                  currentOverride === 'online' ? ' is-active' : ''
                }`}
                onClick={() => void _onSetOverride('online')}
                disabled={overrideBusy}
                data-testid="channel-override-online"
              >
                online
              </button>
              <button
                type="button"
                role="radio"
                aria-checked={currentOverride === 'offline'}
                className={`channel-override-segment${
                  currentOverride === 'offline' ? ' is-active' : ''
                }`}
                onClick={() => void _onSetOverride('offline')}
                disabled={overrideBusy}
                data-testid="channel-override-offline"
              >
                offline
              </button>
              <span className="channel-override-hint t-meta">
                {_overrideLabel(currentOverride)}
              </span>
            </div>
          )}
        </div>
      </section>

      {error && (
        <div className="channel-error" data-testid="channel-error">
          {error}
        </div>
      )}
    </div>
  );
}

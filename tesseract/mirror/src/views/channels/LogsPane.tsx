/* MO-9-11 Logs pane — live tail of `log_error` envelopes whose logger
 * namespace matches the selected channel. Subscribes to the same log
 * fan-out path the pulse panel uses (`log_error` envelopes from the
 * backend log forwarder), filtered through `channels.applyEnvelope`. */
import { useChannelsStore, selectLogsForChannel } from '../../stores/channels';

interface LogsPaneProps {
  channel: string;
}

function _fmtTs(iso: string): string {
  try {
    const dt = new Date(iso);
    if (Number.isNaN(dt.getTime())) return iso;
    return dt.toLocaleTimeString();
  } catch {
    return iso;
  }
}

export function LogsPane({ channel }: LogsPaneProps) {
  const entries = useChannelsStore((s) => selectLogsForChannel(s, channel));
  const clear = useChannelsStore((s) => s.clearLogs);

  return (
    <div className="channel-logs" data-testid="channel-logs-pane">
      <div className="channel-logs-head">
        <span className="channel-logs-meta">
          {entries.length} entr{entries.length === 1 ? 'y' : 'ies'} ·
          live tail
        </span>
        <button
          type="button"
          className="channels-view-btn"
          onClick={() => clear(channel)}
          disabled={entries.length === 0}
          data-testid="channel-logs-clear"
          aria-label="clear log buffer"
        >
          clear
        </button>
      </div>

      {entries.length === 0 ? (
        <div className="channel-log-empty">
          No log_error envelopes yet for{' '}
          <span className="t-meta">tesseract.integrations.{channel}.*</span>
        </div>
      ) : (
        <div className="channel-logs-rows">
          {entries.map((row, idx) => (
            <div
              key={`${row.ts}:${idx}`}
              className="channel-log-row"
              data-testid="channel-log-row"
            >
              <span className="channel-log-ts">{_fmtTs(row.ts)}</span>
              <span className="channel-log-level">{row.level}</span>
              <span className="channel-log-logger">{row.logger}</span>
              <span className="channel-log-message">{row.message}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

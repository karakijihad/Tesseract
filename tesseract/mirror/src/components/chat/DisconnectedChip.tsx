import { useWebSocketStore } from '../../stores/websocket';

export function DisconnectedChip() {
  const status = useWebSocketStore(s => s.status);
  if (status === 'connected') return null;
  return (
    <span className="awaiting-chip" style={{ margin: '8px 16px', display: 'block' }}>
      backend disconnected — retrying…
    </span>
  );
}

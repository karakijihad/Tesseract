import { useEffect } from 'react';
import { useWebSocketStore } from '../stores/websocket';

export function useWebSocket() {
  useEffect(() => {
    const ws = useWebSocketStore.getState();
    ws.connect();
    return () => {
      ws.disconnect();
    };
  }, []);
}

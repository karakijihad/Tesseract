import { useObservationsStore } from '../stores/observations';
import { useUIStore } from '../stores/ui';
import { useWebSocketStore } from '../stores/websocket';

export function sendCommand(cmd: string, tail: string = ''): void {
  const full = cmd + tail;
  if (full === '/stats') {
    useUIStore.getState().setPendingStatsToast(true);
  }
  if (cmd === '/observe') {
    useObservationsStore.getState().setPending(true);
  }
  useWebSocketStore.getState().sendMessage('command', { cmd: full });
}

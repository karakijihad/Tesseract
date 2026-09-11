import { useConversationStore } from '../stores/conversation';
import { useObservationsStore } from '../stores/observations';
import { useToastStore } from '../stores/toasts';
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

/** Put a question to the assistant from a surface that is not the composer.
 *
 * `sendCommand`'s sibling, and the difference is who answers. A slash command
 * is a kernel tool and the model never sees it, which is right when the room
 * already knows the whole act. This is for the other case: the room has a
 * fact and no authority to decide what should be done about it, so the words
 * go in as the operator's own message and the assistant reasons from there.
 *
 * It reports the one way it fails. `sendUserMessage` returns false when there
 * is no conversation to put a message in yet, and a control that silently
 * dropped the request would look exactly like one that worked.
 */
export function askAssistant(text: string): void {
  const sent = useConversationStore.getState().sendUserMessage(null, text);
  if (!sent) {
    useToastStore
      .getState()
      .push('There is no conversation open to ask in yet. Try again in a moment.', 'warning');
  }
}

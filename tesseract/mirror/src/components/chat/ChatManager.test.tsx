import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';

const sendMock = vi.fn();
vi.mock('../../stores/websocket', () => ({
  useWebSocketStore: (sel: (s: { sendMessage: typeof sendMock }) => unknown) => sel({ sendMessage: sendMock }),
}));
vi.mock('../../lib/endpoints', () => ({ BACKEND_BASE: 'http://test' }));

import { ChatManager } from './ChatManager';
import { useConversationStore } from '../../stores/conversation';
import type { ApprovalRequest } from '../../lib/types';

const A = 'a'.repeat(32);
const B = 'b'.repeat(32);
const ARCHIVED = 'e'.repeat(32);

function approval(callId: string): ApprovalRequest {
  return { call_id: callId, name: 'web_search', input: {}, reason: '', received_at: 0, resolved: false };
}

let container: HTMLDivElement;
let root: Root;

function q<T extends Element>(sel: string): T | null {
  return container.querySelector<T>(sel);
}

function openPanel() {
  act(() => (q<HTMLButtonElement>('.chat-mgr-trigger'))!.click());
}

// toggleArchived → fetch() → resp.json() are two microtask hops.
async function flush() {
  await act(async () => { await new Promise(r => setTimeout(r, 0)); });
}

describe('ChatManager (multichat-redesign)', () => {
  beforeEach(() => {
    sendMock.mockClear();
    useConversationStore.setState({ chats: new Map(), orderedIds: [], activeChatId: null });
    const s = useConversationStore.getState();
    s.initChat(A);
    s.initChat(B);
    s.setChatTitle(A, 'Chat A');
    s.setChatTitle(B, 'Chat B');
    useConversationStore.setState({ activeChatId: A }); // A active; B is background
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ chats: [
        { chat_id: ARCHIVED, title: 'Archived One', archived: true, message_count: 3 },
        { chat_id: 'f'.repeat(32), title: 'Still Open', archived: false, message_count: 1 },
      ] }),
    }) as unknown as typeof fetch;
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
    act(() => root.render(<ChatManager />));
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
  });

  it('trigger shows the active chat title and the open-count', () => {
    expect(q('.chat-mgr-active-title')!.textContent).toBe('Chat A');
    expect(q('.chat-mgr-count')!.textContent).toBe('2/10');
  });

  it('opens the panel and lists every open chat', () => {
    expect(q('.chat-mgr-panel')).toBeNull();
    openPanel();
    expect(q('.chat-mgr-panel')).not.toBeNull();
    expect(container.querySelectorAll('.chat-mgr-row').length).toBe(2);
    expect(q('.chat-mgr-row.is-active .chat-mgr-row-title')!.textContent).toBe('Chat A');
  });

  it('shows an aggregate approval dot on the trigger when a background chat awaits a tool ASK', () => {
    expect(q('.chat-mgr-trigger .chat-tab-approval')).toBeNull();
    act(() => { useConversationStore.getState().addApproval(B, approval('c1')); });
    expect(q('.chat-mgr-trigger .chat-tab-approval')).not.toBeNull();
  });

  it('switch, new, and archive route over WS', () => {
    openPanel();
    // orderedIds is newest-first: [B, A]. Row 0 = B (background), row 1 = A (active).
    const rows = container.querySelectorAll<HTMLElement>('.chat-mgr-row');
    const bRow = rows[0]; // Chat B
    expect(bRow.querySelector('.chat-mgr-row-title')!.textContent).toBe('Chat B');

    // switch to background chat B
    act(() => bRow.querySelector<HTMLButtonElement>('.chat-mgr-row-label')!.click());
    expect(sendMock).toHaveBeenCalledWith('chat.switch', { chat_id: B });

    act(() => q<HTMLButtonElement>('.chat-mgr-new')!.click());
    expect(sendMock).toHaveBeenCalledWith('chat.create', {});

    // archive B (its ✕ action)
    act(() => bRow.querySelector<HTMLButtonElement>('[aria-label="Archive chat"]')!.click());
    expect(sendMock).toHaveBeenCalledWith('chat.archive', { chat_id: B });
  });

  it('expands the archived section, lists only archived chats, and restores over WS', async () => {
    openPanel();
    act(() => q<HTMLButtonElement>('.chat-mgr-archived-header')!.click());
    await flush();
    expect(container.textContent).toContain('Archived One');
    expect(container.textContent).not.toContain('Still Open'); // non-archived filtered out
    act(() => q<HTMLButtonElement>('.chat-mgr-restore')!.click());
    expect(sendMock).toHaveBeenCalledWith('chat.restore', { chat_id: ARCHIVED });
  });

  it('permanently deletes an archived chat via REST after confirm', async () => {
    openPanel();
    act(() => q<HTMLButtonElement>('.chat-mgr-archived-header')!.click());
    await flush();
    // arm the confirm, then confirm
    act(() => q<HTMLButtonElement>('[aria-label="Delete permanently"]')!.click());
    act(() => q<HTMLButtonElement>('.chat-mgr-confirm-yes')!.click());
    await flush();
    expect(global.fetch).toHaveBeenCalledWith(`http://test/api/chats/${ARCHIVED}`, { method: 'DELETE' });
    expect(container.textContent).toContain('No archived chats');
  });

  it('a failed delete surfaces a row-level error without blanking the archived list', async () => {
    openPanel();
    act(() => q<HTMLButtonElement>('.chat-mgr-archived-header')!.click());
    await flush();
    // GET succeeded (list is shown); now make the DELETE fail.
    (global.fetch as ReturnType<typeof vi.fn>).mockResolvedValueOnce({ ok: false, status: 500 });
    act(() => q<HTMLButtonElement>('[aria-label="Delete permanently"]')!.click());
    act(() => q<HTMLButtonElement>('.chat-mgr-confirm-yes')!.click());
    await flush();
    // The row is still there (not blanked) and a per-row error shows.
    expect(container.textContent).toContain('Archived One');
    expect(container.textContent).toContain('Couldn’t delete');
    expect(container.textContent).not.toContain('Couldn’t load archived chats');
  });
});

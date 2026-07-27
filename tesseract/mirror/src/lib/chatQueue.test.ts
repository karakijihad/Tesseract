import { describe, it, expect } from 'vitest';
import { lastQueuedPosition, queueChipLabel } from './chatQueue';
import type { ChatMessage } from './types';

function msg(over: Partial<ChatMessage>): ChatMessage {
  return {
    id: over.id ?? 'm1',
    role: 'user',
    content: 'text',
    timestamp: 0,
    ...over,
  };
}

describe('lastQueuedPosition', () => {
  it('returns null when nothing is queued', () => {
    expect(lastQueuedPosition([msg({ status: 'complete' })])).toBeNull();
  });

  it('returns the position of the tail (most recently sent) queued message', () => {
    const messages = [
      msg({ id: 'a', status: 'queued', queuePosition: 1 }),
      msg({ id: 'b', status: 'queued', queuePosition: 2 }),
      msg({ id: 'c', status: 'queued', queuePosition: 3 }),
    ];
    expect(lastQueuedPosition(messages)).toBe(3);
  });

  it('ignores non-queued messages interleaved with queued ones', () => {
    const messages = [
      msg({ id: 'a', status: 'complete' }),
      msg({ id: 'b', role: 'assistant', status: 'complete' }),
      msg({ id: 'c', status: 'queued', queuePosition: 1 }),
    ];
    expect(lastQueuedPosition(messages)).toBe(1);
  });
});

describe('queueChipLabel', () => {
  it('renders "queued · N ahead" for queuePosition 3 (2 ahead)', () => {
    expect(queueChipLabel(3)).toBe('queued · 2 ahead');
  });

  it('shows no chip for queuePosition 1 (next up)', () => {
    expect(queueChipLabel(1)).toBeNull();
  });

  it('shows no chip when nothing is queued', () => {
    expect(queueChipLabel(null)).toBeNull();
  });
});

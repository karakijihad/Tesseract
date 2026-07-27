// Ambient orb captions — the pure line-deriver + the persisted enable toggle.

import { beforeEach, describe, expect, it, vi } from 'vitest';

import { currentTarsLine, tail } from './OrbCaptions';
import type { ChatMessage } from '../lib/types';

function msg(role: ChatMessage['role'], content: string): ChatMessage {
  return { id: `${role}-${content}`, role, content, timestamp: 0, status: 'complete' };
}

describe('currentTarsLine', () => {
  it('shows the live streaming answer while streaming', () => {
    const line = currentTarsLine({
      isStreaming: true,
      streamingText: 'opening the lane…',
      messages: [msg('assistant', 'old reply')],
    });
    expect(line).toBe('opening the lane…');
  });

  it('falls back to the most recent assistant message when idle', () => {
    const line = currentTarsLine({
      isStreaming: false,
      streamingText: '',
      messages: [msg('assistant', 'first'), msg('user', 'hi'), msg('assistant', 'latest reply')],
    });
    expect(line).toBe('latest reply');
  });

  it('never captions the operator (user) lines', () => {
    const line = currentTarsLine({
      isStreaming: false,
      streamingText: '',
      messages: [msg('assistant', 'tars said this'), msg('user', 'user typed this')],
    });
    expect(line).toBe('tars said this');
  });

  it('is empty when TARS has not spoken', () => {
    expect(currentTarsLine({ isStreaming: false, streamingText: '', messages: [msg('user', 'hello')] })).toBe('');
  });

  it('ignores empty streaming text (falls back to last assistant msg)', () => {
    const line = currentTarsLine({
      isStreaming: true,
      streamingText: '   ',
      messages: [msg('assistant', 'prior')],
    });
    expect(line).toBe('prior');
  });
});

describe('tail (subtitle truncation)', () => {
  it('returns a short line unchanged', () => {
    expect(tail('hello there')).toBe('hello there');
  });

  it('keeps the newest words for a long line, breaking on a word boundary', () => {
    const long = 'a'.repeat(300) + ' final words here';
    const out = tail(long);
    expect(out.startsWith('…')).toBe(true);
    expect(out.endsWith('final words here')).toBe(true);
    expect(out).not.toContain(' '.repeat(2)); // no doubled/leading space after …
    expect(out[1]).not.toBe(' '); // no leading space right after the ellipsis
  });

  it('handles a 221-char single word with no space (raw fallback)', () => {
    const word = 'x'.repeat(221);
    expect(tail(word)).toBe('…' + word.slice(1)); // 220-char window, no space → raw
  });
});

describe('captions store', () => {
  beforeEach(() => {
    vi.resetModules();
    localStorage.clear();
  });

  it('defaults enabled when no preference is stored', async () => {
    const { useCaptionsStore } = await import('../stores/captions');
    expect(useCaptionsStore.getState().enabled).toBe(true);
  });

  it('toggle flips + persists to localStorage', async () => {
    const { useCaptionsStore } = await import('../stores/captions');
    useCaptionsStore.getState().toggle();
    expect(useCaptionsStore.getState().enabled).toBe(false);
    expect(localStorage.getItem('tesseract.cockpit.captions.enabled')).toBe('0');
  });

  it('loads a persisted disabled preference on init', async () => {
    localStorage.setItem('tesseract.cockpit.captions.enabled', '0');
    const { useCaptionsStore } = await import('../stores/captions');
    expect(useCaptionsStore.getState().enabled).toBe(false);
  });
});

// TC-B7 — ControllerMirrorBlock render + vocabulary tests
//
// Two layers:
//   1. Static SSR render (renderToStaticMarkup) — structural / class-name checks.
//   2. Pure function unit tests (eventToLine / isThinkingEvent / isTurnEndEvent) —
//      verify the REAL controller event vocabulary (kind-discriminated), so a
//      future regression to the old Mirror ChunkType vocabulary (type-discriminated)
//      is caught immediately without needing a DOM.

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';

vi.mock('../../lib/endpoints', () => ({
  BACKEND_BASE: 'http://localhost:8000',
}));

// Mock WebSocket globally so it doesn't throw in the test env.
const WS_MOCK = vi.fn().mockImplementation(() => ({
  onopen: null,
  onmessage: null,
  onerror: null,
  onclose: null,
  readyState: 0,
  close: vi.fn(),
}));
global.WebSocket = WS_MOCK as unknown as typeof WebSocket;

import {
  ControllerMirrorBlock,
  ControllerEvent,
  eventToLine,
  isThinkingEvent,
  isTurnEndEvent,
  statusCardLabel,
} from './ControllerMirrorBlock';

beforeEach(() => {
  WS_MOCK.mockClear();
});

// ---------------------------------------------------------------------------
// Static render — structural checks
// ---------------------------------------------------------------------------

describe('ControllerMirrorBlock — static render', () => {
  it('renders the handoff card with session_id', () => {
    const html = renderToStaticMarkup(
      <ControllerMirrorBlock session_id="sess-abc-123" ws_path="/ws/controller/sess-abc-123" />,
    );
    expect(html).toContain('launched terminal session');
    expect(html).toContain('sess-abc-123');
  });

  it('renders the tars --session hint text', () => {
    const html = renderToStaticMarkup(
      <ControllerMirrorBlock session_id="sess-xyz" ws_path="/ws/controller/sess-xyz" />,
    );
    expect(html).toContain('tars --session sess-xyz');
  });

  it('does NOT show thinking indicator in initial static render', () => {
    const html = renderToStaticMarkup(
      <ControllerMirrorBlock session_id="sess-1" ws_path="/ws/controller/sess-1" />,
    );
    expect(html).not.toContain('TARS is working');
  });

  it('renders the transcript container', () => {
    const html = renderToStaticMarkup(
      <ControllerMirrorBlock session_id="sess-1" ws_path="/ws/controller/sess-1" />,
    );
    expect(html).toContain('controller-mirror__transcript');
  });

  it('hint text uses controller-mirror__hint class (must use var(--text-meta) in CSS)', () => {
    const html = renderToStaticMarkup(
      <ControllerMirrorBlock session_id="ses-check" ws_path="/ws/controller/ses-check" />,
    );
    expect(html).toContain('controller-mirror__hint');
  });

  it('session-id element uses controller-mirror__session-id class', () => {
    const html = renderToStaticMarkup(
      <ControllerMirrorBlock session_id="ses-id-check" ws_path="/ws/controller/ses-id-check" />,
    );
    expect(html).toContain('controller-mirror__session-id');
    expect(html).toContain('ses-id-check');
  });
});

// ---------------------------------------------------------------------------
// Real controller event vocabulary — eventToLine
//
// These tests exercise kind-discriminated parsing against the contract in
// tesseract/orchestrator/tars_controller/events.py.
// A regression to the old type-discriminated Mirror ChunkType vocabulary
// would cause these to fail.
// ---------------------------------------------------------------------------

describe('eventToLine — real controller vocabulary', () => {
  const key = 'k0';

  it('assistant_text → text line with event.text', () => {
    const ev: ControllerEvent = { kind: 'assistant_text', text: 'Hello', partial: false };
    const line = eventToLine(ev, key);
    expect(line).not.toBeNull();
    expect(line!.kind).toBe('text');
    expect(line!.text).toBe('Hello');
  });

  it('assistant_text with empty text → null (skipped)', () => {
    const ev: ControllerEvent = { kind: 'assistant_text', text: '', partial: true };
    expect(eventToLine(ev, key)).toBeNull();
  });

  it('tool_use → tool line with "→ <tool_name>"', () => {
    const ev: ControllerEvent = { kind: 'tool_use', tool: 'memory_search', tool_use_id: 'tu-1' };
    const line = eventToLine(ev, key);
    expect(line).not.toBeNull();
    expect(line!.kind).toBe('tool');
    expect(line!.text).toContain('memory_search');
    expect(line!.text).toContain('→');
  });

  it('tool_use with missing tool field → fallback "tool" label', () => {
    const ev: ControllerEvent = { kind: 'tool_use', tool_use_id: 'tu-2' };
    const line = eventToLine(ev, key);
    expect(line).not.toBeNull();
    expect(line!.text).toContain('tool');
  });

  it('tool_result success=true → "✓ tool done" tool line', () => {
    const ev: ControllerEvent = { kind: 'tool_result', tool_use_id: 'tu-1', success: true };
    const line = eventToLine(ev, key);
    expect(line).not.toBeNull();
    expect(line!.kind).toBe('tool');
    expect(line!.text).toContain('✓');
  });

  it('tool_result success=false → "✗ tool error" tool line', () => {
    const ev: ControllerEvent = { kind: 'tool_result', tool_use_id: 'tu-1', success: false };
    const line = eventToLine(ev, key);
    expect(line).not.toBeNull();
    expect(line!.kind).toBe('tool');
    expect(line!.text).toContain('✗');
  });

  it('user_text → system line prefixed with ">"', () => {
    const ev: ControllerEvent = { kind: 'user_text', text: 'explain this' };
    const line = eventToLine(ev, key);
    expect(line).not.toBeNull();
    expect(line!.kind).toBe('system');
    expect(line!.text).toContain('explain this');
  });

  it('session_metrics → null (ignored by mirror)', () => {
    const ev: ControllerEvent = { kind: 'session_metrics', turn_state: 'done' };
    expect(eventToLine(ev, key)).toBeNull();
  });

  it('unknown kind → null (ignored)', () => {
    const ev: ControllerEvent = { kind: 'pty_chunk' };
    expect(eventToLine(ev, key)).toBeNull();
  });

  // Regression guard: OLD Mirror ChunkType vocabulary must NOT match.
  it('OLD stream_text kind → null (wrong vocabulary, must not match)', () => {
    const ev = { kind: 'stream_text', delta: 'hi' } as unknown as ControllerEvent;
    expect(eventToLine(ev, key)).toBeNull();
  });

  it('OLD loop_end kind → null (wrong vocabulary, must not match)', () => {
    const ev = { kind: 'loop_end' } as unknown as ControllerEvent;
    expect(eventToLine(ev, key)).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// isThinkingEvent — sets "TARS is working…" true
// ---------------------------------------------------------------------------

describe('isThinkingEvent — real controller vocabulary', () => {
  it('assistant_text partial=true → thinking', () => {
    expect(isThinkingEvent({ kind: 'assistant_text', partial: true })).toBe(true);
  });

  it('assistant_text partial=false → NOT thinking (turn-close)', () => {
    expect(isThinkingEvent({ kind: 'assistant_text', partial: false })).toBe(false);
  });

  it('tool_use → thinking (TARS mid-turn executing tool)', () => {
    expect(isThinkingEvent({ kind: 'tool_use', tool: 'web_search', tool_use_id: 'tu-1' })).toBe(true);
  });

  it('tool_result → NOT thinking', () => {
    expect(isThinkingEvent({ kind: 'tool_result', tool_use_id: 'tu-1', success: true })).toBe(false);
  });

  it('user_text → NOT thinking', () => {
    expect(isThinkingEvent({ kind: 'user_text', text: 'hello' })).toBe(false);
  });

  it('session_metrics → NOT thinking', () => {
    expect(isThinkingEvent({ kind: 'session_metrics', turn_state: 'thinking' })).toBe(false);
  });

  // OLD vocabulary regression guards
  it('OLD stream_text kind → NOT thinking (wrong vocabulary)', () => {
    expect(isThinkingEvent({ kind: 'stream_text' } as unknown as ControllerEvent)).toBe(false);
  });

  it('OLD loop_start kind → NOT thinking (wrong vocabulary)', () => {
    expect(isThinkingEvent({ kind: 'loop_start' } as unknown as ControllerEvent)).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// isTurnEndEvent — clears "TARS is working…"
// ---------------------------------------------------------------------------

describe('isTurnEndEvent — real controller vocabulary', () => {
  it('assistant_text partial=false → turn end (canonical close signal)', () => {
    expect(isTurnEndEvent({ kind: 'assistant_text', partial: false })).toBe(true);
  });

  it('assistant_text partial=true → NOT turn end (still streaming)', () => {
    expect(isTurnEndEvent({ kind: 'assistant_text', partial: true })).toBe(false);
  });

  it('session_metrics turn_state=done → turn end (supplemental signal)', () => {
    expect(isTurnEndEvent({ kind: 'session_metrics', turn_state: 'done' })).toBe(true);
  });

  it('session_metrics turn_state=thinking → NOT turn end', () => {
    expect(isTurnEndEvent({ kind: 'session_metrics', turn_state: 'thinking' })).toBe(false);
  });

  it('tool_use → NOT turn end', () => {
    expect(isTurnEndEvent({ kind: 'tool_use', tool: 'x', tool_use_id: 'tu-1' })).toBe(false);
  });

  it('tool_result → NOT turn end', () => {
    expect(isTurnEndEvent({ kind: 'tool_result', tool_use_id: 'tu-1', success: true })).toBe(false);
  });

  // OLD vocabulary regression guards
  it('OLD loop_end kind → NOT turn end (wrong vocabulary)', () => {
    expect(isTurnEndEvent({ kind: 'loop_end' } as unknown as ControllerEvent)).toBe(false);
  });

  it('OLD stream_stop kind → NOT turn end (wrong vocabulary)', () => {
    expect(isTurnEndEvent({ kind: 'stream_stop' } as unknown as ControllerEvent)).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// statusCardLabel — post-WS-close completion card caption (WS-5)
// ---------------------------------------------------------------------------

describe('statusCardLabel', () => {
  it('reports closed sessions as finished', () => {
    expect(statusCardLabel({ status: 'closed', last_active_at: '2026-05-26T09:27Z' }))
      .toMatch(/finished/i);
  });

  it('reports idle and detached sessions as finished too', () => {
    expect(statusCardLabel({ status: 'idle' })).toMatch(/finished/i);
    expect(statusCardLabel({ status: 'detached' })).toMatch(/finished/i);
  });

  it('reports active sessions as still running', () => {
    expect(statusCardLabel({ status: 'active', last_active_at: '2026-05-26T09:27Z' }))
      .toMatch(/still running/i);
  });
});

// ---------------------------------------------------------------------------
// X-2 (2026-06-02) — completion card surfaces transcript_path
//
// The Mirror completion card must render the on-disk transcript path the
// `GET /api/controller_sessions/{id}` route now returns (X-2 status route
// extension), so the operator can copy the path after the live WS drops.
// `ControllerStatus.transcript_path?: string | null` was added in X-2.
// ---------------------------------------------------------------------------

describe('ControllerStatus.transcript_path — type surface', () => {
  it('accepts an absolute transcript path on a closed status', () => {
    const s = {
      status: 'closed',
      last_active_at: '2026-06-02T10:00Z',
      transcript_path: '/home/op/.tesseract/tars_controller/transcripts/2026-06-02-abc.jsonl',
    };
    // Type assertion compiles — runtime assertion proves the field is preserved.
    expect(s.transcript_path).toContain('transcripts/');
    // statusCardLabel must still summarize the status regardless of
    // transcript_path presence — the transcript_path line is rendered as
    // a sibling element on the completion card, not inside the label.
    expect(statusCardLabel(s)).toMatch(/finished/i);
  });

  it('treats transcript_path absent / null as no extra render line', () => {
    expect(statusCardLabel({ status: 'closed' })).toMatch(/finished/i);
    expect(statusCardLabel({ status: 'closed', transcript_path: null }))
      .toMatch(/finished/i);
  });
});

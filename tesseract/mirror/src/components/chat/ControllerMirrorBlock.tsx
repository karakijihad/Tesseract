import { useEffect, useRef, useReducer } from 'react';
import { BACKEND_BASE } from '../../lib/endpoints';
import './ControllerMirrorBlock.css';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

// Real controller transcript event vocabulary — see:
//   tesseract/orchestrator/tars_controller/events.py
// Discriminator is `kind` (not `type`). Known kinds used by this renderer:
//   assistant_text  – text: string, partial: bool (partial=false closes turn)
//   tool_use        – tool: string, input: dict, tool_use_id: string
//   tool_result     – tool_use_id: string, success: bool, output: any
//   user_text       – text: string (operator prompt, rendered dim)
//   session_metrics – turn_state: string (supplemental; assistant_text partial=false is canonical close)
//   …others are passed through as null (ignored)
// Exported so tests can construct typed event fixtures.
export interface ControllerEvent {
  kind: string;
  text?: string;
  partial?: boolean;
  tool?: string;
  tool_use_id?: string;
  success?: boolean;
  output?: unknown;
  turn_state?: string;
  [key: string]: unknown;
}

interface TranscriptLine {
  key: string;
  kind: 'text' | 'tool' | 'system';
  text: string;
}

// Single controller session status, fetched once on WS close so a detached
// session's outcome stays visible after a manual reload or real disconnect.
// X-2 (2026-06-02) extended with `transcript_path` so the completion card
// surfaces the on-disk path the operator can copy / open in `tars --session`.
export interface ControllerStatus {
  status: string;
  last_active_at?: string | null;
  transcript_path?: string | null;
}

interface State {
  lines: TranscriptLine[];
  thinking: boolean;
  error: string | null;
  connected: boolean;
  status: ControllerStatus | null;
}

type Action =
  | { type: 'connected' }
  | { type: 'disconnected' }
  | { type: 'error'; message: string }
  | { type: 'event'; event: ControllerEvent; key: string }
  | { type: 'thinking'; value: boolean }
  | { type: 'status'; status: ControllerStatus };

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

// Maps a fetched controller status payload to the completion-card caption.
// idle/detached/closed all mean the live turn is over; active means the
// session is still running (e.g. the operator reloaded mid-turn).
// Exported for unit-testing without a DOM.
export function statusCardLabel(s: ControllerStatus): string {
  if (s.status === 'closed' || s.status === 'idle' || s.status === 'detached') {
    return `Session finished (${s.status})${s.last_active_at ? ` · ${s.last_active_at}` : ''}`;
  }
  return `Session still running…${s.last_active_at ? ` · last active ${s.last_active_at}` : ''}`;
}

function controllerWsUrl(wsPath: string): string {
  const base = BACKEND_BASE;
  if (base.startsWith('https://')) return `wss://${base.slice('https://'.length)}${wsPath}`;
  if (base.startsWith('http://')) return `ws://${base.slice('http://'.length)}${wsPath}`;
  return `${base}${wsPath}`;
}

// Maps a real controller transcript event (keyed by `kind`) to a renderable
// line. Returns null for event kinds the mirror ignores (pty_chunk, cli_chunk,
// session_metrics, worker_status, etc.) — the reducer simply skips nulls.
//
// Streaming assistant_text: partial=true events produce text lines that the
// reducer merges into the last text line; partial=false finalises the line.
// The merge happens in the reducer (consecutive text lines → one node) so
// this function just returns the text fragment — no accumulation here.
//
// Exported for unit-testing the vocabulary mapping without a DOM.
export function eventToLine(event: ControllerEvent, key: string): TranscriptLine | null {
  const k = event.kind;

  if (k === 'assistant_text') {
    const text = event.text ?? '';
    if (!text) return null;
    return { key, kind: 'text', text };
  }

  if (k === 'tool_use') {
    const name = event.tool ?? 'tool';
    return { key, kind: 'tool', text: `→ ${name}` };
  }

  if (k === 'tool_result') {
    const ok = event.success !== false;
    return { key, kind: 'tool', text: ok ? '✓ tool done' : '✗ tool error' };
  }

  if (k === 'user_text') {
    // Show operator prompt dimly so the operator knows what triggered this turn.
    const text = event.text ?? '';
    if (!text) return null;
    return { key, kind: 'system', text: `> ${text}` };
  }

  return null;
}

// Sets thinking=true when TARS is actively producing output mid-turn.
// assistant_text with partial=true → streaming; tool_use → executing a tool.
// Exported for unit-testing.
export function isThinkingEvent(event: ControllerEvent): boolean {
  return (event.kind === 'assistant_text' && event.partial === true)
    || event.kind === 'tool_use';
}

// Clears thinking. Canonical signal: assistant_text with partial=false
// (the dispatcher's turn-close contract — dispatcher.py::tail_until_assistant_text).
// session_metrics with turn_state="done" is a supplemental signal.
// Exported for unit-testing.
export function isTurnEndEvent(event: ControllerEvent): boolean {
  if (event.kind === 'assistant_text' && event.partial === false) return true;
  if (event.kind === 'session_metrics' && event.turn_state === 'done') return true;
  return false;
}

// ---------------------------------------------------------------------------
// Reducer
// ---------------------------------------------------------------------------

function reducer(state: State, action: Action): State {
  switch (action.type) {
    case 'connected':
      return { ...state, connected: true, error: null };
    case 'disconnected':
      return { ...state, connected: false, thinking: false };
    case 'error':
      return { ...state, error: action.message, thinking: false };
    case 'thinking':
      return { ...state, thinking: action.value };
    case 'status':
      return { ...state, status: action.status };
    case 'event': {
      const line = eventToLine(action.event, action.key);
      let thinking: boolean;
      if (isTurnEndEvent(action.event)) {
        thinking = false;
      } else if (isThinkingEvent(action.event)) {
        thinking = true;
      } else {
        thinking = state.thinking;
      }
      if (!line) return { ...state, thinking };
      // Merge consecutive text lines to reduce DOM nodes.
      const lines = state.lines;
      const last = lines[lines.length - 1];
      if (line.kind === 'text' && last?.kind === 'text') {
        return {
          ...state,
          thinking,
          lines: [...lines.slice(0, -1), { ...last, text: last.text + line.text }],
        };
      }
      return { ...state, thinking, lines: [...lines, line] };
    }
    default:
      return state;
  }
}

const initialState: State = {
  lines: [],
  thinking: false,
  error: null,
  connected: false,
  status: null,
};

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

export interface ControllerMirrorBlockProps {
  session_id: string;
  ws_path: string;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function ControllerMirrorBlock({ session_id, ws_path }: ControllerMirrorBlockProps) {
  const [state, dispatch] = useReducer(reducer, initialState);
  const transcriptRef = useRef<HTMLDivElement | null>(null);
  const keyRef = useRef(0);

  // Auto-scroll to bottom on new lines
  const lineCount = state.lines.length;
  useEffect(() => {
    const el = transcriptRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [lineCount]);

  // WebSocket lifecycle
  useEffect(() => {
    const url = controllerWsUrl(ws_path);
    let ws: WebSocket;
    let cancelled = false;

    try {
      ws = new WebSocket(url);
    } catch (err) {
      dispatch({ type: 'error', message: String(err) });
      return () => { cancelled = true; };
    }

    ws.onopen = () => {
      if (!cancelled) dispatch({ type: 'connected' });
    };

    ws.onmessage = (ev) => {
      if (cancelled) return;
      let parsed: unknown;
      try {
        parsed = JSON.parse(ev.data as string);
      } catch {
        return;
      }

      const envelope = parsed as { type?: string; data?: unknown };
      if (envelope.type === 'controller_error') {
        const d = envelope.data as { detail?: string; code?: string } | undefined;
        dispatch({ type: 'error', message: d?.detail ?? d?.code ?? 'controller error' });
        return;
      }

      if (envelope.type === 'controller_event') {
        const event = envelope.data as ControllerEvent;
        const key = `e${keyRef.current++}`;
        dispatch({ type: 'event', event, key });
        return;
      }

      // Bare event (some backends emit unwrapped — controller events have `kind`)
      const bare = envelope as Record<string, unknown>;
      if (typeof bare.kind === 'string') {
        const key = `e${keyRef.current++}`;
        dispatch({ type: 'event', event: bare as unknown as ControllerEvent, key });
      }
    };

    ws.onerror = () => {
      if (!cancelled) dispatch({ type: 'error', message: 'WebSocket connection failed' });
    };

    ws.onclose = () => {
      if (cancelled) return;
      dispatch({ type: 'disconnected' });
      // One-shot status fetch so a detached session's outcome stays visible
      // after the live WS drops (manual reload or real disconnect).
      fetch(`${BACKEND_BASE}/api/controller_sessions/${session_id}`)
        .then((r) => (r.ok ? r.json() : null))
        .then((s) => { if (!cancelled && s) dispatch({ type: 'status', status: s }); })
        .catch(() => {});
    };

    return () => {
      cancelled = true;
      if (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING) {
        ws.close();
      }
    };
  }, [ws_path, session_id]);

  return (
    <div className="controller-mirror">
      {/* Handoff card */}
      <div className="controller-mirror__handoff">
        <span className="controller-mirror__handoff-label">launched terminal session</span>
        <span className="controller-mirror__session-id">{session_id}</span>
        <span className="controller-mirror__hint">tars --session {session_id}</span>
      </div>

      {/* Thinking indicator */}
      {state.thinking && (
        <div className="controller-mirror__thinking">
          <span className="controller-mirror__thinking-dot" />
          TARS is working…
        </div>
      )}

      {/* Error banner */}
      {state.error && (
        <div className="controller-mirror__error">{state.error}</div>
      )}

      {/* Completion card — shown after the live WS closes (reload / disconnect) */}
      {!state.connected && state.status && (
        <div className="controller-mirror__status">
          <div>{statusCardLabel(state.status)}</div>
          {state.status.transcript_path && (
            <div className="controller-mirror__status-path t-meta">
              transcript: {state.status.transcript_path}
            </div>
          )}
        </div>
      )}

      {/* Transcript mirror */}
      <div ref={transcriptRef} className="controller-mirror__transcript">
        {state.lines.map(line => (
          <div
            key={line.key}
            className={`controller-mirror__event controller-mirror__event--${line.kind}`}
          >
            {line.text}
          </div>
        ))}
      </div>
    </div>
  );
}

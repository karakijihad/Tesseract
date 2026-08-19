import { create } from 'zustand';

import type { Envelope, EnvelopeCategory, PulseTag } from '../lib/types';

export type PulseSeverity = 'ok' | 'warn' | 'bad';

export interface PulseEntry {
  id: string;
  ts: string;
  tag: PulseTag;
  category: EnvelopeCategory;
  label: string;
  severity: PulseSeverity;
  // Set only for coalesced stream_text rows.
  turn_id?: string;
  delta_count?: number;
  first_ts?: string;
  last_preview?: string;
  // `accumulated` is capped at COALESCE_ACCUMULATED_CAP — consumers MUST
  // treat it as "most recent N chars of the stream," not a full transcript.
  // `truncated` is true once any chars have been dropped from the head, and
  // `total_chars` tracks the real length delivered by the stream.
  accumulated?: string;
  truncated?: boolean;
  total_chars?: number;
}

const COALESCE_ACCUMULATED_CAP = 5_000;
const COALESCE_PREVIEW_CAP = 60;

// Operator-controlled retention for the rolling pulse buffer (F2 §5l).
// 'all' is unbounded — surfaced with a perf warning in the UI because long
// idle sessions can balloon the array. Persisted to `pulse_cap`.
export type PulseCapValue = 100 | 500 | 'all';
const PULSE_CAP_KEY = 'pulse_cap';
// Phase 15 audit reconciled this against the spec (100, not 500). Operators
// can still raise the cap to 500 or 'all' via the Pulse view toggle.
const PULSE_CAP_DEFAULT: PulseCapValue = 100;

// Y-3 — the 12 pulse tags live in the store so the (now separate) filter
// card and stream card share one filter state. `enabledTags === null` means
// "all tags on" (the default); a Set means "only these are on".
export const ALL_PULSE_TAGS: PulseTag[] = [
  'triage', 'tool', 'memory', 'agent', 'model', 'system',
  'chat', 'perm', 'route', 'loop', 'bg', 'other',
];

interface PulseStoreState {
  entries: PulseEntry[];
  cap: PulseCapValue;
  currentTurnId: string | null;
  // Filter state — shared by the pulse-filters and pulse-stream surface cards.
  enabledTags: Set<PulseTag> | null;
  errorsOnly: boolean;
  push: (env: Envelope) => void;
  beginTurn: (turnId: string) => void;
  clear: () => void;
  setCap: (cap: PulseCapValue) => void;
  toggleTag: (tag: PulseTag) => void;
  setErrorsOnly: (v: boolean) => void;
  resetFilter: () => void;
}

function makeId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  return `${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

function loadCap(): PulseCapValue {
  if (typeof window === 'undefined') return PULSE_CAP_DEFAULT;
  try {
    const raw = window.localStorage.getItem(PULSE_CAP_KEY);
    if (raw === 'all') return 'all';
    if (raw === '500') return 500;
    if (raw === '100') return 100;
  } catch {
    // localStorage unavailable (private mode, quota) — fall through to default.
  }
  return PULSE_CAP_DEFAULT;
}

function persistCap(cap: PulseCapValue): void {
  if (typeof window === 'undefined') return;
  try {
    window.localStorage.setItem(PULSE_CAP_KEY, String(cap));
  } catch {
    // ignore persistence failures — the in-memory cap still applies
  }
}

function capLimit(cap: PulseCapValue): number {
  return cap === 'all' ? Number.POSITIVE_INFINITY : cap;
}

function deriveTag(env: Envelope): PulseTag {
  const t = env.type;
  if (t === 'model_selected') return 'model';
  if (t === 'mode_changed') return 'route';
  if (t === 'identity_changed') return 'system';
  if (t === 'stream_stop' || t === 'loop_end' || t === 'loop_start') return 'loop';
  if (t === 'memory_suggestion') return 'memory';
  if (t === 'tool_ask' || t === 'tool_approved' || t === 'tool_denied' || t === 'tool_denied_hard') {
    return 'perm';
  }
  if (t.startsWith('tool_') || t.startsWith('stream_tool_')) return 'tool';
  if (t === 'soul_updated') return 'system';
  if (t.startsWith('schedule_')) return 'bg';

  switch (env.category) {
    case 'loop':           return 'chat';
    case 'session':        return 'system';
    case 'planning':       return 'triage';
    case 'routing':        return 'route';
    case 'execution':      return 'tool';
    case 'offlocal':       return 'agent';
    case 'cli':            return 'agent';
    case 'sandbox':        return 'tool';
    case 'error':          return 'system';
    case 'background':     return 'bg';
    case 'entity':         return 'other';
    case 'terminal':       return 'other';
    case 'command_result': return 'system';
    case 'voice':          return 'system';
    default:               return 'other';
  }
}

function deriveSeverity(env: Envelope): PulseSeverity {
  const t = env.type;
  if (t === 'tool_denied' || t === 'tool_denied_hard' || t === 'stream_error') return 'bad';
  if (t === 'observer_unavailable' || t === 'error') return 'bad';
  if (t === 'log_error') {
    // The envelope has always carried `level`; this is the first thing to read
    // it. Forwarded WARNING records are operational facts, not failures — a
    // stale worker belongs in the feed but not in the errors-only view beside
    // real crashes. Same shape as `command_result` below.
    const level = (env.data as { level?: unknown })?.level;
    return level === 'WARNING' ? 'warn' : 'bad';
  }
  if (t === 'cli_end') {
    const exit = (env.data as { exit_code?: unknown })?.exit_code;
    if (typeof exit === 'number' && exit !== 0) return 'bad';
  }
  if (t === 'command_result') {
    const sev = (env.data as { severity?: unknown })?.severity;
    if (sev === 'error') return 'bad';
    if (sev === 'warning') return 'warn';
  }
  if (t === 'schedule_job_failed') return 'bad';
  if (t === 'schedule_job_done') {
    const data = env.data as { ok?: unknown; job_name?: unknown; payload?: unknown } | null | undefined;
    if (data?.ok === false) return 'bad';
    if (data?.job_name === 'vault_lint' && vaultLintSummary(data) !== null) return 'warn';
  }
  if (t === 'schedule_alarm_fired') return 'warn';
  if (t === 'tool_ask' || t === 'mode_changed') return 'warn';
  if (t === 'model_selected') {
    const isFallback = (env.data as { is_fallback?: unknown })?.is_fallback;
    // Fallback transitions are surfaced as `bad` so they appear in the
    // operator's errors-only filter — a fallback firing is exactly the
    // kind of thing they want to notice without scanning the model tag.
    if (isFallback === true) return 'bad';
  }
  return 'ok';
}

function nameOf(data: unknown): string | undefined {
  const n = (data as { name?: unknown })?.name;
  return typeof n === 'string' ? n : undefined;
}

// Returns null when the payload is clean or unreadable — callers keep the
// default label/severity in that case. Payload shape is the VaultLintReport
// asdict() in tesseract/kernel/tools/vault_lint.py::_report_to_json.
function vaultLintSummary(data: unknown): string | null {
  const payload = (data as { payload?: unknown } | null | undefined)?.payload;
  if (!payload || typeof payload !== 'object') return null;
  const p = payload as Record<string, unknown>;
  const len = (v: unknown): number => (Array.isArray(v) ? v.length : 0);
  const parts: string[] = [];
  const c = len(p.contradictions); if (c > 0) parts.push(`${c} contradiction${c === 1 ? '' : 's'}`);
  const o = len(p.orphans);        if (o > 0) parts.push(`${o} orphan${o === 1 ? '' : 's'}`);
  const s = len(p.stale);          if (s > 0) parts.push(`${s} stale`);
  const m = len(p.missing_hubs);   if (m > 0) parts.push(`${m} missing hub${m === 1 ? '' : 's'}`);
  if (p.scale_alarm) parts.push('scale=alarm');
  return parts.length === 0 ? null : parts.join(', ');
}

function deriveLabel(env: Envelope): string {
  const t = env.type;
  const d = env.data ?? {};
  switch (t) {
    case 'stream_tool_call_end': {
      const n = nameOf(d); return n ? `tool: ${n}` : t;
    }
    case 'tool_ask': {
      const n = nameOf(d); return n ? `ask: ${n}` : t;
    }
    case 'tool_approved': {
      const n = nameOf(d); return n ? `approved: ${n}` : t;
    }
    case 'tool_denied': {
      const n = nameOf(d); return n ? `denied: ${n}` : t;
    }
    case 'tool_auto': {
      const n = nameOf(d); return n ? `auto: ${n}` : t;
    }
    case 'tool_denied_hard': {
      const n = nameOf(d); return n ? `hard-deny: ${n}` : t;
    }
    case 'model_selected': {
      const m = (d as { model?: unknown }).model;
      const isFallback = (d as { is_fallback?: unknown }).is_fallback === true;
      const primary = (d as { primary?: { provider?: string; model?: string } }).primary;
      const reason = (d as { fallback_reason?: unknown }).fallback_reason;
      if (isFallback && typeof m === 'string') {
        const from = primary
          ? `${primary.provider ?? '?'}/${primary.model ?? '?'}`
          : 'primary';
        const reasonStr = typeof reason === 'string' && reason
          ? ` — ${truncate(reason, 80)}`
          : '';
        return `fallback: ${from} → ${m}${reasonStr}`;
      }
      return typeof m === 'string' ? `model: ${m}` : t;
    }
    case 'mode_changed': {
      const to = (d as { to?: unknown }).to;
      return typeof to === 'string' ? `mode → ${to}` : t;
    }
    case 'identity_changed': {
      const name = (d as { name?: unknown }).name;
      return typeof name === 'string' ? `identity → ${name}` : t;
    }
    case 'session_compact': {
      const before = (d as { tokens_before?: unknown }).tokens_before;
      const after  = (d as { tokens_after?: unknown }).tokens_after;
      if (typeof before === 'number' && typeof after === 'number') {
        return `compact ${before}→${after}`;
      }
      return t;
    }
    case 'cli_start': {
      const tool = (d as { tool?: unknown }).tool;
      return typeof tool === 'string' ? `cli: ${tool}` : t;
    }
    case 'cli_end': {
      const ex = (d as { exit_code?: unknown }).exit_code;
      return typeof ex === 'number' ? `cli end (exit ${ex})` : t;
    }
    case 'stream_error': {
      const reason = (d as { reason?: unknown; message?: unknown }).reason
                  ?? (d as { message?: unknown }).message;
      return typeof reason === 'string' ? `error: ${reason}` : t;
    }
    case 'command_result': {
      const cmd = (d as { command?: unknown }).command;
      const reason = (d as { reason?: unknown }).reason;
      const cmdStr = typeof cmd === 'string' ? cmd : 'cmd';
      const reasonStr = typeof reason === 'string' ? reason : 'failed';
      return `${cmdStr}: ${reasonStr}`;
    }
    case 'schedule_job_started': {
      const n = (d as { job_name?: unknown }).job_name;
      return typeof n === 'string' ? `scheduled: ${n} started` : t;
    }
    case 'schedule_job_done': {
      const n = (d as { job_name?: unknown }).job_name;
      const ok = (d as { ok?: unknown }).ok;
      if (typeof n !== 'string') return t;
      if (ok === false) return `scheduled: ${n} failed`;
      if (n === 'vault_lint') {
        const summary = vaultLintSummary(d);
        if (summary) return `vault_lint: ${summary}`;
      }
      return `scheduled: ${n} done`;
    }
    case 'schedule_job_failed': {
      const n = (d as { job_name?: unknown }).job_name;
      return typeof n === 'string' ? `scheduled: ${n} error` : t;
    }
    case 'schedule_alarm_fired': {
      const n = (d as { alarm_name?: unknown }).alarm_name;
      return typeof n === 'string' ? `alarm: ${n}` : t;
    }
    case 'schedule_state': {
      const n = (d as { name?: unknown }).name;
      const action = (d as { action?: unknown }).action;
      if (typeof n === 'string' && typeof action === 'string') return `schedule ${action}: ${n}`;
      return t;
    }
    // ── Voice envelopes — surface details so the pulse feed isn't a
    // wall of `voice_state` / `voice_final` tags. Each label keeps to
    // ~60 chars to stay on a single row.
    case 'voice_state': {
      const s = (d as { state?: unknown }).state;
      return typeof s === 'string' ? `voice: ${s}` : t;
    }
    case 'voice_final': {
      const text = (d as { text?: unknown }).text;
      if (typeof text !== 'string') return t;
      return text ? `voice final: "${truncate(text, 56)}"` : 'voice final: (empty)';
    }
    case 'voice_discarded': {
      // The length is what the row is for. There is no score to show — the
      // decoder hears the phrase or it does not — and the duration is the
      // distinction that matters anyway: two seconds is a gate too tight,
      // thirty is the mic hearing the room.
      const seconds = (d as { audio_seconds?: unknown }).audio_seconds;
      return typeof seconds === 'number'
        ? `no wake word (${seconds.toFixed(1)}s)`
        : 'no wake word';
    }
    case 'tts_chunk': {
      const seq = (d as { sequence?: unknown }).sequence;
      const isFinal = (d as { is_final?: unknown }).is_final;
      const provider = (d as { provider?: unknown }).provider;
      const seqStr = typeof seq === 'number' ? `#${seq}` : '';
      const finalStr = isFinal ? ' final' : '';
      const provStr = typeof provider === 'string' && provider ? ` (${provider})` : '';
      return `tts ${seqStr}${finalStr}${provStr}`.trim();
    }
    case 'voice_instruction': {
      const instr = (d as { instruction?: unknown }).instruction;
      if (typeof instr === 'string' && instr) return `voice: ${truncate(instr, 60)}`;
      return t;
    }
    case 'voice_mode_set': {
      const m = (d as { mode?: unknown }).mode;
      return typeof m === 'string' ? `voice mode: ${m}` : t;
    }
    case 'log_error': {
      const logger = (d as { logger?: unknown }).logger;
      const message = (d as { message?: unknown }).message;
      const excType = (d as { exc_type?: unknown }).exc_type;
      const loggerStr = typeof logger === 'string' ? logger.split('.').pop() ?? logger : 'log';
      const excStr = typeof excType === 'string' && excType ? `${excType}: ` : '';
      const msgStr = typeof message === 'string' ? message : t;
      return `error[${loggerStr}] ${excStr}${truncate(msgStr, 80)}`;
    }
    default:
      return t;
  }
}

function truncate(s: string, max: number): string {
  return s.length > max ? `${s.slice(0, max - 1).trimEnd()}…` : s;
}

export const usePulseStore = create<PulseStoreState>((set, get) => ({
  entries: [],
  cap: loadCap(),
  currentTurnId: null,
  enabledTags: null,
  errorsOnly: false,
  push: (env) =>
    set((s) => {
      // entity_signals is a 2s telemetry pump for orb mood/valence — pure
      // UI-internal signal. It would dominate the feed under any idle
      // session and carries no operator-facing information. Skip entirely.
      if (env.type === 'entity_signals') return s;

      // cli_output fires per subprocess line. The DelegateCard already
      // shows the full stream — pulse is a high-level signal feed, not
      // a subprocess transcript. cli_start + cli_end still bracket the
      // call so the operator sees the spawn lifecycle in pulse.
      if (env.type === 'cli_output') return s;

      const limit = capLimit(s.cap);

      // Coalesce consecutive same-turn stream_text into one row. stream_text
      // fires per adapter chunk (often 1–3 chars); without this a 200-word
      // response produces 150+ rows of noise. Envelope data field is `delta`
      // (set by envelope.py::_chunk_data for ChunkType.TEXT).
      //
      // Pulse is a signal feed, not a transcript — `accumulated` is capped at
      // COALESCE_ACCUMULATED_CAP chars (tail-preserved, so the most recent
      // stream stays visible). When the cap is hit, `truncated=true` flags
      // the head drop and `total_chars` records the real length for any UI
      // surface that needs to show "N chars streamed (last M shown)".
      if (env.type === 'stream_text' && s.currentTurnId) {
        const text = String((env.data as { delta?: unknown })?.delta ?? '');
        const head = s.entries[0];
        if (head && head.turn_id === s.currentTurnId) {
          const totalChars = (head.total_chars ?? (head.accumulated?.length ?? 0)) + text.length;
          const grown = (head.accumulated ?? '') + text;
          const truncated = (head.truncated ?? false) || grown.length > COALESCE_ACCUMULATED_CAP;
          const accumulated =
            grown.length > COALESCE_ACCUMULATED_CAP
              ? grown.slice(-COALESCE_ACCUMULATED_CAP)
              : grown;
          const delta_count = (head.delta_count ?? 1) + 1;
          const updated: PulseEntry = {
            ...head,
            delta_count,
            last_preview: accumulated.slice(-COALESCE_PREVIEW_CAP),
            accumulated,
            truncated,
            total_chars: totalChars,
            label: 'stream',
          };
          return { entries: [updated, ...s.entries.slice(1)] };
        }
        const truncated = text.length > COALESCE_ACCUMULATED_CAP;
        const accumulated = truncated ? text.slice(-COALESCE_ACCUMULATED_CAP) : text;
        const entry: PulseEntry = {
          id: makeId(),
          ts: env.timestamp,
          tag: 'chat',
          category: 'loop',
          label: 'stream',
          severity: 'ok',
          turn_id: s.currentTurnId,
          delta_count: 1,
          first_ts: env.timestamp,
          last_preview: accumulated.slice(-COALESCE_PREVIEW_CAP),
          accumulated,
          truncated,
          total_chars: text.length,
        };
        const nextArr = [entry, ...s.entries];
        const entries = Number.isFinite(limit) ? nextArr.slice(0, limit) : nextArr;
        return { entries };
      }

      const entry: PulseEntry = {
        id: makeId(),
        ts: env.timestamp,
        tag: deriveTag(env),
        category: env.category,
        label: deriveLabel(env),
        severity: deriveSeverity(env),
      };
      const next = [entry, ...s.entries];
      const entries = Number.isFinite(limit) ? next.slice(0, limit) : next;
      return { entries };
    }),
  beginTurn: (turnId) => set({ currentTurnId: turnId }),
  clear: () => set({ entries: [], currentTurnId: null }),
  setCap: (cap) => {
    persistCap(cap);
    const limit = capLimit(cap);
    const current = get().entries;
    const entries = Number.isFinite(limit) ? current.slice(0, limit) : current;
    set({ cap, entries });
  },
  toggleTag: (tag) =>
    set((s) => {
      // Switching from "all" to a single tag unchecked → enable the rest.
      if (s.enabledTags === null) {
        const next = new Set(ALL_PULSE_TAGS);
        next.delete(tag);
        return { enabledTags: next };
      }
      const next = new Set(s.enabledTags);
      if (next.has(tag)) next.delete(tag);
      else next.add(tag);
      return { enabledTags: next.size === ALL_PULSE_TAGS.length ? null : next };
    }),
  setErrorsOnly: (v) => set({ errorsOnly: v }),
  resetFilter: () => set({ enabledTags: null, errorsOnly: false }),
}));

// Slash-command palette — backend-driven.
//
// The list is hydrated from `GET /api/commands` (served by the unified
// `commands_registry` on the Mirror backend). It includes both Mirror
// session ops (`source: 'mirror_session'`) and every kernel tool
// (`source: 'kernel_tool'`) the operator can call from chat.
//
// Hydration: `loadSlashCommands()` is called once on app mount from
// `App.tsx`. Until it resolves, autocomplete returns nothing and slash
// dispatch is optimistic (the backend rejects unknown names). Module
// state caches the result for the lifetime of the page.

import { BACKEND_BASE } from './endpoints';

export type SlashCommandSource = 'mirror_session' | 'kernel_tool';

export interface SlashCommandDef {
  name: string;
  summary: string;
  source: SlashCommandSource;
  aliases: string[];
  argLabel: string | null;
  argHelp: string | null;
  mutatesSession: boolean;
  // Derived: whether picking the hint should leave a trailing space for an
  // arg (vs auto-sending the bare command).
  takesArg: boolean;
}

interface RawSpec {
  name: string;
  summary: string;
  source: SlashCommandSource;
  aliases: string[];
  arg_label: string | null;
  arg_help: string | null;
  mutates_session: boolean;
}

let _commands: SlashCommandDef[] = [];
let _byName: Map<string, SlashCommandDef> = new Map();
let _loaded = false;
let _loading: Promise<void> | null = null;

function _normalize(head: string): string {
  return head.toLowerCase().replace(/-/g, '_');
}

// Backoff schedule for retrying /api/commands while the backend's stage-3
// startup (which builds command_registry) is still in flight. The Mirror's
// stages 1+2 typically finish in <2s but stage 3 can take 5-15s on cold
// boot — so we retry up to ~30s before giving up. A 503 from the route
// means "not ready, retry"; any other failure (network down, 5xx, non-
// JSON) drops to the silent-failure path so chat still works.
const _RETRY_DELAYS_MS = [400, 600, 900, 1300, 1900, 2800, 4200, 6300, 9400];

function _sleep(ms: number): Promise<void> {
  return new Promise((res) => setTimeout(res, ms));
}

export async function loadSlashCommands(): Promise<void> {
  if (_loaded) return;
  if (_loading) return _loading;
  _loading = (async () => {
    try {
      for (let attempt = 0; attempt <= _RETRY_DELAYS_MS.length; attempt++) {
        const r = await fetch(`${BACKEND_BASE}/api/commands`);
        if (r.status === 503) {
          // Registry not built yet — retry after backoff.
          if (attempt < _RETRY_DELAYS_MS.length) {
            await _sleep(_RETRY_DELAYS_MS[attempt]);
            continue;
          }
          throw new Error(`/api/commands still 503 after ${attempt} attempts — backend registry never came up`);
        }
        if (!r.ok) throw new Error(`/api/commands HTTP ${r.status}`);
        const ctype = r.headers.get('content-type') || '';
        if (!ctype.includes('application/json')) {
          throw new Error(`/api/commands non-JSON response (got ${ctype || 'no content-type'}) — backend likely unreachable`);
        }
        const data = await r.json();
        const raw: RawSpec[] = data.commands || [];
        // Empty list with HTTP 200 used to be a sticky failure mode (the
        // registry race wrote `_loaded=true` with zero commands forever).
        // Stage-3 always produces 21+ specs, so an empty 200 means the
        // registry is built-but-empty, which shouldn't happen — treat it
        // as a retryable not-ready instead of caching the broken state.
        if (raw.length === 0) {
          if (attempt < _RETRY_DELAYS_MS.length) {
            console.warn(`[slashCommands] /api/commands returned 0 specs (attempt ${attempt + 1}) — retrying`);
            await _sleep(_RETRY_DELAYS_MS[attempt]);
            continue;
          }
          // Exhausted retries with an empty 200 — registry still empty
          // after ~28s. Throw rather than caching `_loaded=true` with
          // zero specs (the original sticky-cache bug). On next call
          // `_loading` will be null and a fresh retry cycle will run.
          throw new Error(`/api/commands returned 0 specs after ${attempt + 1} attempts — registry never populated`);
        }
        const defs: SlashCommandDef[] = raw.map((c) => ({
          name: c.name,
          summary: c.summary || '',
          source: c.source,
          aliases: c.aliases || [],
          argLabel: c.arg_label,
          argHelp: c.arg_help,
          mutatesSession: !!c.mutates_session,
          takesArg: !!c.arg_label,
        }));
        _commands = defs;
        const byName = new Map<string, SlashCommandDef>();
        for (const d of defs) {
          byName.set(_normalize(d.name), d);
          for (const a of d.aliases) {
            byName.set(_normalize(a), d);
          }
        }
        _byName = byName;
        _loaded = true;
        return;
      }
    } catch (err) {
      console.error('[slashCommands] load failed', err);
    } finally {
      _loading = null;
    }
  })();
  return _loading;
}

export function isSlashCommandsLoaded(): boolean {
  return _loaded;
}

export function lookupCommand(head: string): SlashCommandDef | null {
  if (!_loaded) return null;
  const stripped = head.startsWith('/') ? head.slice(1) : head;
  return _byName.get(_normalize(stripped)) || null;
}

export interface ParsedSlash {
  kind: 'command' | 'chat';
  cmd?: string;
  rawText: string;
}

export function parseSlashInput(text: string): ParsedSlash {
  if (text.length === 0 || text[0] !== '/') {
    return { kind: 'chat', rawText: text };
  }
  const space = text.indexOf(' ');
  const head = text.slice(1, space === -1 ? undefined : space);
  // Optimistic until the registry loads — the backend rejects unknown
  // names. After load, only registered commands route as commands; the
  // rest fall through to "unknown command" handling in ChatInput.
  if (!_loaded) {
    return { kind: 'command', cmd: text, rawText: text };
  }
  if (!lookupCommand(head)) {
    return { kind: 'chat', rawText: text };
  }
  return { kind: 'command', cmd: text, rawText: text };
}

export function stripQuoteEscape(text: string): string {
  const trimmed = text.trimStart();
  if (trimmed.startsWith('"/') && trimmed.endsWith('"') && trimmed.length >= 3) {
    return trimmed.slice(1, -1);
  }
  return text;
}

export function matchingCommands(query: string): SlashCommandDef[] {
  if (!_loaded) return [];
  const q = _normalize(query);
  const seen = new Set<string>();
  const out: SlashCommandDef[] = [];
  for (const d of _commands) {
    const candidates = [d.name, ...d.aliases].map(_normalize);
    if (candidates.some((c) => c.startsWith(q))) {
      if (seen.has(d.name)) continue;
      seen.add(d.name);
      out.push(d);
    }
  }
  // Mirror session ops first (operator-curated chat commands), then kernel
  // tools sorted alphabetically — the dominant set.
  out.sort((a, b) => {
    if (a.source !== b.source) return a.source === 'mirror_session' ? -1 : 1;
    return a.name.localeCompare(b.name);
  });
  return out;
}

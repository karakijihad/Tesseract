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

// Retry cadence for `/api/commands` while the backend's stage-3 startup
// (which builds command_registry) is still in flight. Doubles from 400ms,
// caps at 5s, then keeps polling at that interval for the life of the page.
//
// This was a fixed 9-step schedule totalling ~28s, after which the loader
// gave up silently and cached nothing. Measured packaged-install boot is
// ~39s from backend spawn to `command_registry: N specs ready`, so the
// budget expired before the registry existed and the palette stayed empty
// for the lifetime of the page: typing `/` showed no list while commands
// still dispatched, because `parseSlashInput` stays optimistic while
// unloaded. The supervisor also respawns the backend after a crash, so any
// fixed budget is eventually wrong. One GET per 5s at a local port is cheap.
const _RETRY_BASE_MS = 400;
const _RETRY_MAX_MS = 5_000;

function _sleep(ms: number): Promise<void> {
  return new Promise((res) => setTimeout(res, ms));
}

export async function loadSlashCommands(): Promise<void> {
  if (_loaded) return;
  if (_loading) return _loading;
  _loading = (async () => {
    let delay = _RETRY_BASE_MS;
    let warned = false;
    for (;;) {
      try {
        const r = await fetch(`${BACKEND_BASE}/api/commands`);
        // 503 is the route's explicit "registry not built yet".
        if (r.ok) {
          const ctype = r.headers.get('content-type') || '';
          if (!ctype.includes('application/json')) {
            throw new Error(`/api/commands non-JSON response (got ${ctype || 'no content-type'})`);
          }
          const data = await r.json();
          const raw: RawSpec[] = data.commands || [];
          // An empty list with HTTP 200 means built-but-empty, which should
          // not happen — keep retrying rather than caching the broken state.
          if (raw.length > 0) {
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
            const byName = new Map<string, SlashCommandDef>();
            for (const d of defs) {
              byName.set(_normalize(d.name), d);
              for (const a of d.aliases) {
                byName.set(_normalize(a), d);
              }
            }
            _commands = defs;
            _byName = byName;
            _loaded = true;
            return;
          }
        }
      } catch (err) {
        // Backend down, restarting, or unreachable — same handling as a 503.
        // Warn once so a permanently broken backend leaves a trace without
        // spamming the console every 5s.
        if (!warned) {
          warned = true;
          console.warn('[slashCommands] /api/commands unavailable — retrying', err);
        }
      }
      await _sleep(delay);
      delay = Math.min(delay * 2, _RETRY_MAX_MS);
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

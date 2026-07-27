import type { Envelope } from './types';

const VALID_CATEGORIES = new Set<string>([
  'loop', 'session', 'planning', 'routing', 'execution',
  'offlocal', 'cli', 'terminal', 'sandbox', 'error', 'background', 'entity',
  'command_result', 'command', 'workspace', 'agenda', 'workers',
  'governor', 'schedule', 'voice', 'cost', 'canvas', 'activity',
  'chat', 'controller', 'other',
]);

export function isEnvelope(x: unknown): x is Envelope {
  if (typeof x !== 'object' || x === null) return false;
  if (typeof (x as any).type !== 'string') return false;
  if (!('data' in (x as any))) return false;
  if (!VALID_CATEGORIES.has((x as any).category)) return false;
  if (typeof (x as any).session_id !== 'string') return false;
  if (typeof (x as any).timestamp !== 'string') return false;
  if ('payload' in (x as any)) {
    console.warn('envelope uses payload not data — backend regression?');
  }
  return true;
}

export function parseTimestamp(iso: string): Date {
  return new Date(iso.endsWith('Z') ? iso : iso + 'Z');
}

// Pulse-tag mapping lives in `src/stores/pulse.ts::deriveTag` — type-aware,
// single source of truth. This module intentionally does not export a
// parallel category→tag map.

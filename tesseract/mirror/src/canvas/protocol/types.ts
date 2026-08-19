// Y-2 — Surface Protocol v1 TypeScript contract. The Python twin is
// `tesseract/orchestrator/surfaces/descriptor.py`; keep the two in sync
// (the back-compat guard test locks the version on the backend side).
//
// Wire shape is intentionally loose on `type` (a `string`): the backend
// accepts any type and the renderer registry resolves a fallback for
// unknown ones, so a tool can introduce a new surface type without a
// frontend release. `KNOWN_SURFACE_TYPES` is just the set we ship
// renderers for.

export const SURFACE_SCHEMA_VERSION = 1 as const;

export type SurfaceMode = 'embedded' | 'external' | 'canvas' | 'background';

export interface SurfacePosition {
  x: number;
  y: number;
}

export interface SurfaceSize {
  w: number;
  h: number;
}

export interface BoundSession {
  kind: string;
  id: string;
}

export interface SurfaceDescriptor {
  schema_version: number;
  id: string;
  type: string;
  view: string;
  position: SurfacePosition;
  size: SurfaceSize;
  title?: string | null;
  mode?: SurfaceMode;
  z?: number;
  locked?: boolean;
  props?: Record<string, unknown>;
  bound_session?: BoundSession | null;
  created_at_utc: string;
  updated_at_utc: string;
}

// The 8 reference renderers Y-2 ships, plus the runtime/visualization slots
// later phases fill. Unknown types fall back to the JSON-dump card.
export const KNOWN_SURFACE_TYPES = [
  'folder',
  'file',
  'webview',
  'terminal',
  'code',
  'markdown',
  'html',
  'json',
  // `open` renders these directly rather than handing the file to the OS.
  'pdf',
  'video',
  'audio',
  'table',
  // CV-1 runtime objects.
  'lane',
  // Y-3 views-as-canvases applets.
  'pulse-stream',
  'pulse-filters',
  'terminal-host',
  'delegate-transcript',
  'session-transcript',
] as const;

export type KnownSurfaceType = (typeof KNOWN_SURFACE_TYPES)[number];

// The 11 protocol verbs (documentation-level; the tool→canvas half arrives
// as the WS event kinds below, the canvas→tool half via `events.ts`).
export const SURFACE_VERBS = [
  'create',
  'update',
  'move',
  'resize',
  'focus',
  'close',
  'lock',
  'highlight',
  'open_external',
  'bind_session',
  'emit_event',
] as const;
export type SurfaceVerb = (typeof SURFACE_VERBS)[number];

// WS event kinds the backend SurfaceStore publishes (tool → canvas).
export type SurfaceEventKind =
  | 'surface_created'
  | 'surface_updated'
  | 'surface_focused'
  | 'surface_closed'
  | 'surface_locked'
  | 'surface_highlighted'
  | 'surface_bound';

// Operator interaction events (canvas → tool), POSTed back to the backend.
export type OperatorEvent =
  | 'moved'
  | 'resized'
  | 'closed'
  | 'clicked'
  | 'edited'
  | 'highlighted';

// What a card reports about its own drawing (canvas → tool). Separate from
// OperatorEvent on purpose: those are things the operator did and they
// persist geometry, this is the renderer talking about itself and the
// backend keeps it in memory only. `mounted` is deliberately weak — it means
// the renderer mounted and reported no failure, not that the pixels are
// right; anything stronger would be the circular check this replaces.
export type SurfaceRenderStatus =
  | 'mounted'
  | 'degraded'
  | 'errored'
  | 'unmounted';

export type ReportRender = (
  status: SurfaceRenderStatus,
  detail?: string,
) => void;

// A descriptor is renderable only at the version this build speaks. A v2
// descriptor is rejected (forward-incompatible by design, GOVERNANCE Rule 7).
export function isSupportedDescriptor(d: unknown): d is SurfaceDescriptor {
  if (typeof d !== 'object' || d === null) return false;
  const o = d as Record<string, unknown>;
  return (
    o.schema_version === SURFACE_SCHEMA_VERSION &&
    typeof o.id === 'string' &&
    typeof o.type === 'string' &&
    typeof o.view === 'string' &&
    typeof o.position === 'object' &&
    typeof o.size === 'object'
  );
}

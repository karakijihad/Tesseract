// Y-2 — Surface Protocol canvas → tool half (`surface.emit_event`). When
// the operator drags / resizes / closes a card, the SurfaceLayer calls one
// of these to POST the interaction back to the backend, which persists the
// new geometry (so a reload re-renders where the operator left it) and, for
// `closed`, removes the surface.
//
// Fire-and-forget: a dropped emit is non-fatal — the next interaction
// re-sends.

import { BACKEND_BASE } from '../../lib/endpoints';
import type { OperatorEvent } from './types';

function eventUrl(view: string): string {
  return `${BACKEND_BASE}/api/surfaces/${encodeURIComponent(view)}/event`;
}

export async function emitSurfaceEvent(
  view: string,
  surfaceId: string,
  event: OperatorEvent,
  detail: Record<string, unknown> = {},
): Promise<void> {
  try {
    const resp = await fetch(eventUrl(view), {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ surface_id: surfaceId, event, detail }),
    });
    if (!resp.ok && resp.status !== 404) {
      console.error(`surface: emit ${event} failed: ${resp.status}`);
    }
  } catch (err) {
    console.error(`surface: emit ${event} threw`, err);
  }
}

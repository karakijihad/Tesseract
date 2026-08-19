// Y-2 — Surface Protocol canvas → tool half (`surface.emit_event`). When
// the operator drags / resizes / closes a card, the SurfaceLayer calls one
// of these to POST the interaction back to the backend, which persists the
// new geometry (so a reload re-renders where the operator left it) and, for
// `closed`, removes the surface.
//
// Fire-and-forget: a dropped emit is non-fatal — the next interaction
// re-sends.

import { BACKEND_BASE } from '../../lib/endpoints';
import type { OperatorEvent, SurfaceRenderStatus } from './types';

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

// The render half of the same channel. Without this the model's only
// self-check is `surface_list`, which reads back the backend's record of what
// the model itself asked for — it confirms a card was registered and says
// nothing about whether it drew. `sendBeacon` on the unmount path because a
// card usually unmounts while the page is going away, and `fetch` from an
// unload handler is routinely dropped.
export async function reportSurfaceRender(
  view: string,
  surfaceId: string,
  status: SurfaceRenderStatus,
  detail = '',
): Promise<void> {
  const url = `${BACKEND_BASE}/api/surfaces/${encodeURIComponent(view)}/${encodeURIComponent(surfaceId)}/render`;
  const body = JSON.stringify({ status, detail });
  if (status === 'unmounted' && typeof navigator?.sendBeacon === 'function') {
    try {
      navigator.sendBeacon(url, new Blob([body], { type: 'application/json' }));
      return;
    } catch {
      // Fall through to fetch — a refused beacon is not a reason to lose the report.
    }
  }
  try {
    const resp = await fetch(url, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body,
      keepalive: status === 'unmounted',
    });
    if (!resp.ok && resp.status !== 404) {
      console.error(`surface: render report ${status} failed: ${resp.status}`);
    }
  } catch (err) {
    console.error(`surface: render report ${status} threw`, err);
  }
}

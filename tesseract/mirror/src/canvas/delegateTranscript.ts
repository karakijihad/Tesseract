// Y-3 / D-6 / SC-4 — open a delegate transcript as a Surface-Protocol card.
// Replaces the old single-slot SpawnDrawer overlay: clicking a running-spawn
// chip spawns (or reveals) a `delegate-transcript` surface on the cockpit's
// `orb` view, which `SurfaceLayer` renders over the orb regardless of which
// panel is focused (SC-4 re-homed the surface layer into `CockpitStage`), so no
// forced navigation is needed. Idempotent — a card already bound to the same
// call_id is not duplicated.

import { BACKEND_BASE } from '../lib/endpoints';

const TRANSCRIPT_VIEW = 'orb';

export async function openDelegateTranscript(callId: string, toolName: string): Promise<void> {
  if (!callId) return;
  try {
    const resp = await fetch(`${BACKEND_BASE}/api/surfaces/${TRANSCRIPT_VIEW}`);
    if (resp.ok) {
      const body = (await resp.json()) as { surfaces?: Array<Record<string, unknown>> };
      const exists = (body.surfaces ?? []).some(
        (s) =>
          s.type === 'delegate-transcript' &&
          (s.props as Record<string, unknown> | undefined)?.call_id === callId,
      );
      if (exists) return; // already on the canvas — don't duplicate
    }
  } catch {
    // fall through to create — a failed dedupe check shouldn't block opening
  }
  await fetch(`${BACKEND_BASE}/api/surfaces/${TRANSCRIPT_VIEW}`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({
      type: 'delegate-transcript',
      title: toolName || 'delegate',
      props: { call_id: callId, tool_name: toolName },
      position: { x: 160, y: 120 },
      size: { w: 560, h: 520 },
    }),
  }).catch(() => undefined);
}

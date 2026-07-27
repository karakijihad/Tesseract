// AS-2 — open a running activity's own surface on the cockpit `tars` view.
// Shared by the activity map rows and the chat running-work chip, so "jump
// to the work" behaves identically from either entry point. Idempotent:
// a card already bound to the same id is revealed, not duplicated.

import { BACKEND_BASE } from '../lib/endpoints';
import { openDelegateTranscript } from './delegateTranscript';
import type { ActivityRecord } from '../stores/activity';

const VIEW = 'tars';

// Kinds this module can put on the canvas — the single source of truth for
// "openable vs. detail-only" (Task 6.2: ActivityMap + the chat activity
// taskbar both need this to decide click behavior; a locally-duplicated set
// would drift from the switch below).
export const OPENABLE_ACTIVITY_KINDS = new Set([
  'lane',
  'delegate',
  'controller_session',
]);

function bareId(activityId: string): string {
  const i = activityId.indexOf(':');
  return i >= 0 ? activityId.slice(i + 1) : activityId;
}

async function postSurface(body: Record<string, unknown>, dedupe: (s: Record<string, unknown>) => boolean): Promise<void> {
  try {
    const resp = await fetch(`${BACKEND_BASE}/api/surfaces/${VIEW}`);
    if (resp.ok) {
      const json = (await resp.json()) as { surfaces?: Array<Record<string, unknown>> };
      if ((json.surfaces ?? []).some(dedupe)) return; // already on the canvas
    }
  } catch {
    // a failed dedupe check shouldn't block opening
  }
  await fetch(`${BACKEND_BASE}/api/surfaces/${VIEW}`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  }).catch(() => undefined);
}

export async function openActivity(record: ActivityRecord): Promise<void> {
  const id = bareId(record.activity_id);
  if (!id) return;

  if (record.kind === 'delegate') {
    await openDelegateTranscript(id, record.label);
    return;
  }

  if (record.kind === 'lane') {
    await postSurface(
      {
        type: 'lane',
        title: record.label || 'lane',
        props: { lane_id: id },
        position: { x: 200, y: 140 },
        size: { w: 480, h: 460 },
      },
      (s) => s.type === 'lane' && (s.props as Record<string, unknown> | undefined)?.lane_id === id,
    );
    return;
  }

  if (record.kind === 'controller_session') {
    // Render the session's live transcript via ControllerMirrorBlock (WS
    // observer bridge → /ws/controller/<id>, replays from offset 0 then
    // live-follows). The delegate-transcript renderer can't show this — it only
    // reads CLI delegate stdout streams keyed by call_id — so a session row used
    // to open an empty "transcript not available" card.
    await postSurface(
      {
        type: 'session-transcript',
        title: record.label || 'session',
        props: { session_id: id },
        position: { x: 220, y: 160 },
        size: { w: 560, h: 520 },
      },
      (s) =>
        s.type === 'session-transcript' &&
        (s.props as Record<string, unknown> | undefined)?.session_id === id,
    );
    return;
  }

  // Other kinds (routine | autonomy | mcp_session) have no surface to
  // open — the activity map shows an inline detail block for those
  // instead of routing through here (see ActivityMap.tsx).
}

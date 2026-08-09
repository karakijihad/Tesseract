// Re-surface named lanes that survived a backend restart as canvas cards.
// Sourced from the AS-1 activity registry (live lanes only), idempotent
// against cards already on the canvas.

import { BACKEND_BASE } from '../lib/endpoints';

interface NamedLaneRecord {
  name: string;
  lane_id: string;
  kind: string;
  model?: string;
}

const LANE_W = 460;
const LANE_H = 420;
const TOP = 90;

async function surfacesFor(view: string): Promise<Array<Record<string, unknown>>> {
  try {
    const resp = await fetch(`${BACKEND_BASE}/api/surfaces/${encodeURIComponent(view)}`);
    if (!resp.ok) return [];
    const body = (await resp.json()) as { surfaces?: Array<Record<string, unknown>> };
    return body.surfaces ?? [];
  } catch {
    return [];
  }
}

async function createSurface(view: string, body: Record<string, unknown>): Promise<void> {
  await fetch(`${BACKEND_BASE}/api/surfaces/${encodeURIComponent(view)}`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  });
}

function boundLaneIds(surfaces: Array<Record<string, unknown>>): Set<string> {
  const ids = new Set<string>();
  for (const s of surfaces) {
    const props = (s.props as Record<string, unknown>) ?? {};
    if (s.type === 'lane' && typeof props.lane_id === 'string') ids.add(props.lane_id);
  }
  return ids;
}

interface ActivityItem {
  activity_id: string;
  kind: string;
  label: string;
  provider?: string | null;
  state: string;
}

// Lane activity states that still have a live PTY. A named lane whose process
// died leaves a `closed` (or failed/cancelled) record in the registry AND a
// persisted name binding, so we must filter by state here — restoring a card
// for a dead lane makes it poll a gone lane (502 storm) until the gone-fix
// dismisses it. Only restore cards for lanes that are actually alive.
const LIVE_LANE_STATES = new Set(['spawning', 'running', 'idle']);

// Live lanes from the AS-1 activity registry — the lanes that survived a
// restart with a live PTY (state-filtered; closed/failed/cancelled omitted).
async function liveLanes(): Promise<NamedLaneRecord[]> {
  try {
    const resp = await fetch(`${BACKEND_BASE}/api/activity`);
    if (!resp.ok) return [];
    const items = ((await resp.json()) as { items?: ActivityItem[] }).items ?? [];
    return items
      .filter(
        (i) => i.kind === 'lane' && i.activity_id.startsWith('lane:') && LIVE_LANE_STATES.has(i.state),
      )
      .map((i) => ({
        name: i.label,
        lane_id: i.activity_id.slice('lane:'.length),
        kind: i.provider ?? 'claude',
      }));
  } catch {
    return [];
  }
}

const _restoreInFlight = new Map<string, Promise<string[]>>();

// Re-surface the lanes that survived a restart as canvas cards. Sourced from
// the activity registry (live lanes only), idempotent against cards already on
// the canvas. It does NOT ensure/spawn new lanes — it only restores cards for
// lanes that already exist, so a reload shows last session's lanes without
// resurrecting closed ones or starting fresh processes.
export function restoreLanes(view: string): Promise<string[]> {
  const running = _restoreInFlight.get(view);
  if (running) return running;
  const p = _restoreLanes(view).finally(() => _restoreInFlight.delete(view));
  _restoreInFlight.set(view, p);
  return p;
}

async function _restoreLanes(view: string): Promise<string[]> {
  const lanes = await liveLanes();
  if (lanes.length === 0) return [];
  const existing = boundLaneIds(await surfacesFor(view));
  const restored: string[] = [];
  let idx = existing.size; // continue the left/right grid past any existing cards
  for (const rec of lanes) {
    if (existing.has(rec.lane_id)) continue;
    const x = idx % 2 === 0 ? 60 : 60 + LANE_W + 320;
    const y = TOP + Math.floor(idx / 2) * (LANE_H + 30);
    await createSurface(view, {
      type: 'lane',
      title: rec.name,
      position: { x, y },
      size: { w: LANE_W, h: LANE_H },
      props: { lane_id: rec.lane_id, name: rec.name, kind: rec.kind, model: rec.model ?? '' },
    });
    restored.push(rec.lane_id);
    idx++;
  }
  return restored;
}

// The map. The first thing on the floor plan: the shape of the machine, with
// live state on it where something reports.
//
// Every node, every edge, every heading and every word for a state arrives in
// the payload. This file decides where things sit and nothing else — a name
// written here would be a description of the backend that goes stale on its
// own, which is how the Kernel rail acquired five signals that render cold
// forever.
//
// Positions come from the bands, so two nodes added to a band move the drawing
// without anybody opening this file. Inline SVG, no library, no fetch of its
// own: the same constraint the rest of the app lives under.
//
// **Nothing moves.** No transition, no pulse, no dash offset animating along
// an edge. Motion on this surface is reserved for an event that actually
// arrived. Events arrive now, and a colour changing when one does is the
// honest amount of movement: a node repaints because the runtime said it
// changed. Anything driven by elapsed time is decorative, and decorative
// motion on an operational surface is a lie about liveness.

import { useEffect } from 'react';
import { useAutonomyStore, type AutonomyLevel } from '../../stores/autonomy';
import {
  live,
  NOTHING_PUSHED,
  useLiveContext,
  type LiveContext,
} from '../../stores/liveness';
import type { MachineMapResponse, MapEdge, MapNode } from '../../lib/api';

// The map reconciles at the slow end of the liveness contract's window. It is
// no longer how a state gets here — `stores/liveness.ts` holds what the runtime
// pushed — but a read is what the declared shape comes from, and what the
// panel falls back to when nothing is being pushed.
const POLL_MS = 60_000;

// The level kinds a room on this panel can draw. The payload names what a node
// opens; this is what the app can actually open, and the two are checked
// against each other rather than assumed equal.
const OPENABLE: ReadonlySet<string> = new Set(['entry', 'agenda', 'worker', 'step']);

const VIEW_W = 880;
const PAD = 14;
const DOOR_W = 96;
const NODE_H = 26;
const ROW_STEP = 34;
const SESSION_W = 124;
const SESSION_H = 36;
const REACH_W = 110;
const TOP_Y = 30;
const CHIP_W = 128;
const CHIP_H = 28;
const CHIP_GAP = 12;
const CHIP_STEP = CHIP_H + 10;
const BAND_GAP = 30;

interface Placed {
  node: MapNode;
  x: number;
  y: number;
  w: number;
  h: number;
}

interface Drawing {
  placed: Placed[];
  edges: { edge: MapEdge; from: Placed; to: Placed }[];
  bands: { id: string; label: string; x: number; y: number }[];
  divider: number | null;
  height: number;
}

function _column(nodes: MapNode[], x: number, w: number, top: number): Placed[] {
  return nodes.map((node, i) => ({
    node,
    x,
    y: top + i * ROW_STEP,
    w,
    h: NODE_H,
  }));
}

/** Where everything sits, from the bands the payload declares.
 *
 *  A reach node fed by another reach node sits beside its source rather than
 *  under the session, because the edge says it hangs off that one — the same
 *  rule that puts the doors in a column and the session between them. */
function _layout(data: MachineMapResponse): Drawing {
  const of = (band: string) => data.nodes.filter((n) => n.band === band);
  const doors = of('in');
  const sessions = of('session');
  const reaches = of('reaches');
  const runs = of('runs');

  const fedByReach = new Set(
    data.edges
      .filter((e) => reaches.some((n) => n.name === e.source))
      .map((e) => e.target),
  );
  const direct = reaches.filter((n) => !fedByReach.has(n.name));
  const hanging = reaches.filter((n) => fedByReach.has(n.name));

  const doorX = PAD;
  const sessionX = 226;
  const reachX = 512;
  const hangX = reachX + REACH_W + 46;

  const placed: Placed[] = [
    ..._column(doors, doorX, DOOR_W, TOP_Y),
    ..._column(direct, reachX, REACH_W, TOP_Y),
  ];

  const doorsBottom = TOP_Y + Math.max(doors.length, 1) * ROW_STEP - (ROW_STEP - NODE_H);
  const sessionY = Math.max(TOP_Y, (TOP_Y + doorsBottom) / 2 - SESSION_H / 2);
  sessions.forEach((node, i) => {
    placed.push({
      node,
      x: sessionX,
      y: sessionY + i * (SESSION_H + 10),
      w: SESSION_W,
      h: SESSION_H,
    });
  });

  const by = new Map(placed.map((p) => [p.node.name, p]));
  hanging.forEach((node) => {
    const feeder = data.edges.find((e) => e.target === node.name);
    const source = feeder ? by.get(feeder.source) : undefined;
    const spot = {
      node,
      x: hangX,
      y: (source?.y ?? TOP_Y) + ROW_STEP,
      w: REACH_W,
      h: NODE_H,
    };
    placed.push(spot);
    by.set(node.name, spot);
  });

  const topBottom = placed.reduce((low, p) => Math.max(low, p.y + p.h), TOP_Y);
  const divider = runs.length ? topBottom + BAND_GAP : null;
  const runsTop = (divider ?? topBottom) + BAND_GAP;
  const perRow = Math.max(1, Math.floor((VIEW_W - PAD * 2 + CHIP_GAP) / (CHIP_W + CHIP_GAP)));
  runs.forEach((node, i) => {
    const spot = {
      node,
      x: PAD + (i % perRow) * (CHIP_W + CHIP_GAP),
      y: runsTop + Math.floor(i / perRow) * CHIP_STEP,
      w: CHIP_W,
      h: CHIP_H,
    };
    placed.push(spot);
    by.set(node.name, spot);
  });

  const edges = data.edges
    .map((edge) => ({ edge, from: by.get(edge.source), to: by.get(edge.target) }))
    .filter((e): e is { edge: MapEdge; from: Placed; to: Placed } =>
      Boolean(e.from && e.to),
    );

  const labels = new Map(data.bands.map((b) => [b.id, b.label]));
  const bands: Drawing['bands'] = [];
  if (doors.length) bands.push({ id: 'in', label: labels.get('in') ?? '', x: PAD, y: TOP_Y - 12 });
  if (direct.length)
    bands.push({ id: 'reaches', label: labels.get('reaches') ?? '', x: reachX, y: TOP_Y - 12 });
  if (runs.length)
    bands.push({ id: 'runs', label: labels.get('runs') ?? '', x: PAD, y: runsTop - 12 });

  const bottom = placed.reduce((low, p) => Math.max(low, p.y + p.h), TOP_Y);
  return { placed, edges, bands, divider, height: bottom + 34 };
}

/** An elbow from one box to another: out of the right edge, across, in. */
function _path(from: Placed, to: Placed): string {
  const x1 = from.x + from.w;
  const y1 = from.y + from.h / 2;
  const x2 = to.x;
  const y2 = to.y + to.h / 2;
  const mid = x1 + Math.max(12, (x2 - x1) / 2);
  if (Math.abs(y1 - y2) < 1) return `M${x1} ${y1} L${x2} ${y2}`;
  return `M${x1} ${y1} L${mid} ${y1} L${mid} ${y2} L${x2} ${y2}`;
}

export interface MachineMapViewProps {
  data: MachineMapResponse | null;
  status: 'idle' | 'loading' | 'ready' | 'error';
  error: string | null;
  /** A node is layer 4's handle on layer 3: opening one opens what it stands
   *  for. Only nodes the payload gives an `opens` to are openable, so what can
   *  be clicked is the backend's answer and not this file's. */
  onOpen?: (opens: { kind: string; id: string }) => void;
  /** The node the thing beside this map IS, drawn as where you are. */
  marked?: string;
  /** When this payload was read. What the runtime pushed wins only while it is
   *  newer than this, so a feed that stopped cannot outrank a room that is
   *  still reading. */
  readAt?: number | null;
  /** What the runtime has pushed since. Passed in rather than read here: this
   *  view is rendered to static markup in its tests, where a store subscription
   *  answers with the state it was created holding rather than the one it has. */
  liveness?: LiveContext;
}

export function MachineMapView({
  data,
  status,
  error,
  onOpen,
  marked,
  readAt = null,
  liveness = NOTHING_PUSHED,
}: MachineMapViewProps): React.ReactElement | null {
  if (status === 'error') {
    return (
      <p className="map-note t-meta" data-testid="machine-map">
        {error ?? 'The map could not be read.'} What the machine looks like is not known.
      </p>
    );
  }
  if (!data) return null;

  // Every node reads through the one reader, so what a state means when the
  // runtime goes quiet is answered in one place for the whole panel.
  const drawn: MachineMapResponse = {
    ...data,
    nodes: data.nodes.map((node) => ({
      ...node,
      liveness: live(liveness, `node:${node.name}`, node.liveness, readAt),
    })),
  };
  const { placed, edges, bands, divider, height } = _layout(drawn);
  const states = [...new Map(placed.map((p) => [p.node.liveness.state, p.node])).values()];

  return (
    <div className="machine-map" data-testid="machine-map">
      <svg
        viewBox={`0 0 ${VIEW_W} ${height}`}
        className="machine-map__svg"
        role="img"
        aria-label="How the machine is put together, and what is working right now"
      >
        {edges.map(({ edge, from, to }) => (
          <path
            key={`${edge.source}-${edge.target}`}
            className="map-edge"
            d={_path(from, to)}
          >
            <title>{`${edge.source} to ${edge.target}: ${edge.carries}`}</title>
          </path>
        ))}

        {divider !== null && (
          <line
            className="map-divider"
            x1={PAD}
            y1={divider}
            x2={VIEW_W - PAD}
            y2={divider}
          />
        )}

        {bands.map((band) => (
          <text key={band.id} className="map-band" x={band.x} y={band.y}>
            {band.label}
          </text>
        ))}

        {placed.map((p) => {
          const opens = p.node.opens;
          const open = opens && onOpen ? () => onOpen(opens) : undefined;
          return (
            <g
              key={p.node.name}
              className={`map-node map-node--${p.node.liveness.state}${
                open ? ' map-node--opens' : ''
              }${p.node.name === marked ? ' map-node--here' : ''}`}
              role={open ? 'button' : undefined}
              tabIndex={open ? 0 : undefined}
              aria-current={p.node.name === marked ? 'true' : undefined}
              onClick={open}
              onKeyDown={
                open
                  ? (e) => {
                      if (e.key !== 'Enter' && e.key !== ' ') return;
                      e.preventDefault();
                      open();
                    }
                  : undefined
              }
            >
              <title>{`${p.node.name}, ${p.node.liveness.label}. ${p.node.summary}`}</title>
              <rect className="map-node__box" x={p.x} y={p.y} width={p.w} height={p.h} rx={4} />
              <text className="map-node__name" x={p.x + 12} y={p.y + p.h / 2 + 4}>
                {p.node.name}
              </text>
            </g>
          );
        })}
      </svg>

      <ul className="map-legend">
        {states.map((node) => (
          <li key={node.liveness.state} className="map-legend__item">
            <span
              className={`map-legend__mark map-node--${node.liveness.state}`}
              aria-hidden="true"
            />
            <span className="t-meta">{node.liveness.label}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

export function MachineMap({ marked }: { marked?: string } = {}): React.ReactElement | null {
  const section = useAutonomyStore((s) => s.map);
  const liveness = useLiveContext(section.data?.labels);
  const fetchMap = useAutonomyStore((s) => s.fetchMap);
  const pushLevel = useAutonomyStore((s) => s.pushLevel);

  useEffect(() => {
    if (section.status === 'idle') void fetchMap();
  }, [section.status, fetchMap]);

  useEffect(() => {
    const timer = window.setInterval(() => void fetchMap(), POLL_MS);
    return () => window.clearInterval(timer);
  }, [fetchMap]);

  return (
    <MachineMapView
      data={section.data}
      status={section.status}
      error={section.error}
      marked={marked}
      readAt={section.lastFetched}
      liveness={liveness}
      // A node opens the thing it stands for, one level down IN THE PANE.
      // Which nodes can be opened is the payload's answer: everything with an
      // `opens`, and nothing else.
      //
      // Checked rather than cast. A kind this app cannot draw would open a
      // level that renders "cannot open a ... yet" and leave the operator
      // somewhere with nothing in it; not opening at all is the honest answer
      // until the room learns that kind.
      onOpen={(opens) => {
        if (!OPENABLE.has(opens.kind)) return;
        pushLevel({
          kind: opens.kind as AutonomyLevel['kind'],
          id: opens.id,
          label: opens.id,
        });
      }}
    />
  );
}

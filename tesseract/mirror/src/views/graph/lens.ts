// What the picture is showing, and nothing about how it looks.
//
// The map never opens on the whole library: a hairball answers no question,
// and a picture of six hundred records reads as a picture of all of them. So a
// visit starts by choosing a way in, and everything after that narrows or
// widens what the way in found. This module is that arithmetic, kept away from
// the canvas so it can be tested without a DOM and read without one.
//
// **Two rules hold everything here together.**
//
// A way in and a filter grow and cut the SET of records drawn. Neither of them
// ever moves one: positions come off the build, so a record a person learnt
// the place of stays there whatever is being shown, and narrowing the view
// takes dots away rather than rearranging the ones that remain. The picture
// only ever moves when somebody asks it to, by finding a record by name.
//
// And nothing here decides what a word about the graph MEANS. What arrived in
// the last drawing is the graph's own recorded delta, the most connected
// records are `report.hubs`, whether a link crosses between compartments is on
// the link, and every phrase naming a kind, a compartment, a class or a kind
// of link arrives with the payload. What this file counts is how much of that
// is currently on screen, which is a fact about the picture and not about the
// library.

import type { GraphNode, GraphResponse } from '../../lib/api';

/** How a visit starts. Not a filter: it chooses the records the picture is
 *  drawn around, and the filters below then cut what it found.
 *
 *  `orphans` is not joined by a fourth way for the other two rows the Atlas
 *  room can now name a record for. A conflict's subjects are two real nodes
 *  with no edge between them, so drawing them would show two unconnected
 *  dots and lose the one thing worth knowing, which is WHY they disagree.
 *  A dangling link's missing side is not a node at all and never can be, so
 *  there is no dot to seed a picture from. Both stay questions for the
 *  assistant instead, in the room, where the fact that makes them worth
 *  asking about still travels with the id. */
export type Way = 'new' | 'crossing' | 'hubs' | 'orphans' | 'all' | 'record';

export interface Lens {
  /** `null` is the entry state, where nothing is drawn yet. */
  way: Way | null;
  /** The record a search named, when the way in is one record. */
  named: string | null;
  /** How many links out from the way in the picture follows. */
  reach: number;
  /** Records kept on the picture whatever else is shown, so two
   *  neighbourhoods can be looked at at once. */
  pinned: string[];
  /** Kinds of record kept. Empty means every kind. */
  kinds: string[];
  /** Arrived at this moment or later. `null` means whenever. */
  since: string | null;
  /** Where a link came from. Empty means every class. */
  provenance: string[];
  /** Kinds of link kept. Empty means every kind. */
  linkTypes: string[];
  crossingOnly: boolean;
  newOnly: boolean;
}

export const ENTRY: Lens = {
  way: null,
  named: null,
  reach: 1,
  pinned: [],
  kinds: [],
  since: null,
  provenance: [],
  linkTypes: [],
  crossingOnly: false,
  newOnly: false,
};

/** How far out each way in reaches, before anybody expands it.
 *
 *  A record on its own is a dot, and what arrived in the last drawing is only
 *  interesting beside what it attached itself to, so both start one link out.
 *  The crossing links already name both of their ends, and everything is
 *  already everything. An orphan has no edges by definition, so a reach past
 *  zero would follow nothing: the person looking is free to raise it, but
 *  there is nothing to gain by starting anywhere but zero. */
export const REACH_OF: Record<Way, number> = {
  new: 1,
  crossing: 0,
  hubs: 1,
  orphans: 0,
  all: 0,
  record: 1,
};

/** How far out the picture will follow. Past two links from a hub the
 *  neighbourhood is most of the map again, which is the thing the entry state
 *  exists to prevent. */
export const REACH_MAX = 3;

export interface View {
  /** The records drawn, by id. */
  nodes: Set<string>;
  /** The links drawn, by id. */
  edges: Set<string>;
  /** What the surface says about what it drew. The app's own sentence, about
   *  the picture: every claim about the library is the payload's. */
  said: string;
  /** Records that every pinned one touches. The whole of what a comparison
   *  is: two neighbourhoods side by side, and what they have in common. */
  shared: string[];
  /** How many of the drawn records are in each compartment, and how many are
   *  of each kind. Counted here rather than beside the legend because the
   *  legend, the key and the sentence over the picture have to be counting
   *  the same set: the payload's own totals are about what the map SENT, and
   *  printing those beside a narrowed picture says a compartment has more
   *  records on screen than the whole picture holds. */
  byRegion: Map<string, number>;
  byKind: Map<string, number>;
}

/** Whether a record survives the filters. */
function keepsNode(lens: Lens, node: GraphNode): boolean {
  if (lens.kinds.length > 0 && !lens.kinds.includes(node.kind)) return false;
  if (lens.since !== null) {
    if (!node.firstSeen) return false;
    if (Date.parse(node.firstSeen) < Date.parse(lens.since)) return false;
  }
  return true;
}

/** Whether a link survives the filters.
 *
 *  Used for the picture AND for following a neighbourhood, deliberately: with
 *  only the links a record states itself let through, expanding it walks the
 *  stated ones, which is the question somebody narrowing to them is asking. */
function keepsEdge(
  lens: Lens,
  edge: GraphResponse['edges'][number],
  arrived: Set<string>,
): boolean {
  if (lens.crossingOnly && !edge.crosses) return false;
  if (lens.newOnly && !arrived.has(edge.id)) return false;
  if (lens.provenance.length > 0 && !lens.provenance.includes(edge.provenance)) {
    return false;
  }
  if (lens.linkTypes.length > 0 && !lens.linkTypes.includes(edge.type)) {
    return false;
  }
  return true;
}

/** The records the way in is drawn around. */
function seedsOf(data: GraphResponse, lens: Lens): string[] {
  const here = new Set(data.nodes.map((n) => n.id));
  switch (lens.way) {
    case 'new':
      return data.delta.nodes.filter((id) => here.has(id));
    case 'crossing': {
      const ends = new Set<string>();
      for (const edge of data.edges) {
        if (!edge.crosses) continue;
        ends.add(edge.subject);
        ends.add(edge.object);
      }
      return [...ends].filter((id) => here.has(id));
    }
    case 'hubs':
      return data.hubs.filter((id) => here.has(id));
    case 'orphans':
      return data.orphans.filter((id) => here.has(id));
    case 'all':
      return data.nodes.map((n) => n.id);
    case 'record':
      return lens.named && here.has(lens.named) ? [lens.named] : [];
    default:
      return [];
  }
}

/** Everything each record is linked to, through the links the filters let
 *  through. */
function adjacency(
  data: GraphResponse,
  lens: Lens,
  arrived: Set<string>,
): Map<string, Set<string>> {
  const near = new Map<string, Set<string>>();
  for (const edge of data.edges) {
    if (!keepsEdge(lens, edge, arrived)) continue;
    if (!near.has(edge.subject)) near.set(edge.subject, new Set());
    if (!near.has(edge.object)) near.set(edge.object, new Set());
    near.get(edge.subject)?.add(edge.object);
    near.get(edge.object)?.add(edge.subject);
  }
  return near;
}

/** What is on the picture, given what was asked for. Pure, and the one place
 *  that decides it: the canvas draws this set and the sentence over it counts
 *  this set, so what a person sees and what they are told cannot differ. */
export function view(data: GraphResponse, lens: Lens): View {
  const arrived = new Set(data.delta.edges);
  const near = adjacency(data, lens, arrived);
  const byId = new Map(data.nodes.map((n) => [n.id, n]));

  const seeds = new Set(seedsOf(data, lens));
  for (const id of lens.pinned) if (byId.has(id)) seeds.add(id);

  // Out from the seeds, one link at a time. Bounded by `reach` rather than by
  // a node count, so what is drawn is a shape somebody asked for and not the
  // first n records a walk happened to reach.
  const grown = new Set(seeds);
  let edge = [...seeds];
  for (let step = 0; step < lens.reach && edge.length > 0; step += 1) {
    const next: string[] = [];
    for (const id of edge) {
      for (const other of near.get(id) ?? []) {
        if (grown.has(other) || !byId.has(other)) continue;
        grown.add(other);
        next.push(other);
      }
    }
    edge = next;
  }

  const nodes = new Set<string>();
  for (const id of grown) {
    const node = byId.get(id);
    if (node && keepsNode(lens, node)) nodes.add(id);
  }
  // A pin is somebody saying "keep this one in front of me" and a filter is
  // somebody saying "show me only these". The named record wins, because a pin
  // that vanished when a filter was touched would look like the pin failing.
  for (const id of lens.pinned) if (byId.has(id)) nodes.add(id);

  const edges = new Set<string>();
  for (const link of data.edges) {
    if (!nodes.has(link.subject) || !nodes.has(link.object)) continue;
    if (!keepsEdge(lens, link, arrived)) continue;
    edges.add(link.id);
  }

  // What they have in common, and a pinned record is not one of them: two
  // pins that link to each other would otherwise report each other as
  // something they share.
  const shared =
    lens.pinned.length < 2
      ? []
      : [...(near.get(lens.pinned[0]) ?? [])].filter(
          (id) =>
            !lens.pinned.includes(id) &&
            lens.pinned.every((pin) => near.get(pin)?.has(id)),
        );

  const byRegion = new Map<string, number>();
  const byKind = new Map<string, number>();
  for (const id of nodes) {
    const node = byId.get(id);
    if (!node) continue;
    byRegion.set(node.region, (byRegion.get(node.region) ?? 0) + 1);
    byKind.set(node.kind, (byKind.get(node.kind) ?? 0) + 1);
  }

  return {
    nodes,
    edges,
    said: said(data, lens, nodes.size, edges.size),
    shared,
    byRegion,
    byKind,
  };
}

/** What the surface prints about what it drew.
 *
 *  Separate from the payload's own sentence, which says how much of the
 *  library reached the browser at all. This one says how much of THAT is on
 *  screen, which is the second half of the same promise: a picture that
 *  quietly shows a tenth of what it was handed misleads exactly as much as one
 *  that shows a tenth of the library. */
function said(
  data: GraphResponse,
  lens: Lens,
  drawn: number,
  links: number,
): string {
  if (lens.way === null) return '';
  const of = data.drawn.nodes;
  if (drawn === 0) {
    return (
      'nothing here matches all of that. Take a filter off, or follow the ' +
      'links out one more step'
    );
  }
  const records =
    drawn >= of
      ? `all ${of.toLocaleString()} of the records the map sent`
      : `${drawn.toLocaleString()} of the ${of.toLocaleString()} records the map sent`;
  return `${records}, and the ${links.toLocaleString()} link${
    links === 1 ? '' : 's'
  } between them`;
}

/** Records whose name, id or file carries the words typed. Ranked by where
 *  the match falls, so typing the start of a title finds it first. */
export function matches(
  data: GraphResponse,
  query: string,
  limit = 8,
): GraphNode[] {
  const want = query.trim().toLowerCase();
  if (!want) return [];
  const scored: { node: GraphNode; at: number }[] = [];
  for (const node of data.nodes) {
    const title = (node.title || '').toLowerCase();
    const at = title.indexOf(want);
    if (at >= 0) {
      scored.push({ node, at });
      continue;
    }
    if (
      node.id.toLowerCase().includes(want) ||
      node.locator.toLowerCase().includes(want)
    ) {
      // Behind every title match: somebody typing words is looking for a
      // record by its name before its filename.
      scored.push({ node, at: Number.MAX_SAFE_INTEGER });
    }
  }
  scored.sort(
    (a, b) => a.at - b.at || a.node.title.localeCompare(b.node.title),
  );
  return scored.slice(0, limit).map((hit) => hit.node);
}

/** The drawings the records on the picture arrived in, newest first.
 *
 *  Taken from the payload rather than from a calendar: these are the days the
 *  map actually gained something, so every one of them narrows the view to
 *  something. The value is the earliest moment on that day, so choosing it
 *  means "this drawing and everything after it" and no arithmetic on a
 *  timezone happens anywhere. */
export function arrivals(data: GraphResponse): { at: string; day: string }[] {
  const earliest = new Map<string, string>();
  for (const node of data.nodes) {
    if (!node.firstSeen) continue;
    const day = new Date(node.firstSeen).toDateString();
    const seen = earliest.get(day);
    if (!seen || Date.parse(node.firstSeen) < Date.parse(seen)) {
      earliest.set(day, node.firstSeen);
    }
  }
  return [...earliest.values()]
    .sort((a, b) => Date.parse(b) - Date.parse(a))
    .map((at) => ({
      at,
      day: new Date(at).toLocaleDateString(undefined, {
        day: 'numeric',
        month: 'short',
      }),
    }));
}

/** How many filters are narrowing the view. The count is what the surface
 *  shows when the filters are folded away, because a filter nobody can see is
 *  a picture that is wrong for a reason nobody can find. */
export function narrowing(lens: Lens): number {
  return (
    (lens.kinds.length > 0 ? 1 : 0) +
    (lens.since !== null ? 1 : 0) +
    (lens.provenance.length > 0 ? 1 : 0) +
    (lens.linkTypes.length > 0 ? 1 : 0) +
    (lens.crossingOnly ? 1 : 0) +
    (lens.newOnly ? 1 : 0)
  );
}

/** One of a list of choices, on or off. Used by every chip filter here, so
 *  none of them writes the same three lines. */
export function toggle(chosen: string[], key: string): string[] {
  return chosen.includes(key)
    ? chosen.filter((one) => one !== key)
    : [...chosen, key];
}

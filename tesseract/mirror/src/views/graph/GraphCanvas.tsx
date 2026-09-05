// The picture. Canvas, because a few hundred nodes on screen and a few
// thousand in the corpus is not a DOM tree.
//
// **Nothing here settles anything.** Positions arrive with the payload,
// settled by the build, so the same graph draws the same picture and a map a
// person learnt survives the next pass. There is no simulation, no seed, no
// iteration count and no timer: every repaint is a pure function of the
// payload, the view transform, and what is under the pointer.
//
// **Compartments are named, with no box round them.** The colour already
// separates them, and a box would draw a boundary the data does not have --- it
// draws one less than it used to, because a compartment is no longer a PLACE:
// the build settles a record beside what it links to, so a run that references
// a memory sits among the memories, which is the fact worth seeing. The name
// goes under the middle half of its own records for that reason, and a record
// pulled across the map does not take it along.
//
// **The crossing links are the point.** A link between two compartments is
// drawn differently from one inside a compartment, because a memory promoted
// out of a failed run is the thread that makes this one brain rather than four
// piles.
//
// **Labels are virtualised, and the resting picture keeps very few.** The
// selection, whatever is under the pointer, whatever is being compared, and a
// handful of hubs. Everything else earns a label by being zoomed into, because
// six hundred at once is a grey field --- and because the hubs are now all in
// one cluster, so their titles arrive as a block of text over the one part of
// the picture worth looking at.
//
// **What is drawn is `shown`. Where a record sits never changes; where the
// CAMERA sits changes once per question.** The frame is fitted to what a way
// in put on the picture, and then held: filters and expansion take dots away
// and put them back inside that frame, so trimming what is shown never
// rearranges what is left. Refitting on every chip would make the picture jump
// under the hand that is filtering it; refitting on nothing at all was worse
// and this is measured, not guessed: eighty of six hundred records drawn in
// the whole corpus's frame is a speck in the middle of an empty panel. Moving
// the camera is not moving the map, which is what criterion 5 is about and
// what `layout.py` already settled.

// **Why this is one file rather than four.** Everything below the transform
// paints into one context against one frame, and every piece of it needs the
// same handful of locals: the scale and origin, the brand, what is shown, what
// is selected, what is under the pointer, what is held and what is pinned.
// Split into modules, each would take those as arguments and the seams would
// be wider than the code they separate. What is genuinely separable already
// left: `lens.ts` decides WHAT is drawn and `brand.ts` decides what it is
// painted with, and neither knows this file exists.

import { useCallback, useEffect, useRef } from 'react';

import type { GraphResponse } from '../../lib/api';
import { readBrand, type Brand } from './brand';
import type { View } from './lens';

/** Room round the picture so a node at its edge is not against the edge of
 *  the canvas. A multiplier on the frame the current question opened with, not
 *  on a fixed radius: the abstract disc the build lays nodes out in belongs to
 *  `layout.py`, and a copy of its radius here would mis-scale the picture in
 *  silence the day that number changed. What is drawn is measured from what
 *  arrived. */
const MARGIN = 1.18;
/** **What a repaint costs, measured rather than assumed.** Walked live on the
 *  cockpit at 600 records and 1,194 links, on a 904 by 580 canvas: `paint` is
 *  4.1 ms and `pick` is 0.3 ms, so a pointer move while dragging is a quarter
 *  of a frame at 60 fps.
 *
 *  AR-9 measured 0.87 ms and that number is not the budget any more. It was
 *  taken when the map drew about a third as many links, and the cost here is
 *  the drawing itself: 600 marks and 1,194 line segments. Two ways of making
 *  that cheaper were tried and BOTH were measured at no gain, so neither is in
 *  this file. Batching every link into one path per look took 1,194 stroke
 *  calls down to six and came back at 4.4 ms, slightly worse. Skipping the
 *  backing-store reallocation when the canvas had not resized came back at
 *  4.6 ms. Anyone reaching for either again should measure first: the frame is
 *  spent inside the canvas, not around it.
 */

/** How far in and out the picture may be taken. Below the floor the labels are
 *  unreadable and above the ceiling one dot fills the panel. */
const ZOOM_MIN = 0.4;
const ZOOM_MAX = 8;
const ZOOM_STEP = 1.12;
/** How close the pointer has to be to count as pointing at a record. In screen
 *  pixels, so it stays the same size to the hand at every zoom. */
const HIT_RADIUS = 14;
/** How thick a link is drawn, and how thick one worth spotting is. */
const LINE = 1;
const LINE_STRONG = 1.6;
/** A dot's size, by how connected the record is: a floor so a lone record is
 *  still visible, and a ceiling so a hub does not swallow its neighbourhood. */
const DOT_MIN = 2.2;
const DOT_MAX = 9;
const DOT_PER_LINK = 0.5;
/** Three steps, not two: the record that is open, what it touches, and
 *  everything else.
 *
 *  It was two, and in a settled cluster that separated nothing. The layout
 *  puts what is connected in the same place, so the background of a
 *  neighbourhood IS the rest of the same knot, and 0.3 against 1.0 was not a
 *  difference the eye could find there. `FADE_ASIDE` is what everything the
 *  selection does not touch drops to; `NEAR` is what its neighbours hold, a
 *  step under the selection itself so the record that was clicked is still
 *  the brightest dot on the picture.
 *
 *  `FADE_OUT` is a different question with a similar look: what a record
 *  outside the HELD COMPARTMENT drops to. It stays lower, so "not in this
 *  compartment" and "not beside the selection" do not read the same. */
const FADE_ASIDE = 0.08;
const NEAR = 0.85;
const FADE_OUT = 0.04;
/** Where a record's name comes back. In `view.k`, the zoom a person set, and
 *  never the drawing scale: fitting a small answer into the panel raises the
 *  drawing scale without anybody having asked to see names.
 *
 *  Two names at rest is right and AR-9 measured why. What was wrong was
 *  cutting the zoom tier with them: somebody taking the picture into a cluster
 *  is asking what is in it, which is the one moment names are the answer
 *  rather than a grey field. */
const LABEL_FROM = 1.9;
/** How many names one frame may lay out. A ceiling on the work rather than on
 *  the picture: past this it is a wall of text again, and the frame is paying
 *  to measure text nobody can read. */
const LABEL_MAX = 40;
/** Room around a name before it counts as landing on another, and how tall a
 *  line of it is. The height is the app's caption tier plus its leading; the
 *  canvas has no box to measure, and reading one back per label would force a
 *  style recalculation on every name. */
const LABEL_PAD = 3;
const LABEL_LINE = 14;
/** How far outside a dot its pinned ring sits. */
const PIN_RING = 4;
const NODE_LABEL_DROP = 12;

/** What the machine DID, as opposed to what it knows. Three kinds out of ten,
 *  and they are the three that are events rather than knowledge: a run is
 *  something the body did, a defect is something the watchman filed about one,
 *  and an agenda item is something it decided to do. They are given a shape of
 *  their own so that activity is legible without asking anybody to hold ten
 *  colours in their head, and so it is still legible to a reader who cannot
 *  separate two of those colours.
 *
 *  Three shapes, never ten. A shape per kind is a second alphabet to learn on
 *  top of the colours, and the colour key already teaches one. The split this
 *  encodes is the one a reader has to make anyway: everything the machine
 *  KNOWS is a disc, everything it DID has a corner. */
const EVENT_KINDS: Record<string, 'run' | 'filed' | 'decided'> = {
  run: 'run',
  feedback: 'filed',
  agenda: 'decided',
};

/** Trace a record's outline, ready to be filled or stroked. Never both a fill
 *  and a stroke here: the caller decides which, so the pinned ring and the dot
 *  itself are the same shape at two sizes. */
function mark(
  ctx: CanvasRenderingContext2D,
  kind: string,
  x: number,
  y: number,
  r: number,
): void {
  const shape = EVENT_KINDS[kind];
  if (shape === undefined) {
    ctx.arc(x, y, r, 0, Math.PI * 2);
    return;
  }
  if (shape === 'run') {
    // Pointing up: something the body did. Grown a little, because a triangle
    // of a given radius reads smaller than a disc of the same one.
    const h = r * 1.25;
    ctx.moveTo(x, y - h);
    ctx.lineTo(x + h, y + h * 0.72);
    ctx.lineTo(x - h, y + h * 0.72);
    ctx.closePath();
    return;
  }
  if (shape === 'decided') {
    // Square: something it decided to do. Flat sides, so it is the one that
    // reads as a thing waiting rather than a thing that happened.
    const h = r * 0.92;
    ctx.moveTo(x - h, y - h);
    ctx.lineTo(x + h, y - h);
    ctx.lineTo(x + h, y + h);
    ctx.lineTo(x - h, y + h);
    ctx.closePath();
    return;
  }
  // Turned on its corner: something found wrong.
  const d = r * 1.28;
  ctx.moveTo(x, y - d);
  ctx.lineTo(x + d, y);
  ctx.lineTo(x, y + d);
  ctx.lineTo(x - d, y);
  ctx.closePath();
}

interface Props {
  data: GraphResponse;
  /** The records and links the current question puts on the picture. */
  shown: View;
  /** The record the inspector is showing. */
  selected: string | null;
  onSelect: (id: string | null) => void;
  /** The compartment the picture is held on, or null for all four. */
  region: string | null;
  /** Records kept on the picture whatever else is shown. Ringed, so two
   *  neighbourhoods being compared are visible as two. */
  pinned: string[];
  /** A record to move the picture to. The number changes on every ask, so
   *  finding the same record twice still brings it back. */
  focus: { id: string; nonce: number } | null;
  /** Which QUESTION is on the picture. When this changes the camera is fitted
   *  to the answer; while it holds, filters draw inside the frame it opened
   *  with. */
  frameKey: string;
}

interface Placed {
  id: string;
  kind: string;
  region: string;
  title: string;
  degree: number;
  x: number;
  y: number;
}

export function GraphCanvas({
  data,
  shown,
  selected,
  onSelect,
  region,
  pinned,
  focus,
  frameKey,
}: Props): React.ReactElement {
  const ref = useRef<HTMLCanvasElement | null>(null);
  const view = useRef({ k: 1, x: 0, y: 0 });
  const hover = useRef<string | null>(null);
  const brand = useRef<Brand | null>(null);
  /** Records pulled aside by hand, in the build's own units.
   *
   *  **This is not the map moving, and the distinction is the ruling.** Where
   *  a record SITS is the build's answer and must not change when it is
   *  browsed, so a map somebody learnt survives the next pass. A drag is
   *  temporary and forgotten on reload: it lives here beside the pan and the
   *  zoom, it is cleared when a new payload arrives, and no part of it is ever
   *  sent anywhere. What it moves is the same thing panning moves, which is
   *  what is in front of the reader rather than what the graph says.
   *
   *  A dense knot cannot be read without moving something out of it, and there
   *  was no way to move anything. */
  const moved = useRef(new Map<string, { x: number; y: number }>());

  // Everything the paint needs, derived once per payload rather than per
  // frame. A drag repaints on every pointer move; rebuilding an adjacency map
  // inside that is how a canvas that draws fine at rest stutters when touched.
  const model = useRef<{
    nodes: Placed[];
    byId: Map<string, Placed>;
    neighbours: Map<string, Set<string>>;
  }>({
    nodes: [],
    byId: new Map(),
    neighbours: new Map(),
  });

  useEffect(() => {
    const nodes: Placed[] = data.nodes.map((n) => ({
      id: n.id,
      kind: n.kind,
      region: n.region,
      title: n.title || n.id,
      degree: n.degree,
      x: n.x,
      y: n.y,
    }));
    const byId = new Map(nodes.map((n) => [n.id, n]));
    const neighbours = new Map<string, Set<string>>();
    for (const edge of data.edges) {
      if (!neighbours.has(edge.subject)) neighbours.set(edge.subject, new Set());
      if (!neighbours.has(edge.object)) neighbours.set(edge.object, new Set());
      neighbours.get(edge.subject)?.add(edge.object);
      neighbours.get(edge.object)?.add(edge.subject);
    }
    model.current = { nodes, byId, neighbours };
    // A record pulled aside is forgotten when the map is drawn again. Holding
    // it would be persisting a position, one poll at a time.
    moved.current.clear();
  }, [data]);

  /** The frame the picture is drawn in: where the middle of it is, and how far
   *  the furthest record sits from that. Recomputed when the QUESTION changes
   *  and not when the answer is trimmed, which is what keeps a filter from
   *  moving the picture under the hand that pressed it. */
  const frame = useRef({ cx: 0, cy: 0, halfX: 1, halfY: 1 });


  /** Where a record in the build's own units lands on the canvas. One
   *  function, because the picture and the pointer have to agree about where
   *  a dot is: two copies of this arithmetic is a map you cannot click. */
  const camera = useCallback((rect: DOMRect) => {
    const { cx, cy, halfX, halfY } = frame.current;
    const fit =
      Math.min(
        rect.width / (halfX * 2 * MARGIN),
        rect.height / (halfY * 2 * MARGIN),
      ) * view.current.k;
    return {
      fit,
      ox: rect.width / 2 - cx * fit + view.current.x,
      oy: rect.height / 2 - cy * fit + view.current.y,
    };
  }, []);

  /** Everything below paints through `draw`, which is `paint` at most once a
   *  frame.
   *
   *  A pointer move used to repaint synchronously, and a mouse reports faster
   *  than a screen refreshes, so a drag did the work two or three times for
   *  every frame anybody saw. That was affordable while the picture was 600
   *  records and 4 ms; asked for all 4,640 it is 26 ms, and three of those
   *  inside one frame is where a drag stops keeping up with the hand. */
  const pending = useRef(0);

  const paint = useCallback(() => {
    const canvas = ref.current;
    const ctx = canvas?.getContext('2d');
    if (!canvas || !ctx || !brand.current) return;
    const paints = brand.current;
    const { nodes, byId, neighbours } = model.current;

    const dpr = window.devicePixelRatio || 1;
    const rect = canvas.getBoundingClientRect();
    canvas.width = Math.max(1, Math.round(rect.width * dpr));
    canvas.height = Math.max(1, Math.round(rect.height * dpr));
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, rect.width, rect.height);

    const { fit, ox, oy } = camera(rect);
    // Where a record is RIGHT NOW: where the build put it, unless a hand has
    // pulled it aside this visit. One function, because the picture and the
    // pointer have to agree.
    const at = (n: Placed) => moved.current.get(n.id) ?? n;
    const px = (n: Placed) => at(n).x * fit + ox;
    const py = (n: Placed) => at(n).y * fit + oy;

    const near = selected ? (neighbours.get(selected) ?? new Set()) : null;
    const inHeld = (n: Placed) => !region || n.region === region;
    const here = shown.nodes;
    // The pinned ones. Named apart from the HELD COMPARTMENT above, which is
    // a different question with a similar word.
    const kept = new Set(pinned);

    ctx.textAlign = 'center';

    for (const edge of data.edges) {
      if (!shown.edges.has(edge.id)) continue;
      const a = byId.get(edge.subject);
      const b = byId.get(edge.object);
      if (!a || !b) continue;
      const touchesSelection =
        selected !== null && (a.id === selected || b.id === selected);
      if (touchesSelection) ctx.strokeStyle = paints.ink;
      else if (edge.crosses) ctx.strokeStyle = paints.crossing;
      else ctx.strokeStyle = paints.link;
      // A crossing link is drawn heavier as well as brighter, so it is still
      // the one that stands out where a picture is dense enough that colour
      // alone stops separating things.
      ctx.lineWidth = touchesSelection || edge.crosses ? LINE_STRONG : LINE;
      ctx.globalAlpha =
        region && !(inHeld(a) && inHeld(b)) && !touchesSelection
          ? FADE_OUT
          : 1;
      ctx.beginPath();
      ctx.moveTo(px(a), py(a));
      ctx.lineTo(px(b), py(b));
      ctx.stroke();
    }
    ctx.globalAlpha = 1;
    ctx.lineWidth = LINE;

    const radius = (n: Placed) =>
      Math.max(DOT_MIN, Math.min(DOT_MAX, DOT_MIN + n.degree * DOT_PER_LINK)) *
      Math.max(0.7, Math.min(1.6, fit));

    for (const n of nodes) {
      if (!here.has(n.id)) continue;
      const beside = near !== null && (n.id === selected || near.has(n.id));
      const inband = inHeld(n);
      ctx.globalAlpha = !inband
        ? FADE_OUT
        : near === null || n.id === selected
          ? 1
          : beside
            ? NEAR
            : FADE_ASIDE;
      // A neighbour of the selection is drawn a size up, so a neighbourhood
      // reads as a shape rather than as a change in opacity.
      const r = radius(n) * (beside && n.id !== selected ? 1.35 : 1);
      ctx.beginPath();
      mark(ctx, n.kind, px(n), py(n), r);
      ctx.fillStyle = paints.of(n.kind);
      ctx.fill();
      if (n.id === selected || n.id === hover.current) {
        ctx.lineWidth = 2;
        ctx.strokeStyle = paints.ink;
        ctx.stroke();
        ctx.lineWidth = LINE;
      }
      // A pinned record wears a ring a little off the dot, so that being
      // compared and being open are two different marks rather than the same
      // outline meaning two things.
      if (kept.has(n.id)) {
        ctx.beginPath();
        mark(ctx, n.kind, px(n), py(n), r + PIN_RING);
        ctx.strokeStyle = paints.label;
        ctx.stroke();
      }
    }
    ctx.globalAlpha = 1;

    // The names, and there are two of them at most: the record under the
    // pointer, and the record that is open.
    //
    // **This surface is read, not edited, and the map is drawn once a night.**
    // Nobody comes here to scan for a title they already know; they come to
    // see a shape and then ask about one thing in it. So a name at rest is
    // pure cost, and it was a large one: with a layout that settles what is
    // connected into the same cluster, every hub landed in one place and their
    // titles arrived as a block of text over the only part of the picture
    // worth looking at. The full name, its fields, where it came from and what
    // it touches are in the column beside the map, which is where a person
    // reads. Pinned records are not labelled either, because the chips in that
    // column already name them.
    // **Except on the way in.** Zooming into a cluster is asking what is in
    // it, so past `LABEL_FROM` the names come back — for what is drawn, in the
    // order the picture already ranks by, and only where one does not land on
    // another. Bounded by `LABEL_MAX` so the work per frame has a ceiling: at
    // rest this loop runs over two records and never measures any text.
    ctx.font = paints.caption;
    const taken: { x: number; y: number; w: number; h: number }[] = [];
    const write = (n: Placed, loud: boolean): void => {
      const x = px(n);
      const y = py(n) + radius(n) + NODE_LABEL_DROP;
      const w = ctx.measureText(n.title).width;
      const box = {
        x: x - w / 2 - LABEL_PAD,
        y: y - LABEL_LINE,
        w: w + LABEL_PAD * 2,
        h: LABEL_LINE + LABEL_PAD,
      };
      for (const other of taken) {
        if (
          box.x < other.x + other.w &&
          box.x + box.w > other.x &&
          box.y < other.y + other.h &&
          box.y + box.h > other.y
        ) {
          return;
        }
      }
      taken.push(box);
      ctx.fillStyle = loud ? paints.ink : paints.label;
      ctx.fillText(n.title, x, y);
    };

    // The two that are always named go first, so a name that is the answer to
    // a click never loses its place to one that arrived by zoom.
    const named = [hover.current, selected].filter(
      (id): id is string => id !== null && here.has(id),
    );
    for (const id of new Set(named)) {
      const n = byId.get(id);
      if (!n || !inHeld(n)) continue;
      write(n, true);
    }
    if (view.current.k >= LABEL_FROM) {
      for (const n of nodes) {
        if (taken.length >= LABEL_MAX) break;
        if (!here.has(n.id) || !inHeld(n) || named.includes(n.id)) continue;
        if (near !== null && !(n.id === selected || near.has(n.id))) continue;
        write(n, false);
      }
    }
  }, [data, shown, selected, region, pinned, camera]);

  /** One repaint per frame at most. Coalescing rather than throttling:
   *  the LAST state before the frame is what gets drawn, so the picture
   *  never lags behind the pointer, it simply stops drawing frames nobody
   *  was going to see. */
  const draw = useCallback(() => {
    if (pending.current) return;
    pending.current = window.requestAnimationFrame(() => {
      pending.current = 0;
      paint();
    });
  }, [paint]);

  // A frame still queued when the canvas goes away would paint into a context
  // that is no longer on the screen.
  //
  // **Clearing the id is the whole of it, not the cancel.** `draw` refuses to
  // schedule while one is pending, so a cancelled frame whose id was left
  // behind is a repaint that never happens again: React runs an effect,
  // cleans it up and runs it again on mount in development, which cancelled
  // the first frame and left the canvas at its untouched 300 by 150 default
  // with nothing drawn on it, for good.
  useEffect(
    () => () => {
      if (!pending.current) return;
      window.cancelAnimationFrame(pending.current);
      pending.current = 0;
    },
    [],
  );

  /** Fit the frame to what is on the picture, and put the view back to it.
   *  Called when the question changes, and when somebody asks for a record by
   *  name, which is the same act: a record found by name IS the new question,
   *  and the answer is that record with what it touches. Zooming to the dot
   *  instead left its neighbours off the edge, which is the half of the
   *  answer worth having. */
  const fitFrame = useCallback(() => {
    const { nodes, byId } = model.current;
    // Whatever the question put on the picture, or the whole map when it put
    // nothing there, so an empty answer still draws in a sane frame.
    const inFrame = shown.nodes.size > 0
      ? [...shown.nodes].map((id) => byId.get(id)).filter((n): n is Placed => !!n)
      : nodes;
    if (inFrame.length === 0) return;
    let minX = Infinity;
    let maxX = -Infinity;
    let minY = Infinity;
    let maxY = -Infinity;
    for (const n of inFrame) {
      minX = Math.min(minX, n.x);
      maxX = Math.max(maxX, n.x);
      minY = Math.min(minY, n.y);
      maxY = Math.max(maxY, n.y);
    }
    frame.current = {
      cx: (minX + maxX) / 2,
      cy: (minY + maxY) / 2,
      // Each axis on its own, so a wide short panel showing a wide short
      // cluster uses its width. One radius for both fitted the picture to
      // whichever side of the panel was tighter and left the other empty.
      halfX: Math.max((maxX - minX) / 2, 1),
      halfY: Math.max((maxY - minY) / 2, 1),
    };
    // Back to where the frame puts it, because a pan held from the last
    // question would open the new one somewhere off the edge.
    view.current = { k: 1, x: 0, y: 0 };
    draw();
    // `shown` is deliberately read and NOT depended on: the frame answers the
    // question, and every filter under one question draws inside the frame it
    // opened with, rather than jumping under the hand that pressed the chip.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draw]);

  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(fitFrame, [data, frameKey]);

  // What is under the pointer, in screen space so the target stays the same
  // size to the hand however far in the picture is taken.
  const pick = useCallback((clientX: number, clientY: number): string | null => {
    const canvas = ref.current;
    if (!canvas) return null;
    const rect = canvas.getBoundingClientRect();
    const mx = clientX - rect.left;
    const my = clientY - rect.top;
    const { fit, ox, oy } = camera(rect);
    let best: string | null = null;
    let bestDistance = HIT_RADIUS * HIT_RADIUS;
    for (const n of model.current.nodes) {
      // A record that is not drawn cannot be clicked. Without this the
      // filtered ones would still answer the pointer, and clicking empty
      // canvas would open something invisible.
      if (!shown.nodes.has(n.id)) continue;
      const here = moved.current.get(n.id) ?? n;
      const dx = here.x * fit + ox - mx;
      const dy = here.y * fit + oy - my;
      const distance = dx * dx + dy * dy;
      if (distance < bestDistance) {
        bestDistance = distance;
        best = n.id;
      }
    }
    return best;
  }, [shown, camera]);

  useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return undefined;
    brand.current = readBrand(canvas);
    draw();
    const onResize = () => {
      // Re-read here rather than per frame: `getComputedStyle` forces a style
      // recalculation, and a drag would pay for one sixty times a second to
      // learn nothing new.
      brand.current = readBrand(canvas);
      draw();
    };
    // Bound natively and NOT passively. React attaches `onWheel` as a passive
    // listener, where `preventDefault` is ignored, so zooming the picture
    // would also scroll the panel it is sitting in.
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      view.current.k = Math.max(
        ZOOM_MIN,
        Math.min(
          ZOOM_MAX,
          view.current.k * (e.deltaY < 0 ? ZOOM_STEP : 1 / ZOOM_STEP),
        ),
      );
      draw();
    };
    canvas.addEventListener('wheel', onWheel, { passive: false });
    window.addEventListener('resize', onResize);
    return () => {
      canvas.removeEventListener('wheel', onWheel);
      window.removeEventListener('resize', onResize);
    };
  }, [draw]);

  // Moving the picture to a record. The only thing that changes the view
  // without a hand on it, and it happens only when somebody asked for that
  // record by name. Keyed on the ask rather than on the selection: clicking a
  // dot must not pull the picture out from under the pointer.
  useEffect(() => {
    if (!focus) return;
    fitFrame();
  }, [focus, fitFrame]);

  const drag = useRef<{ x: number; y: number; vx: number; vy: number } | null>(
    null,
  );
  /** The record being pulled aside, and where it was when the hand landed on
   *  it. Kept beside the pan and the zoom, and cleared with them: see `moved`
   *  above for why nothing here is persisted. */
  const held = useRef<{ id: string; x: number; y: number } | null>(null);

  return (
    <canvas
      ref={ref}
      className="graph-canvas"
      role="img"
      aria-label={`${shown.nodes.size} records and the links between them, coloured by what kind of record each one is`}
      onPointerDown={(e) => {
        (e.target as HTMLCanvasElement).setPointerCapture(e.pointerId);
        // A hand on a record pulls THAT record; a hand on empty canvas pulls
        // the whole picture. One gesture, two meanings, and which it is was
        // decided the moment the pointer went down rather than by how far it
        // travelled afterwards.
        const under = pick(e.clientX, e.clientY);
        const node = under ? model.current.byId.get(under) : undefined;
        held.current = node ? { id: node.id, x: node.x, y: node.y } : null;
        drag.current = {
          x: e.clientX,
          y: e.clientY,
          vx: view.current.x,
          vy: view.current.y,
        };
      }}
      onPointerUp={(e) => {
        (e.target as HTMLCanvasElement).releasePointerCapture(e.pointerId);
        const moved =
          drag.current !== null &&
          Math.abs(e.clientX - drag.current.x) +
            Math.abs(e.clientY - drag.current.y) >
            3;
        drag.current = null;
        held.current = null;
        // A drag that ends over a record is a drag, not a click. Without this
        // every pan that finished on a dot would open it, and every record
        // pulled aside would open itself on the way.
        if (!moved) onSelect(pick(e.clientX, e.clientY));
      }}
      onPointerMove={(e) => {
        if (drag.current && held.current) {
          // In the build's own units, so the record moves with the hand at
          // every zoom. `moved` is the whole state this writes, and nothing
          // reads it but the next paint.
          const rect = (e.target as HTMLCanvasElement).getBoundingClientRect();
          const { fit } = camera(rect);
          moved.current.set(held.current.id, {
            x: held.current.x + (e.clientX - drag.current.x) / fit,
            y: held.current.y + (e.clientY - drag.current.y) / fit,
          });
          draw();
          return;
        }
        if (drag.current) {
          view.current.x = drag.current.vx + (e.clientX - drag.current.x);
          view.current.y = drag.current.vy + (e.clientY - drag.current.y);
          draw();
          return;
        }
        const hit = pick(e.clientX, e.clientY);
        if (hit !== hover.current) {
          hover.current = hit;
          draw();
        }
      }}
      onPointerLeave={() => {
        if (hover.current === null) return;
        hover.current = null;
        draw();
      }}
    />
  );
}

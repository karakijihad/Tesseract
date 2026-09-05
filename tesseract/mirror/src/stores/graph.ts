// The graph surface's one read.
//
// Its own store rather than a section of `stores/autonomy.ts`, because the
// autonomy store is the panel's rail: everything in it is fetched by
// `fetchAll` so a room's mark is current whether or not the room is open. This
// is a quarter of a megabyte and nothing on the rail reads it, so it is
// fetched when the surface is opened and not before.
//
// What is selected lives here too, for one reason: the canvas and the
// inspector beside it are two views of one selection, and a selection held in
// the canvas would have to be handed out on every render.

import { create } from 'zustand';

import {
  fetchGraph,
  fetchGraphRecord,
  type GraphRecord,
  type GraphResponse,
} from '../lib/api';
import { ENTRY, REACH_MAX, REACH_OF, type Lens, type Way } from '../views/graph/lens';

type Status = 'idle' | 'loading' | 'ready' | 'error';

interface GraphState {
  data: GraphResponse | null;
  status: Status;
  error: string | null;
  lastFetched: number | null;
  /** The record the inspector is showing, by id. `null` is the entry state. */
  selected: string | null;
  /** The compartment the picture is held on, by key. `null` is all four. */
  region: string | null;
  /** The open record itself, read one at a time. Separate from `selected`
   *  because the picture answers instantly and the file behind it does not:
   *  the inspector names what was clicked while it is still being read. */
  record: GraphRecord | null;
  recordStatus: Status;
  recordError: string | null;
  /** What the picture is showing: the way in, how far out it reaches, what is
   *  pinned, and what is filtered. One object because they are one question,
   *  and `views/graph/lens.ts` is the only thing that answers it. */
  lens: Lens;
  /** A record the picture has been asked to move to, and a number that
   *  changes every time it is asked. Without the number, finding the same
   *  record twice would not move a picture the operator had dragged away
   *  from it. */
  focus: { id: string; nonce: number } | null;
  /** How much of the map the reader has asked for: `''` is the ceiling the
   *  config sets, `'all'` is every record the graph holds, and a number is
   *  that many. Held in the store rather than in the surface because it
   *  decides what is FETCHED, not what is drawn from what arrived. */
  drawn: string;
  load: () => Promise<void>;
  /** Ask for a different amount and read it again. */
  drawAmount: (drawn: string) => void;
  select: (id: string | null) => void;
  holdOn: (region: string | null) => void;
  /** Take a way in. Each one brings its own reach, because a record alone is
   *  a dot and everything is already everything. */
  enter: (way: Way) => void;
  /** Open a record found by name, and move the picture to it. */
  find: (id: string) => void;
  /** Follow the links out one more step, or back one. */
  expand: (by: number) => void;
  /** Keep a record on the picture whatever else is shown. */
  pin: (id: string) => void;
  narrow: (patch: Partial<Lens>) => void;
  clearFilters: () => void;
  /** Back to choosing a way in. */
  restart: () => void;
}

function describe(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

export const useGraphStore = create<GraphState>((set, get) => ({
  data: null,
  status: 'idle',
  error: null,
  lastFetched: null,
  selected: null,
  region: null,
  record: null,
  recordStatus: 'idle',
  recordError: null,
  lens: ENTRY,
  focus: null,
  drawn: '',

  drawAmount: (drawn) => {
    if (get().drawn === drawn) return;
    set((s) => ({ ...s, drawn }));
    void get().load();
  },

  load: async () => {
    // A second call while one is in flight is a repeat of the same quarter of
    // a megabyte. The surface mounts, polls and re-reads after a redraw, so
    // this is reached more than once by design.
    if (get().status === 'loading') return;
    set((s) => ({ ...s, status: 'loading', error: null }));
    try {
      const data = await fetchGraph(get().drawn);
      set((s) => ({
        ...s,
        data,
        status: 'ready',
        error: null,
        lastFetched: Date.now(),
        // A redraw can drop the record that was open. Keeping the id would
        // leave the inspector showing a title for something no longer on the
        // map, which is worse than coming back to the entry state.
        selected:
          s.selected && data.nodes.some((n) => n.id === s.selected)
            ? s.selected
            : null,
        // A pin or a search that no longer names anything on the map is
        // dropped for the same reason: it would be a control pointing at a
        // record the picture cannot draw.
        lens: {
          ...s.lens,
          pinned: s.lens.pinned.filter((id) =>
            data.nodes.some((n) => n.id === id),
          ),
          named:
            s.lens.named && data.nodes.some((n) => n.id === s.lens.named)
              ? s.lens.named
              : null,
        },
      }));
    } catch (err) {
      set((s) => ({ ...s, status: 'error', error: describe(err) }));
    }
  },

  select: (id) => {
    set((s) => ({
      ...s,
      selected: id,
      // The previous record is dropped rather than left under a new name.
      record: null,
      recordStatus: id === null ? 'idle' : 'loading',
      recordError: null,
    }));
    if (id === null) return;
    void fetchGraphRecord(id)
      .then((record) => {
        // A record that arrives after the operator clicked something else is
        // the wrong record, and rendering it would name one thing and show
        // another.
        if (get().selected !== id) return;
        set((s) => ({ ...s, record, recordStatus: 'ready' }));
      })
      .catch((err) => {
        if (get().selected !== id) return;
        set((s) => ({
          ...s,
          recordStatus: 'error',
          recordError: describe(err),
        }));
      });
  },
  holdOn: (region) => set((s) => ({ ...s, region })),

  // A different way in is a different question, so whatever was open closes
  // with it: an inspector describing a record the new picture does not draw
  // names something the operator cannot see.
  enter: (way) =>
    set((s) => ({
      ...s,
      lens: { ...s.lens, way, named: null, reach: REACH_OF[way] },
      selected: null,
      record: null,
      recordStatus: 'idle',
      recordError: null,
    })),

  find: (id) => {
    set((s) => ({
      ...s,
      lens: { ...s.lens, way: 'record', named: id, reach: REACH_OF.record },
      focus: { id, nonce: (s.focus?.nonce ?? 0) + 1 },
    }));
    get().select(id);
  },

  expand: (by) =>
    set((s) => ({
      ...s,
      lens: {
        ...s.lens,
        reach: Math.max(0, Math.min(REACH_MAX, s.lens.reach + by)),
      },
    })),

  pin: (id) =>
    set((s) => ({
      ...s,
      lens: {
        ...s.lens,
        pinned: s.lens.pinned.includes(id)
          ? s.lens.pinned.filter((one) => one !== id)
          : [...s.lens.pinned, id],
      },
    })),

  narrow: (patch) => set((s) => ({ ...s, lens: { ...s.lens, ...patch } })),

  clearFilters: () =>
    set((s) => ({
      ...s,
      lens: {
        ...s.lens,
        kinds: [],
        since: null,
        provenance: [],
        linkTypes: [],
        crossingOnly: false,
        newOnly: false,
      },
    })),

  // The pins go too. Starting again means the picture is empty, and a pin
  // surviving it would draw two records over the entry state.
  restart: () =>
    set((s) => ({ ...s, lens: ENTRY, selected: null, record: null, focus: null })),
}));

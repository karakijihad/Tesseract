// What the runtime last said about the state of a thing, and whether it is
// still saying anything.
//
// Every room on the Autonomy panel used to be REST-fed and polled, so a state
// changed on screen because the room asked again. `mirror/server/liveness_feed.py`
// pushes state changes instead, and this holds them. One reader for all of
// them: a room that decided for itself what a socket going quiet means would
// be the second definition of that, and the first one to be wrong.
//
// **Three rules, and none of them invents a state.**
//
// A row nothing produces is left exactly as it is. `not_instrumented` is a
// fact about the code rather than about now, and it stays true whether or not
// the runtime is reachable.
//
// A row that IS produced reads `unknown` once the runtime has stopped reaching
// us AND nothing has been read since. It has stopped reaching us when the
// socket drops, when a message goes missing, and when the whole-of-it sweep
// simply stops arriving: the feed sends one on a known cadence and says what
// that cadence is, so silence past it is a fact rather than a guess. Not the last state it had: the whole
// point of this layer is that the panel stops guessing, and the last thing it
// heard is a guess the moment it stops hearing. A room that has read since is
// not guessing, so it keeps what it read: the socket is how a state arrives
// between reads, not the only way one can. The word for `unknown` comes with
// the payload the room already has, because a state named in TSX is a second
// place a state is named.
//
// What was pushed wins only while it is newer than what the room last read.
// A feed that dies must not pin a panel to the state it froze at, so the
// room's own read takes over the moment it is the fresher of the two.

import { create } from 'zustand';
import type { MapLiveness, OperationalState } from '../lib/api';

/** The half of a row that IS a state. A department row also carries a
 *  sentence, a number and the band it is grouped under; those are the row and
 *  they arrive with the room's own read, so nothing here has to invent one. */
export type LiveState = Pick<MapLiveness, 'state' | 'label' | 'observedAt' | 'reason'>;

/** One thing's state, and when this session was told about it. */
interface Held {
  liveness: MapLiveness;
  at: number;
}

interface LivenessState {
  states: Record<string, Held>;
  /** The last sequence number seen. The feed counts its own messages so a
   *  dropped one is detectable rather than silently absorbed. */
  seq: number | null;
  /** When the runtime stopped being a source we can rely on: the socket
   *  dropped, or a message went missing. Null while it is one. Anything read
   *  before this moment is information nothing has confirmed since. */
  staleSince: number | null;
  /** When the whole of it last arrived, and how often the runtime says it
   *  will. What acts on them is the timer below, which turns the deadline into
   *  `staleSince` the moment it passes rather than waiting for something else
   *  to repaint; they are held because AR-8b's age-on-every-number reads them
   *  and because a reader cannot ask the timer how long it has left. */
  lastFullAt: number | null;
  sweepSeconds: number | null;
  apply: (
    seq: number,
    full: boolean,
    states: Record<string, MapLiveness>,
    sweepSeconds?: number,
  ) => void;
  /** Forget everything. The socket dropped, so nothing held is current. */
  clear: () => void;
}

export const useLivenessStore = create<LivenessState>((set) => ({
  states: {},
  seq: null,
  staleSince: null,
  lastFullAt: null,
  sweepSeconds: null,
  apply: (seq, full, states, sweepSeconds) => {
    // Before the write, not inside it: a store updater that also arms a timer
    // is a side effect in a place nothing expects one.
    const cadence =
      typeof sweepSeconds === "number" && sweepSeconds > 0
        ? sweepSeconds
        : useLivenessStore.getState().sweepSeconds;
    // Only the WHOLE of it restarts the clock. The deadline is a claim about
    // the sweep, not about the socket carrying traffic: the fast pass and the
    // sweep are separate reads on the backend and either can fail on its own,
    // so a sweep that has stopped while ticks keep arriving is exactly the
    // case this exists to catch. Re-arming on a change would have let those
    // ticks hold the deadline open for ever.
    if (full) watchForSilence(cadence);
    set((s) => {
      const at = Date.now();
      const held: Record<string, Held> = {};
      for (const [key, liveness] of Object.entries(states)) {
        held[key] = { liveness, at };
      }
      // The whole of it. Whatever was missed before this is answered by it,
      // which is what the sweep is for.
      if (full) {
        return {
          states: held,
          seq,
          staleSince: null,
          lastFullAt: at,
          sweepSeconds: cadence,
        };
      }
      if (s.seq !== null && seq !== s.seq + 1) {
        // Something was dropped, so what is held may be a state that has since
        // changed. Held values are let go rather than kept and flagged: a
        // panel must not be able to draw one.
        return { states: {}, seq, staleSince: at, sweepSeconds: cadence };
      }
      return { states: { ...s.states, ...held }, seq, sweepSeconds: cadence };
    });
  },
  clear: () => {
    watchForSilence(null);
    set({
      states: {},
      seq: null,
      staleSince: Date.now(),
      lastFullAt: null,
      sweepSeconds: null,
    });
  },
}));

/** How many sweeps may be missed before silence is read as the feed having
 *  stopped. Two and a half rather than one: a sweep arriving a little late is
 *  an ordinary thing on a busy machine, and calling that a failure would put
 *  the panel into `unknown` every time the loop was briefly loaded. */
const MISSED_SWEEPS = 2.5;

/** The one-shot that turns the deadline into a fact.
 *
 * Deciding it at render time was not enough: nothing re-renders a panel that
 * is open and idle, so a frozen state could outlive its own declared cutoff
 * by as long as it took a room to poll. The timer makes the flip happen when
 * it is due, and the rooms then draw it because the store changed.
 */
let _silence: ReturnType<typeof setTimeout> | null = null;

function watchForSilence(sweepSeconds: number | null): void {
  if (_silence !== null) {
    clearTimeout(_silence);
    _silence = null;
  }
  if (sweepSeconds === null || sweepSeconds <= 0) return;
  _silence = setTimeout(
    () => {
      _silence = null;
      // Only if nothing has arrived since. `apply` re-arms this on every
      // message, so reaching here means the feed has said nothing for the
      // whole window.
      useLivenessStore.setState({ staleSince: Date.now() });
    },
    sweepSeconds * MISSED_SWEEPS * 1_000,
  );
}

/** What a caller needs to read a state, gathered once per render. */
export interface LiveContext {
  states: Record<string, Held>;
  /** When the runtime stopped telling us things, or null while it is. One
   *  signal for all three ways that happens: the socket dropped, a message
   *  went missing, or the sweeps stopped arriving and the deadline passed. */
  staleSince: number | null;
  /** Every state's word, from the payload the room already holds. */
  labels: Record<string, string>;
}

/** Nothing pushed, and nothing missed. What a view renders against when it is
 *  handed no context: exactly what it was given. */
export const NOTHING_PUSHED: LiveContext = {
  states: {},
  staleSince: null,
  labels: {},
};

/**
 * How one thing reads now: what the runtime last said, what the room last
 * read, or that it is not known.
 *
 * `restAt` is when the room's own payload was read. It decides which of the
 * two answers is the more recent, which is what stops a stopped feed from
 * outranking a room that is still reading.
 */
export function live<T extends LiveState>(
  ctx: LiveContext,
  key: string,
  rest: T,
  restAt: number | null,
): T {
  if (rest.state === 'not_instrumented') return rest;
  // One signal, whether the socket dropped, a message went missing, or the
  // sweeps simply stopped arriving. All three mean the same thing: nothing is
  // telling us any more, and a reading taken before that moment is a reading
  // nothing has confirmed since.
  const unheard =
    ctx.staleSince !== null && (restAt === null || restAt < ctx.staleSince);
  if (unheard) {
    const state: OperationalState = 'unknown';
    return {
      ...rest,
      state,
      label: ctx.labels[state] ?? rest.label,
      // `observedAt` is kept, because when it was last seen is still true and
      // is the thing that says how much not knowing matters. The reason is
      // not: it explained a state this no longer is.
      reason: null,
    };
  }
  const held = ctx.states[key];
  if (!held) return rest;
  if (restAt !== null && restAt > held.at) return rest;
  // What was pushed over what the caller holds, so a row keeps whatever else
  // it carries and only the state moves.
  return { ...rest, ...held.liveness };
}

/** The reading context, gathered from the socket and the payload in hand.
 *
 * `labels` rides in from whichever payload the caller is drawing, because the
 * words for the states belong to the backend and every one of those payloads
 * carries the whole table for exactly this: the panel has to be able to say
 * "not known" at the moment it has stopped being able to ask.
 */
export function useLiveContext(labels?: Record<string, string>): LiveContext {
  const states = useLivenessStore((s) => s.states);
  const staleSince = useLivenessStore((s) => s.staleSince);
  return { states, staleSince, labels: labels ?? {} };
}

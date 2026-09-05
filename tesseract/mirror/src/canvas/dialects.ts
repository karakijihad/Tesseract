// What a framed page understands, per page.
//
// The general shape, and the reason this is a table rather than a branch:
// there are only three kinds of thing on the far side of a card, and only one
// of them is special-cased anywhere.
//
//   an element we own      -> call a method            (useMediaCommands)
//   a frame that listens   -> postMessage in its dialect   (this file)
//   a frame that does not  -> say so, and stop
//
// The CHANNEL is universal: a parent can `postMessage` into any iframe, and a
// sandboxed frame can post back, whatever its sandbox says. What differs per
// page is only the wording, so a dialect is a translation of one fixed verb
// set into whatever that page's authors chose to accept. Supporting a new
// player is a row here, not new machinery.
//
// The sandbox is not what limits this. `allow-same-origin` is about whether a
// player can BOOT (it reads storage on start), not about whether it can be
// spoken to, and it stays narrow for the reason `WebViewRenderer` gives.

export type MediaAction =
  | 'play'
  | 'pause'
  | 'mute'
  | 'unmute'
  | 'volume'
  | 'seek'
  | 'read';

export interface FrameState {
  paused?: boolean;
  muted?: boolean;
  volume?: number;
  position?: number;
  duration?: number;
}

export interface Dialect {
  /** What to call it when reporting to the operator. */
  readonly name: string;
  /** Does this dialect speak for the page at this URL? */
  matches(url: URL): boolean;
  /** Anything the URL needs before the page will listen at all. */
  prepare?(url: URL): URL;
  /** The messages that ask for one action. More than one where the API
   *  splits what we treat as a single verb. Empty means "cannot express it". */
  encode(action: MediaAction, value: number | null): unknown[];
  /** Sent once when the frame loads, if the API needs subscribing to. */
  subscribe?(): unknown[];
  /** Read state out of a message the page sent us, or null if it carried
   *  none. Called for every message from that frame. */
  decode(message: unknown): FrameState | null;
}

// ── YouTube ──────────────────────────────────────────────────────────
// The IFrame Player API. Commands are `{event:"command", func, args}`; it only
// listens at all when the embed URL carries `enablejsapi=1`, and it only
// reports state after being sent `{event:"listening"}`.

const YOUTUBE_STATES: Record<number, boolean> = {
  1: false, // playing
  2: true, // paused
  0: true, // ended reads as paused, which is what a reader wants to know
};

const youtube: Dialect = {
  name: 'YouTube',
  matches: (u) =>
    (u.hostname === 'www.youtube.com' || u.hostname === 'www.youtube-nocookie.com') &&
    u.pathname.startsWith('/embed/'),
  prepare: (u) => {
    const next = new URL(u.toString());
    next.searchParams.set('enablejsapi', '1');
    return next;
  },
  encode: (action, value) => {
    const cmd = (func: string, args: unknown[] = []) => ({
      event: 'command',
      func,
      args,
    });
    switch (action) {
      case 'play':
        return [cmd('playVideo')];
      case 'pause':
        return [cmd('pauseVideo')];
      case 'mute':
        return [cmd('mute')];
      case 'unmute':
        return [cmd('unMute')];
      case 'volume':
        // Unmuting too: turning it up on a muted player changes nothing
        // audible, which reads as the command having been ignored.
        return [cmd('unMute'), cmd('setVolume', [Math.round((value ?? 0) * 100)])];
      case 'seek':
        return [cmd('seekTo', [value ?? 0, true])];
      case 'read':
        return [];
    }
  },
  subscribe: () => [{ event: 'listening' }],
  decode: (message) => {
    const m = message as { event?: string; info?: Record<string, unknown> } | undefined;
    const info = m?.info;
    if (!info) return null;
    const state: FrameState = {};
    if (typeof info.playerState === 'number' && info.playerState in YOUTUBE_STATES) {
      state.paused = YOUTUBE_STATES[info.playerState];
    }
    if (typeof info.muted === 'boolean') state.muted = info.muted;
    if (typeof info.volume === 'number') state.volume = info.volume / 100;
    if (typeof info.currentTime === 'number') state.position = info.currentTime;
    if (typeof info.duration === 'number') state.duration = info.duration;
    return Object.keys(state).length ? state : null;
  },
};

// ── Vimeo ────────────────────────────────────────────────────────────
// The Player API. `{method, value}`, listening needs no URL parameter, and
// events are subscribed to one at a time.

const vimeo: Dialect = {
  name: 'Vimeo',
  matches: (u) => u.hostname === 'player.vimeo.com' && u.pathname.startsWith('/video/'),
  encode: (action, value) => {
    switch (action) {
      case 'play':
        return [{ method: 'play' }];
      case 'pause':
        return [{ method: 'pause' }];
      case 'mute':
        return [{ method: 'setMuted', value: true }];
      case 'unmute':
        return [{ method: 'setMuted', value: false }];
      case 'volume':
        return [{ method: 'setMuted', value: false }, { method: 'setVolume', value: value ?? 0 }];
      case 'seek':
        return [{ method: 'setCurrentTime', value: value ?? 0 }];
      case 'read':
        return [{ method: 'getPaused' }, { method: 'getVolume' }, { method: 'getCurrentTime' }];
    }
  },
  subscribe: () => [
    { method: 'addEventListener', value: 'play' },
    { method: 'addEventListener', value: 'pause' },
    { method: 'addEventListener', value: 'volumechange' },
    { method: 'addEventListener', value: 'timeupdate' },
  ],
  decode: (message) => {
    const m = message as
      | { event?: string; method?: string; data?: Record<string, unknown> | number | boolean }
      | undefined;
    if (!m) return null;
    const state: FrameState = {};
    if (m.event === 'play') state.paused = false;
    if (m.event === 'pause') state.paused = true;
    const d = m.data;
    if (d && typeof d === 'object') {
      if (typeof d.volume === 'number') state.volume = d.volume;
      if (typeof d.seconds === 'number') state.position = d.seconds;
      if (typeof d.duration === 'number') state.duration = d.duration;
    }
    // A `getX` reply carries the bare value under the method it answers.
    if (m.method === 'getPaused' && typeof d === 'boolean') state.paused = d;
    if (m.method === 'getVolume' && typeof d === 'number') state.volume = d;
    if (m.method === 'getCurrentTime' && typeof d === 'number') state.position = d;
    return Object.keys(state).length ? state : null;
  },
};

const DIALECTS: readonly Dialect[] = [youtube, vimeo];

/** The dialect for a framed page, or null when nothing here speaks to it.
 *  Null is the honest answer and the tool reports it as one: most of the web
 *  exposes no way in, and pretending otherwise is how a command goes out and
 *  silently does nothing. */
export function dialectFor(rawUrl: string): Dialect | null {
  try {
    const u = new URL(rawUrl);
    return DIALECTS.find((d) => d.matches(u)) ?? null;
  } catch {
    return null;
  }
}

/** Apply whatever a dialect needs in the URL before the page will listen. */
export function prepareFrameUrl(rawUrl: string): string {
  try {
    const u = new URL(rawUrl);
    const d = DIALECTS.find((x) => x.matches(u));
    return d?.prepare ? d.prepare(u).toString() : rawUrl;
  } catch {
    return rawUrl;
  }
}

export const KNOWN_DIALECTS = DIALECTS.map((d) => d.name);

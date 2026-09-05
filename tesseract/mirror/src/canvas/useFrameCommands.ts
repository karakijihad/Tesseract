// The adapter that makes a framed player obey.
//
// Same command shape and same channel as `useMediaCommands`; the difference is
// that we cannot call a method on the far side, only speak to it. So the verb
// is translated by the page's dialect and posted in, and the page's own
// messages are read back for state.
//
// **State is what the player last told us, not a fresh read.** These APIs are
// event-based: there is no call that returns the current position. So the card
// subscribes when it mounts and keeps what arrives, and an answer carries the
// most recent of that. Reporting it as though it were read at that instant
// would be a small lie the assistant would then repeat to the operator.

import { useEffect, useRef } from 'react';

import { answerCommand, useCommandStore, type SurfaceCommand } from './commands';
import { dialectFor, type FrameState, type MediaAction } from './dialects';

const MEDIA_ACTIONS = new Set<string>([
  'play',
  'pause',
  'mute',
  'unmute',
  'volume',
  'seek',
  'read',
]);

/** How long to let a player's own message arrive after a command before
 *  answering with what we already had. Long enough for a local frame to
 *  respond, short enough that the tool is not waiting on it. */
const SETTLE_MS = 250;

export function useFrameCommands(
  surfaceId: string,
  view: string,
  url: string,
  ref: React.RefObject<HTMLIFrameElement | null>,
): void {
  // The player's last reported state, kept outside React: it changes on every
  // timeupdate and nothing on screen reads it.
  const known = useRef<FrameState>({});

  useEffect(() => {
    const dialect = dialectFor(url);
    let disposed = false;
    known.current = {};

    const post = (messages: unknown[]) => {
      const frame = ref.current?.contentWindow;
      if (!frame) return false;
      for (const m of messages) {
        // `*` because a sandboxed frame has an opaque origin and a specific
        // target origin would never match. The reply side is what is checked:
        // `onMessage` below ignores anything not from this frame.
        frame.postMessage(JSON.stringify(m), '*');
      }
      return true;
    };

    const onMessage = (ev: MessageEvent) => {
      if (!dialect || disposed) return;
      // The only check that matters. Origin cannot be used (opaque under
      // sandbox), so identity is: did THIS frame send it.
      if (!ref.current || ev.source !== ref.current.contentWindow) return;
      let payload: unknown = ev.data;
      if (typeof payload === 'string') {
        try {
          payload = JSON.parse(payload);
        } catch {
          return;
        }
      }
      const state = dialect.decode(payload);
      if (state) known.current = { ...known.current, ...state };
    };

    window.addEventListener('message', onMessage);

    // Subscribing has to wait for the player to exist inside the frame. One
    // retry ladder rather than a load event, because a cross-origin frame's
    // load fires before its own script has booted.
    const timers: number[] = [];
    if (dialect?.subscribe) {
      for (const delay of [0, 300, 1200]) {
        timers.push(window.setTimeout(() => post(dialect.subscribe!()), delay));
      }
    }

    const act = (cmd: SurfaceCommand) => {
      if (!dialect) {
        void answerCommand(view, cmd.command_id, {
          ok: false,
          error:
            'this page offers no way to control it from outside, so nothing ' +
            'was sent. Only embedded players that publish an API can be driven.',
        });
        return;
      }
      if (!MEDIA_ACTIONS.has(cmd.action)) {
        void answerCommand(view, cmd.command_id, {
          ok: false,
          error: `a ${dialect.name} player cannot ${cmd.action}`,
        });
        return;
      }
      const value = typeof cmd.value === 'number' ? cmd.value : Number(cmd.value);
      const messages = dialect.encode(
        cmd.action as MediaAction,
        Number.isFinite(value) ? value : null,
      );
      if (messages.length && !post(messages)) {
        void answerCommand(view, cmd.command_id, {
          ok: false,
          error: 'the card is on the canvas but its frame has not loaded yet',
        });
        return;
      }
      // Optimistic for the two the player will not always report back, so a
      // pause is not answered with "playing" from a stale timeupdate.
      if (cmd.action === 'play') known.current.paused = false;
      if (cmd.action === 'pause') known.current.paused = true;
      if (cmd.action === 'mute') known.current.muted = true;
      if (cmd.action === 'unmute') known.current.muted = false;

      timers.push(
        window.setTimeout(() => {
          if (disposed) return;
          void answerCommand(view, cmd.command_id, {
            ok: true,
            state: { ...known.current, player: dialect.name, reported: true },
          });
        }, SETTLE_MS),
      );
    };

    const claim = () => {
      if (disposed) return;
      const cmd = useCommandStore.getState().take(surfaceId);
      if (cmd) act(cmd);
    };

    claim();
    // Subscribed rather than rendered, for the reason `useMediaCommands`
    // gives: claiming a command mutates the store, and an effect keyed on that
    // value would cancel the work it had just started.
    const unsubscribe = useCommandStore.subscribe((s, prev) => {
      if (s.pending[surfaceId] && s.pending[surfaceId] !== prev.pending[surfaceId]) {
        claim();
      }
    });

    return () => {
      disposed = true;
      unsubscribe();
      window.removeEventListener('message', onMessage);
      for (const t of timers) window.clearTimeout(t);
    };
  }, [surfaceId, view, url, ref]);
}

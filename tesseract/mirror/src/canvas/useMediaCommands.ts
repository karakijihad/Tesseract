// The adapter that makes a local video or song obey.
//
// This is the cheap case and the reason is structural: `VideoRenderer` and
// `AudioRenderer` mount a real `<video>` / `<audio>` in the app's OWN DOM.
// There is no iframe, no sandbox and nothing to negotiate with, so operating
// one is calling a method on an element we already hold a ref to.
//
// The other two adapters (an embedded player's API, an authored page's bridge)
// answer the same command shape on the same channel; only `apply` differs.

import { useEffect } from 'react';

import { answerCommand, useCommandStore, type SurfaceCommand } from './commands';

/** What the card reads after acting. The tool relays this, which is what
 *  removes the "take a screenshot to check" round trip. */
function readState(el: HTMLMediaElement): Record<string, unknown> {
  return {
    paused: el.paused,
    muted: el.muted,
    volume: Number(el.volume.toFixed(2)),
    position: Number(el.currentTime.toFixed(2)),
    duration: Number.isFinite(el.duration) ? Number(el.duration.toFixed(2)) : null,
  };
}

async function apply(el: HTMLMediaElement, cmd: SurfaceCommand): Promise<string> {
  const n = typeof cmd.value === 'number' ? cmd.value : Number(cmd.value);
  switch (cmd.action) {
    case 'play':
      // Autoplay policy can refuse this, and refusing silently would leave the
      // assistant reporting that it played something the operator cannot hear.
      await el.play();
      return '';
    case 'pause':
      el.pause();
      return '';
    case 'mute':
      el.muted = true;
      return '';
    case 'unmute':
      el.muted = false;
      return '';
    case 'volume':
      if (!Number.isFinite(n)) return 'volume needs a number between 0 and 1';
      el.volume = Math.min(1, Math.max(0, n));
      // Turning the volume up on a muted element does nothing audible, which
      // reads as the command being ignored.
      if (el.volume > 0) el.muted = false;
      return '';
    case 'seek':
      if (!Number.isFinite(n)) return 'seek needs a position in seconds';
      el.currentTime = Math.max(0, n);
      return '';
    case 'read':
      return '';
    default:
      return `a media card cannot ${cmd.action}`;
  }
}

/** Subscribe one media card to commands aimed at it.
 *
 * **Subscribed, not rendered.** Reading `pending[surfaceId]` as a hook value
 * and acting in an effect looks equivalent and is not: claiming the command
 * mutates the store, which changes that value, which re-runs the effect, whose
 * cleanup then cancels the work the first run had just started. The element
 * moved and the answer never went back, so the tool timed out on a command
 * that had in fact been obeyed. Subscribing sidesteps the render cycle
 * entirely — acting on a command is not a reason to re-render a player.
 */
export function useMediaCommands(
  surfaceId: string,
  view: string,
  ref: React.RefObject<HTMLMediaElement | null>,
): void {
  useEffect(() => {
    let disposed = false;

    const act = (cmd: SurfaceCommand) => {
      const el = ref.current;
      if (!el) {
        void answerCommand(view, cmd.command_id, {
          ok: false,
          error: 'the card is on the canvas but its player has not mounted yet',
        });
        return;
      }
      void (async () => {
        let error = '';
        try {
          error = await apply(el, cmd);
        } catch (err) {
          // The autoplay refusal lands here, and its message is the useful part.
          error = err instanceof Error ? err.message : String(err);
        }
        // Answered even after unmount: the element already moved, and a tool
        // waiting on it deserves the outcome rather than a timeout.
        void answerCommand(view, cmd.command_id, {
          ok: !error,
          state: readState(el),
          error,
        });
      })();
    };

    const claim = () => {
      if (disposed) return;
      const cmd = useCommandStore.getState().take(surfaceId);
      if (cmd) act(cmd);
    };

    // One already waiting when this card mounted.
    claim();
    const unsubscribe = useCommandStore.subscribe((s, prev) => {
      if (s.pending[surfaceId] && s.pending[surfaceId] !== prev.pending[surfaceId]) {
        claim();
      }
    });
    return () => {
      disposed = true;
      unsubscribe();
    };
  }, [surfaceId, view, ref]);
}

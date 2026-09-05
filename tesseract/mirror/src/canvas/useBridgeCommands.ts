// The adapter for an authored page, and the only one that carries traffic in
// both directions.
//
// The media adapter calls a method on an element we own. The frame adapter
// speaks a player's dialect and hears whatever that player chooses to report.
// This one talks to a listener we injected, so the shape is ours on both ends:
// a command goes in with the id the tool is waiting on, the answer comes back
// under the same id, and anything the operator does in the page arrives on the
// operator-event route the canvas has always had.
//
// The identity check is the same one `useFrameCommands` gives: an authored
// page runs in an opaque origin under `sandbox="allow-scripts"`, so `ev.origin`
// is useless and the question that can be answered is whether THIS frame sent
// it.

import { useEffect, useRef } from 'react';

import { answerCommand, useCommandStore, type SurfaceCommand } from './commands';
import { emitSurfaceEvent } from './protocol/events';

/** How long to wait for the injected listener before answering for it. Under
 *  the tool's own timeout on purpose: a page that lost its listener should get
 *  a sentence saying so rather than the tool's blanket "nothing answered". */
const ANSWER_MS = 1500;

export function useBridgeCommands(
  surfaceId: string,
  view: string,
  ref: React.RefObject<HTMLIFrameElement | null>,
): void {
  // Ids this card is waiting on. Outside React: nothing on screen reads it.
  const waiting = useRef<Set<string>>(new Set());

  useEffect(() => {
    let disposed = false;
    const timers: number[] = [];
    const pending = waiting.current;
    pending.clear();

    const onMessage = (ev: MessageEvent) => {
      if (disposed || !ref.current || ev.source !== ref.current.contentWindow) return;
      let d: unknown = ev.data;
      if (typeof d === 'string') {
        try {
          d = JSON.parse(d);
        } catch {
          return;
        }
      }
      const msg = d as Record<string, unknown> | null;
      if (!msg) return;
      if (msg.tesseract === 'result') {
        const id = String(msg.id ?? '');
        if (!pending.delete(id)) return;
        void answerCommand(view, id, {
          ok: Boolean(msg.ok),
          state: (msg.state as Record<string, unknown>) ?? {},
          error: String(msg.error ?? ''),
        });
        return;
      }
      if (msg.tesseract === 'event' && (msg.event === 'clicked' || msg.event === 'edited')) {
        void emitSurfaceEvent(view, surfaceId, msg.event, {
          target: String(msg.target ?? ''),
          value: msg.value ?? null,
        });
      }
    };

    window.addEventListener('message', onMessage);

    const act = (cmd: SurfaceCommand) => {
      const frame = ref.current?.contentWindow;
      if (!frame) {
        void answerCommand(view, cmd.command_id, {
          ok: false,
          error: 'the card is on the canvas but its page has not loaded yet',
        });
        return;
      }
      pending.add(cmd.command_id);
      frame.postMessage(
        JSON.stringify({
          tesseract: 'command',
          id: cmd.command_id,
          action: cmd.action,
          target: cmd.target ?? '',
          value: cmd.value ?? null,
        }),
        '*',
      );
      timers.push(
        window.setTimeout(() => {
          if (disposed || !pending.delete(cmd.command_id)) return;
          void answerCommand(view, cmd.command_id, {
            ok: false,
            error:
              'this page did not answer. Markup that replaces the whole ' +
              'document at runtime drops the part that listens.',
          });
        }, ANSWER_MS),
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
      pending.clear();
    };
  }, [surfaceId, view, ref]);
}

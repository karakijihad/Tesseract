// Commands arriving for a card, and the answer going back.
//
// `reduceSurfaceEvent` is a pure reducer over the surface MAP, and a command
// is not a map mutation — it has to reach the one renderer instance that owns
// the card and can act on its element. So commands land here instead, keyed by
// surface, and a renderer takes its own.
//
// One pending command per card, deliberately. A second arriving before the
// first is taken replaces it: the newest instruction is the one the operator
// just gave, and answering a superseded "pause" after a "play" is worse than
// dropping it. The superseded one is answered as such so the tool waiting on
// it wakes rather than timing out.

import { create } from 'zustand';

import { BACKEND_BASE } from '../lib/endpoints';

export interface SurfaceCommand {
  command_id: string;
  surface_id: string;
  /** Carried on the command rather than looked up: the answer has to go back
   *  to the view the command came from, and a card can be closed between the
   *  command arriving and the answer being posted. */
  view: string;
  action: string;
  value?: number | string | null;
  target?: string | null;
}

export interface CommandResult {
  ok: boolean;
  state?: Record<string, unknown>;
  error?: string;
}

interface CommandStore {
  /** The command each card has been handed and not yet acted on. */
  pending: Record<string, SurfaceCommand>;
  push: (cmd: SurfaceCommand) => void;
  /** A renderer claiming its card's command, which clears it. */
  take: (surfaceId: string) => SurfaceCommand | null;
  clear: () => void;
}

export const useCommandStore = create<CommandStore>((set, get) => ({
  pending: {},
  push: (cmd) =>
    set((s) => {
      const superseded = s.pending[cmd.surface_id];
      if (superseded) {
        void answerCommand(superseded.view, superseded.command_id, {
          ok: false,
          error: 'superseded by a newer command',
        });
      }
      return { pending: { ...s.pending, [cmd.surface_id]: cmd } };
    }),
  take: (surfaceId) => {
    const cmd = get().pending[surfaceId];
    if (!cmd) return null;
    set((s) => {
      const next = { ...s.pending };
      delete next[surfaceId];
      return { pending: next };
    });
    return cmd;
  },
  clear: () => set({ pending: {} }),
}));

/** Post a card's answer back against the id it was asked with.
 *
 * Fire-and-forget in the same sense `reportSurfaceRender` is: a dropped answer
 * costs the waiting tool a timeout and a sentence saying nothing answered,
 * which is honest, where a thrown error here would lose the action that
 * already happened. */
export async function answerCommand(
  view: string,
  commandId: string,
  result: CommandResult,
): Promise<void> {
  const url = `${BACKEND_BASE}/api/surfaces/${encodeURIComponent(view)}/command-result`;
  try {
    await fetch(url, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        command_id: commandId,
        ok: result.ok,
        state: result.state ?? {},
        error: result.error ?? '',
      }),
    });
  } catch (err) {
    console.error('surface: command answer threw', err);
  }
}

/** Route one inbound `surface_command` envelope to the card it names. */
export function handleSurfaceCommand(data: unknown, view: string): void {
  const d = data as Partial<SurfaceCommand> | undefined;
  if (!d?.command_id || !d.surface_id || !d.action) return;
  useCommandStore.getState().push({
    command_id: d.command_id,
    surface_id: d.surface_id,
    view: d.view || view,
    action: d.action,
    value: d.value ?? null,
    target: d.target ?? null,
  });
}

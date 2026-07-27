import { create } from 'zustand';

import { isTauri } from '../lib/endpoints';
import { applyUpdate, checkUpdate } from '../lib/update';

interface UpdateStoreState {
  version: string | null;
  behind: number;
  summaries: string[];
  checking: boolean;
  applying: boolean;
  error: string | null;
  check: () => Promise<void>;
  apply: () => Promise<void>;
}

// Tauri command errors reject with a plain string (serde Err(String)), not
// an Error instance — surface it as-is rather than "[object Object]".
function readableError(err: unknown): string {
  if (typeof err === 'string') return err;
  if (err instanceof Error) return err.message;
  return 'update check failed';
}

export const useUpdateStore = create<UpdateStoreState>((set, get) => ({
  version: null,
  behind: 0,
  summaries: [],
  checking: false,
  applying: false,
  error: null,

  check: async () => {
    // Guards against a browser dev session (no Tauri IPC bridge) and against
    // overlapping calls from the launch check + periodic timer + manual
    // "Check now" click landing at once.
    if (!isTauri() || get().checking) return;
    set({ checking: true, error: null });
    try {
      const status = await checkUpdate();
      set({ behind: status.behind, summaries: status.summaries, version: status.version });
    } catch (err) {
      set({ error: readableError(err) });
    } finally {
      set({ checking: false });
    }
  },

  apply: async () => {
    // The backend itself rejects a concurrent second call, but the operator
    // should never have to see that error — this guard makes a second click
    // while one is in flight a no-op rather than a round trip that surfaces
    // "already in progress".
    if (!isTauri() || get().applying) return;
    set({ applying: true, error: null });
    try {
      await applyUpdate();
    } catch (err) {
      set({ error: readableError(err), applying: false });
      return;
    }
    set({ applying: false });
    // update_apply only returns the new SHA — re-check to refresh
    // version/behind/summaries (behind should land at 0) post-update.
    await get().check();
  },
}));

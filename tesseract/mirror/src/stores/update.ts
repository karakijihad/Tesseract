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
  // Which action last produced `error` — lets a consumer show only
  // apply-sourced failures somewhere as prominent as the HUD chip without
  // also surfacing routine background-check network blips there (those
  // stay Settings-only, same as before).
  errorSource: 'check' | 'apply' | null;
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

// update.rs's three dead-app failure branches (fast-forward failed and the
// respawn also failed; reinstall failed and the respawn also failed; the
// final spawn_supervisor after a successful apply failed) all end in this
// exact phrase — the one case where the user must act themselves rather
// than just retrying from the UI.
export function needsManualRestart(error: string): boolean {
  return error.includes('restart TESSERACT manually');
}

export const useUpdateStore = create<UpdateStoreState>((set, get) => ({
  version: null,
  behind: 0,
  summaries: [],
  checking: false,
  applying: false,
  error: null,
  errorSource: null,

  check: async () => {
    // Guards against a browser dev session (no Tauri IPC bridge) and against
    // overlapping calls from the launch check + periodic timer + manual
    // "Check now" click landing at once.
    if (!isTauri() || get().checking) return;
    set({ checking: true, error: null, errorSource: null });
    try {
      const status = await checkUpdate();
      set({ behind: status.behind, summaries: status.summaries, version: status.version });
    } catch (err) {
      set({ error: readableError(err), errorSource: 'check' });
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
    set({ applying: true, error: null, errorSource: null });
    try {
      await applyUpdate();
    } catch (err) {
      set({ error: readableError(err), errorSource: 'apply', applying: false });
      return;
    }
    try {
      // update_apply only returns the new SHA — re-check to refresh
      // version/behind/summaries (behind should land at 0) post-update.
      // `applying` stays true across this GitHub round-trip: clearing it
      // the instant applyUpdate() resolves would flash the HUD chip back
      // to an enabled "update · N" pill showing the OLD commit count for
      // the whole duration of the re-check — visually indistinguishable
      // from the update having silently undone itself.
      await get().check();
    } finally {
      set({ applying: false });
    }
  },
}));

import { create } from "zustand";

import {
  fetchCapabilities,
  postCapabilitiesDismiss,
  postCapabilitiesReverify,
  type CapabilityRole,
} from "../lib/api";

// cli-auth DESIGN.md §5 — backs the non-blocking first-run notice. `roles`
// and `noticeDismissed` come straight off the /api/capabilities report;
// self-suppression (no broken role -> never render, regardless of
// dismissal) is computed by the consumer via `selectBrokenRoles`, not
// stored here, so there is exactly one source of truth for "broken".
interface CapabilityNoticeState {
  roles: CapabilityRole[];
  noticeDismissed: boolean;
  loaded: boolean;
  verifying: boolean;
  error: string | null;
  fetch: () => Promise<void>;
  verify: () => Promise<void>;
  dismiss: () => Promise<void>;
}

function readableError(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

export const useCapabilityNoticeStore = create<CapabilityNoticeState>(
  (set, get) => ({
    roles: [],
    noticeDismissed: false,
    loaded: false,
    verifying: false,
    error: null,

    fetch: async () => {
      try {
        const report = await fetchCapabilities();
        set({
          roles: report.roles,
          noticeDismissed: report.notice_dismissed,
          loaded: true,
          error: null,
        });
      } catch (err) {
        set({ error: readableError(err) });
      }
    },

    verify: async () => {
      if (get().verifying) return;
      set({ verifying: true, error: null });
      try {
        const report = await postCapabilitiesReverify();
        set({ roles: report.roles, noticeDismissed: report.notice_dismissed });
      } catch (err) {
        set({ error: readableError(err) });
      } finally {
        set({ verifying: false });
      }
    },

    dismiss: async () => {
      try {
        const report = await postCapabilitiesDismiss();
        set({ roles: report.roles, noticeDismissed: report.notice_dismissed });
      } catch (err) {
        // The operator explicitly asked to dismiss — hide it client-side even
        // if the backend write failed. It may reappear next restart if the
        // marker didn't persist, but refusing to budge on a transient network
        // error is the worse failure mode for a "dismissible" card.
        set({ noticeDismissed: true, error: readableError(err) });
      }
    },
  }),
);

export function selectBrokenRoles(roles: CapabilityRole[]): CapabilityRole[] {
  return roles.filter((r) => r.broken);
}

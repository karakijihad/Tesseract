import { create } from 'zustand';

import { fetchSessionPolicy, type SessionResumePolicy } from '../lib/api';

interface SessionPolicyState {
  policy: SessionResumePolicy;
  days: number;
  show_config_reload_toasts: boolean;
  loaded: boolean;
  set: (next: {
    policy: SessionResumePolicy;
    days: number;
    show_config_reload_toasts: boolean;
  }) => void;
  fetch: () => Promise<void>;
}

export const useSessionPolicyStore = create<SessionPolicyState>((set) => ({
  // Phase 15 default — preserved when the backend is unreachable so
  // first-paint behaviour matches what shipped before Task C.
  policy: 'today_plus_yesterday',
  days: 1,
  show_config_reload_toasts: true,
  loaded: false,
  set: (next) =>
    set({
      policy: next.policy,
      days: next.days,
      show_config_reload_toasts: next.show_config_reload_toasts,
      loaded: true,
    }),
  fetch: async () => {
    try {
      const res = await fetchSessionPolicy();
      set({
        policy: res.policy,
        days: res.days,
        show_config_reload_toasts: res.show_config_reload_toasts,
        loaded: true,
      });
    } catch {
      // Leave defaults; SessionPolicySection will surface the error
      // when the operator opens the Settings panel.
    }
  },
}));

/** Phase 18 Task C — policy-driven cutoff used by `websocket.ts`
 * `_isWithinResumeCutoff`. Reads from the live store snapshot. */
export function isWithinResumeCutoff(startedAt: string | null | undefined): boolean {
  if (!startedAt) return false;
  const ts = Date.parse(startedAt);
  if (Number.isNaN(ts)) return false;
  const { policy, days } = useSessionPolicyStore.getState();
  if (policy === 'always') return true;
  const start = new Date();
  start.setHours(0, 0, 0, 0);
  if (policy === 'today_only') {
    return ts >= start.getTime();
  }
  if (policy === 'n_days') {
    start.setDate(start.getDate() - Math.max(0, days - 1));
    return ts >= start.getTime();
  }
  // today_plus_yesterday (default)
  start.setDate(start.getDate() - 1);
  return ts >= start.getTime();
}

/** Phase 18 audit m1 — describe the *current* resume cutoff in operator
 * terms so the auto-resume reject-toast in `websocket.ts` matches the
 * policy actually in effect (the old toast hardcoded "older than
 * yesterday" no matter the operator's choice). */
export function describeResumeCutoff(): string {
  const { policy, days } = useSessionPolicyStore.getState();
  if (policy === 'always') return 'within the resume cutoff';
  if (policy === 'today_only') return 'older than today';
  if (policy === 'n_days') {
    return days === 1 ? 'older than today' : `older than the last ${days} days`;
  }
  return 'older than yesterday';
}

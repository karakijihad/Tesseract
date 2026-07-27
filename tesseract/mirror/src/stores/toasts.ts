import { create } from 'zustand';

import { stopAlarmToneFor } from '../lib/alarmSound';

export type ToastKind = 'info' | 'warning' | 'error';

export interface ToastAlarmMeta {
  id: string;
  label: string;
  snoozeOptions: string[];
}

export interface ToastRestartMeta {
  // Operator-actionable restart prompt (e.g. code_drift_detected with
  // classification=restart_required). The toast renders the action
  // button and POSTs to the matching endpoint when clicked.
  headSha: string | null;
  pathCount: number;
}

export interface Toast {
  id: string;
  message: string;
  kind: ToastKind;
  alarm?: ToastAlarmMeta;
  restart?: ToastRestartMeta;
  sticky?: boolean;
}

interface ToastPushOptions {
  timeoutMs?: number;
  alarm?: ToastAlarmMeta;
  restart?: ToastRestartMeta;
  sticky?: boolean;
}

interface ToastStore {
  toasts: Toast[];
  push: (message: string, kind?: ToastKind, timeoutMs?: number) => void;
  pushWith: (message: string, kind: ToastKind, options: ToastPushOptions) => void;
  dismiss: (id: string) => void;
}

const DEFAULT_TIMEOUT_MS = 4000;

// Phase 4 follow-up — dedup window. When N background spawns complete
// in quick succession the `spawn_done` envelopes can produce N nearly-
// identical toasts (e.g. "delegate_codex done · …"). If a toast with
// the same message-prefix lands within this window, collapse it into
// the existing toast with a "(xN)" suffix instead of stacking.
const DEDUP_WINDOW_MS = 1500;

export const useToastStore = create<ToastStore>((set, get) => ({
  toasts: [],
  push: (message, kind = 'info', timeoutMs = DEFAULT_TIMEOUT_MS) => {
    const id = `toast-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    const now = Date.now();
    set((state) => {
      // Look at the most recent toast — if same prefix and within
      // window, coalesce instead of stacking. Prefix = up to first em
      // dash / hyphen which separates the spawn kind from the summary.
      const prefix = message.split(/\s[—-]\s/)[0];
      const head = state.toasts[state.toasts.length - 1];
      if (
        head &&
        head.kind === kind &&
        // Never coalesce into a sticky or alarm toast — they carry
        // semantics (ringing, manual-dismiss) that a "(xN)" suffix
        // would silently strip. Alarms and stickies stay distinct
        // even when a follow-up spawn_done prefix happens to match.
        !head.sticky &&
        !head.alarm &&
        (head.message === message || head.message.startsWith(prefix)) &&
        now - parseInt(head.id.split('-')[1], 10) < DEDUP_WINDOW_MS
      ) {
        const countMatch = head.message.match(/\s\(x(\d+)\)$/);
        const newCount = countMatch ? parseInt(countMatch[1], 10) + 1 : 2;
        const baseMsg = countMatch
          ? head.message.replace(/\s\(x\d+\)$/, '')
          : head.message;
        const coalesced: Toast = { ...head, message: `${baseMsg} (x${newCount})` };
        return { toasts: [...state.toasts.slice(0, -1), coalesced] };
      }
      return { toasts: [...state.toasts, { id, message, kind }] };
    });
    window.setTimeout(() => get().dismiss(id), timeoutMs);
  },
  pushWith: (message, kind, options) => {
    const id = `toast-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    const toast: Toast = {
      id,
      message,
      kind,
      alarm: options.alarm,
      restart: options.restart,
      sticky: options.sticky ?? Boolean(options.alarm ?? options.restart),
    };
    set((state) => ({ toasts: [...state.toasts, toast] }));
    if (!toast.sticky) {
      const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
      window.setTimeout(() => get().dismiss(id), timeoutMs);
    }
  },
  dismiss: (id) => set((state) => {
    // Removing an alarm toast must also silence its tone — guarantees no
    // orphan ringing if dismiss is called from any path other than the
    // AlarmToast's own Snooze/Dismiss buttons.
    const t = state.toasts.find((x) => x.id === id);
    if (t?.alarm) stopAlarmToneFor(t.alarm.id);
    return { toasts: state.toasts.filter((x) => x.id !== id) };
  }),
}));

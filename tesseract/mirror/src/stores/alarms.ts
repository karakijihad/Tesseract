import { create } from 'zustand';
import { cancelAlarm, createAlarm, fetchAlarms, snoozeAlarm } from '../lib/api';
import type { Alarm } from '../lib/types';

interface AlarmsState {
  alarms: Alarm[];
  loading: boolean;
  lastError: string | null;

  fetchAlarms: () => Promise<void>;
  cancel: (handle: string) => Promise<void>;
  snooze: (handle: string, duration: string) => Promise<void>;
  create: (payload: { label: string; when: string; message?: string }) => Promise<boolean>;
}

function _errMessage(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

export const useAlarmsStore = create<AlarmsState>((set, get) => ({
  alarms: [],
  loading: false,
  lastError: null,

  fetchAlarms: async () => {
    set({ loading: true, lastError: null });
    try {
      const res = await fetchAlarms();
      set({ alarms: res.alarms, loading: false });
    } catch (err) {
      set({ loading: false, lastError: _errMessage(err) });
    }
  },

  cancel: async (handle) => {
    set({ lastError: null });
    try {
      await cancelAlarm(handle);
      await get().fetchAlarms();
    } catch (err) {
      set({ lastError: _errMessage(err) });
    }
  },

  snooze: async (handle, duration) => {
    set({ lastError: null });
    try {
      await snoozeAlarm(handle, duration);
      await get().fetchAlarms();
    } catch (err) {
      set({ lastError: _errMessage(err) });
    }
  },

  create: async (payload) => {
    set({ lastError: null });
    try {
      await createAlarm(payload);
      await get().fetchAlarms();
      return true;
    } catch (err) {
      set({ lastError: _errMessage(err) });
      return false;
    }
  },
}));

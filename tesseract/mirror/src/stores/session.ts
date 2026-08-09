import { create } from 'zustand';
import { persist, createJSONStorage } from 'zustand/middleware';
import type {
  SessionMeta,
  SessionListData,
  SessionStatsData,
} from '../lib/types';
import { BACKEND_BASE } from '../lib/endpoints';

// Phase 1 — per-day grouping + archive lazy-fetch.
export interface DayGroup {
  date: string;            // 'YYYY-MM-DD' or the literal 'custom' bucket
  runs: SessionMeta[];
  run_count: number;
  total_turns: number;
}

export interface ArchivedSession extends SessionMeta {
  archived_in: string;     // 'YYYY-MM'
}

interface SessionStore {
  sessions: SessionMeta[];
  saveName: string | null;
  latestStats: SessionStatsData | null;
  days: DayGroup[];
  archive: ArchivedSession[];
  archiveLoaded: boolean;
  setSessionList: (data: SessionListData) => void;
  setSaveName: (name: string | null) => void;
  setLatestStats: (data: SessionStatsData) => void;
  fetchList: () => Promise<void>;
  fetchDays: () => Promise<void>;
  fetchArchive: () => Promise<void>;
}

// fetchList dedupe — the list is requested from three independent triggers
// (WS-connect auto-resume, SessionDrawer open/mutate, session_* envelope
// handlers) which can fire near-simultaneously and produce burst duplicates
// of GET /api/sessions. Concurrent callers share the in-flight promise; no
// TTL, so a mutation-triggered refresh after completion always refetches.
let _fetchListInflight: Promise<void> | null = null;

export const useSessionStore = create<SessionStore>()(
  persist(
    (set) => ({
      sessions: [],
      saveName: null,
      latestStats: null,
      days: [],
      archive: [],
      archiveLoaded: false,
      setSessionList: (data) => set({ sessions: data.sessions }),
      setSaveName: (name) => set({ saveName: name }),
      setLatestStats: (data) => set({ latestStats: data }),
      fetchList: async () => {
        if (_fetchListInflight) return _fetchListInflight;
        _fetchListInflight = (async () => {
          try {
            const res = await fetch(`${BACKEND_BASE}/api/sessions`);
            if (!res.ok) return;
            const body = (await res.json()) as SessionListData;
            set({ sessions: body.sessions });
          } catch (err) {
            console.warn('fetchList failed', err);
          } finally {
            _fetchListInflight = null;
          }
        })();
        return _fetchListInflight;
      },
      fetchDays: async () => {
        try {
          const res = await fetch(`${BACKEND_BASE}/api/sessions/days`);
          if (!res.ok) return;
          const body = (await res.json()) as { days: DayGroup[] };
          set({ days: body.days });
        } catch (err) {
          console.warn('fetchDays failed', err);
        }
      },
      fetchArchive: async () => {
        try {
          const res = await fetch(`${BACKEND_BASE}/api/sessions/archive`);
          if (!res.ok) return;
          const body = (await res.json()) as { sessions: ArchivedSession[] };
          set({ archive: body.sessions, archiveLoaded: true });
        } catch (err) {
          console.warn('fetchArchive failed', err);
        }
      },
    }),
    {
      name: 'tesseract-mirror-session',
      storage: createJSONStorage(() => localStorage),
      partialize: (state) => ({ saveName: state.saveName }),
    },
  ),
);

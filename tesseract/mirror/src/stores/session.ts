import { create } from 'zustand';
import { persist, createJSONStorage } from 'zustand/middleware';
import type {
  SessionMeta,
  SessionListData,
  SessionStatsData,
} from '../lib/types';
import { BACKEND_BASE } from '../lib/endpoints';

// Per-day grouping + archive lazy-fetch.
export interface DayGroup {
  // Always 'YYYY-MM-DD', from the conversation's `created_at`. The old
  // 'custom' bucket existed only to catch a filename that would not parse as
  // a date, and a uuid names no date to fall back from.
  date: string;
  runs: SessionMeta[];
  run_count: number;
  total_turns: number;
}

// Archiving is a flag on the record, not a folder it was moved into — so
// there is no month bucket to show and `ended_at` is what "when" means here.
export type ArchivedSession = SessionMeta;

interface SessionStore {
  sessions: SessionMeta[];
  // The conversation the operator was last in, by id. It was a filename
  // until 2026-08-18 — auto-resume matched it against a directory listing.
  lastChatId: string | null;
  latestStats: SessionStatsData | null;
  days: DayGroup[];
  archive: ArchivedSession[];
  archiveLoaded: boolean;
  setSessionList: (data: SessionListData) => void;
  setLastChatId: (chatId: string | null) => void;
  setLatestStats: (data: SessionStatsData) => void;
  fetchList: () => Promise<void>;
  fetchDays: () => Promise<void>;
  fetchArchive: () => Promise<void>;
}

// fetchList dedupe — the list is requested from three independent triggers
// (WS-connect auto-resume, SessionDrawer open/mutate, session_* envelope
// handlers) which can fire near-simultaneously and produce burst duplicates
// of GET /api/chats. Concurrent callers share the in-flight promise; no
// TTL, so a mutation-triggered refresh after completion always refetches.
let _fetchListInflight: Promise<void> | null = null;

export const useSessionStore = create<SessionStore>()(
  persist(
    (set) => ({
      sessions: [],
      lastChatId: null,
      latestStats: null,
      days: [],
      archive: [],
      archiveLoaded: false,
      setSessionList: (data) => set({ sessions: data.sessions }),
      setLastChatId: (chatId) => set({ lastChatId: chatId }),
      setLatestStats: (data) => set({ latestStats: data }),
      fetchList: async () => {
        if (_fetchListInflight) return _fetchListInflight;
        _fetchListInflight = (async () => {
          try {
            const res = await fetch(`${BACKEND_BASE}/api/chats`);
            if (!res.ok) return;
            const body = (await res.json()) as { chats: SessionMeta[] };
            set({ sessions: body.chats });
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
          const res = await fetch(`${BACKEND_BASE}/api/chats/days`);
          if (!res.ok) return;
          const body = (await res.json()) as { days: DayGroup[] };
          set({ days: body.days });
        } catch (err) {
          console.warn('fetchDays failed', err);
        }
      },
      fetchArchive: async () => {
        try {
          const res = await fetch(`${BACKEND_BASE}/api/chats?archived=only`);
          if (!res.ok) return;
          const body = (await res.json()) as { chats: ArchivedSession[] };
          set({ archive: body.chats, archiveLoaded: true });
        } catch (err) {
          console.warn('fetchArchive failed', err);
        }
      },
    }),
    {
      name: 'tesseract-mirror-session',
      storage: createJSONStorage(() => localStorage),
      partialize: (state) => ({ lastChatId: state.lastChatId }),
      // v0 persisted `saveName`, a filename that was also the identity. There
      // is no id to recover from one — the file it named is not the record —
      // so the pointer is dropped rather than guessed at, and the stale key
      // goes with it instead of sitting in localStorage forever.
      version: 1,
      migrate: (persisted) => {
        const prior = persisted as { saveName?: string | null } | null;
        if (prior && 'saveName' in prior) delete prior.saveName;
        return { lastChatId: null };
      },
    },
  ),
);

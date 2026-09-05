import { create } from 'zustand';
import { persist, createJSONStorage } from 'zustand/middleware';
import type {
  SessionMeta,
  SessionListData,
  SessionStatsData,
} from '../lib/types';
import { BACKEND_BASE } from '../lib/endpoints';

// Archiving is a flag on the record, not a folder it was moved into — so
// there is no month bucket to show and `ended_at` is what "when" means here.
export type ArchivedSession = SessionMeta;

interface SessionStore {
  sessions: SessionMeta[];
  // The conversation the operator was last in, by id. It was a filename
  // until 2026-08-18 — auto-resume matched it against a directory listing.
  lastChatId: string | null;
  latestStats: SessionStatsData | null;
  /** Which conversation `latestStats` describes. Stats are per chat, and a
   *  switch leaves the previous chat's numbers standing until the new one
   *  emits its own, so a reader has to check before trusting them. */
  latestStatsChatId: string | null;
  archive: ArchivedSession[];
  archiveLoaded: boolean;
  setSessionList: (data: SessionListData) => void;
  setLastChatId: (chatId: string | null) => void;
  setLatestStats: (data: SessionStatsData, chatId: string | null) => void;
  fetchList: () => Promise<void>;
  fetchArchive: () => Promise<void>;
}

// fetchList dedupe — the list is requested from three independent triggers
// (WS-connect auto-resume, the chat rail opening or mutating, session_*
// envelope handlers) which can fire near-simultaneously and produce burst
// duplicates of GET /api/chats. Concurrent callers share the in-flight; no
// TTL, so a mutation-triggered refresh after completion always refetches.
let _fetchListInflight: Promise<void> | null = null;

export const useSessionStore = create<SessionStore>()(
  persist(
    (set) => ({
      sessions: [],
      lastChatId: null,
      latestStats: null,
      latestStatsChatId: null,
      archive: [],
      archiveLoaded: false,
      setSessionList: (data) => set({ sessions: data.sessions }),
      setLastChatId: (chatId) => set({ lastChatId: chatId }),
      setLatestStats: (data, chatId) =>
        set({ latestStats: data, latestStatsChatId: chatId }),
      fetchList: async () => {
        if (_fetchListInflight) return _fetchListInflight;
        _fetchListInflight = (async () => {
          try {
            const res = await fetch(`${BACKEND_BASE}/api/chats`);
            if (!res.ok) return;
            const body = (await res.json()) as { chats: SessionMeta[] };
            // A body without `chats` is a list of none, not a crash in the
            // rail that renders straight from this.
            set({ sessions: body.chats ?? [] });
          } catch (err) {
            console.warn('fetchList failed', err);
          } finally {
            _fetchListInflight = null;
          }
        })();
        return _fetchListInflight;
      },
      fetchArchive: async () => {
        try {
          const res = await fetch(`${BACKEND_BASE}/api/chats?archived=only`);
          if (!res.ok) return;
          const body = (await res.json()) as { chats: ArchivedSession[] };
          set({ archive: body.chats ?? [], archiveLoaded: true });
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

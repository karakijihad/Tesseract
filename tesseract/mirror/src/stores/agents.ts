import { create } from 'zustand';
import { fetchAgents, fetchAgent, fetchPendingAgents } from '../lib/api';
import type { Agent, AgentDetail } from '../lib/types';

interface AgentsState {
  agents: Agent[];
  pending: Agent[];
  selectedName: string | null;
  detail: AgentDetail | null;
  detailLoading: boolean;
  loading: boolean;
  error: string | null;

  fetchAll: () => Promise<void>;
  selectAgent: (name: string | null) => Promise<void>;
}

export const useAgentsStore = create<AgentsState>((set) => ({
  agents: [],
  pending: [],
  selectedName: null,
  detail: null,
  detailLoading: false,
  loading: false,
  error: null,

  fetchAll: async () => {
    set({ loading: true, error: null });
    try {
      const [active, pending] = await Promise.all([fetchAgents(), fetchPendingAgents()]);
      set({ agents: active.agents, pending: pending.agents, loading: false });
    } catch (err) {
      set({ loading: false, error: err instanceof Error ? err.message : String(err) });
    }
  },

  selectAgent: async (name) => {
    if (name === null) {
      set({ selectedName: null, detail: null });
      return;
    }
    set({ selectedName: name, detailLoading: true, detail: null });
    try {
      const detail = await fetchAgent(name);
      set({ detail, detailLoading: false });
    } catch (err) {
      set({
        detailLoading: false,
        error: err instanceof Error ? err.message : String(err),
      });
    }
  },
}));

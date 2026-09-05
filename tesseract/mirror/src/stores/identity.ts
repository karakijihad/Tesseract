import { create } from 'zustand';
import { fetchIdentity as apiFetchIdentity } from '../lib/api';
import type {
  IdentityCompactThreshold,
  IdentityCostTracking,
  IdentityRoleModel,
  IdentityRoleStatus,
} from '../lib/types';

interface IdentityState {
  name: string;
  operatorName: string;
  version: string;
  securityMode: string;
  modelRole: string;
  modelName: string;
  provider: string;
  observerModel: string | null;
  observerProvider: string | null;
  models: Record<string, IdentityRoleModel> | null;
  roles: Record<string, IdentityRoleStatus> | null;
  compactThresholds: Record<string, IdentityCompactThreshold> | null;
  costTracking: IdentityCostTracking | null;
  latestMessage: string | null;
  setLatestMessage: (msg: string | null) => void;
  setSecurityMode: (mode: string) => void;
  setNames: (name: string, operatorName: string) => void;
  setModel: (provider: string, model: string) => void;
  setCompactThreshold: (role: string, threshold: IdentityCompactThreshold) => void;
  setCostTracking: (cost: IdentityCostTracking) => void;
  fetchIdentity: () => Promise<void>;
  reset: () => void;
}

const defaultState = {
  name: '',
  operatorName: '',
  version: '',
  securityMode: '',
  modelRole: '',
  modelName: '',
  provider: '',
  observerModel: null as string | null,
  observerProvider: null as string | null,
  models: null as Record<string, IdentityRoleModel> | null,
  roles: null as Record<string, IdentityRoleStatus> | null,
  compactThresholds: null as Record<string, IdentityCompactThreshold> | null,
  costTracking: null as IdentityCostTracking | null,
  latestMessage: null as string | null,
};

export const useIdentityStore = create<IdentityState>((set) => ({
  ...defaultState,
  setLatestMessage: (msg) => set({ latestMessage: msg }),
  setSecurityMode: (mode) => set({ securityMode: mode }),
  setNames: (name, operatorName) => set({ name, operatorName }),
  setModel: (provider, model) => set({ provider, modelName: model }),
  // Merged onto what is already there, not swapped for it. The
  // compact-threshold route answers with the four fields it just wrote, while
  // the entry also carries the model's own limits that only `/api/identity`
  // sends. Replacing the entry wholesale dropped those the instant a control
  // was saved. The measured floor is NOT here: it rides the `session_stats`
  // envelope into the session store, because a GET with no session and no
  // chat cannot say whose floor it is answering with.
  setCompactThreshold: (role, threshold) =>
    set((state) => ({
      compactThresholds: {
        ...(state.compactThresholds ?? {}),
        [role]: { ...(state.compactThresholds?.[role] ?? {}), ...threshold },
      },
    })),
  setCostTracking: (cost) => set({ costTracking: cost }),
  async fetchIdentity() {
    try {
      const ident = await apiFetchIdentity();
      set({
        name: ident.name,
        operatorName: ident.operator_name,
        version: ident.version,
        securityMode: ident.security_mode,
        modelRole: ident.model_role,
        modelName: ident.model_name,
        provider: ident.provider,
        observerModel: ident.observer_model,
        observerProvider: ident.observer_provider,
        models: ident.models ?? null,
        roles: ident.roles ?? null,
        compactThresholds: ident.compact_thresholds ?? null,
        costTracking: ident.cost_tracking ?? null,
      });
    } catch (err) {
      console.error('[identity] fetchIdentity failed:', err);
    }
  },
  reset: () => set({ ...defaultState }),
}));

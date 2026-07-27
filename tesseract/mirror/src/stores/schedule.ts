import { create } from 'zustand';
import { fetchSchedule } from '../lib/api';
import type {
  ScheduleJob,
  ScheduleJobDoneData,
  ScheduleJobStartedData,
  ScheduleStateData,
} from '../lib/types';

export interface RunFlash {
  run_id: string;
  ok?: boolean;
  detail?: string;
  started_at: number;
  finished_at?: number;
}

interface ScheduleState {
  jobs: ScheduleJob[];
  runFlashes: Record<string, RunFlash>;
  loading: boolean;
  lastError: string | null;

  fetchJobs: () => Promise<void>;
  setJobs: (jobs: ScheduleJob[]) => void;
  applyState: (data: ScheduleStateData) => void;
  markStarted: (data: ScheduleJobStartedData) => void;
  markDone: (data: ScheduleJobDoneData) => void;
}

function _patchRuntime(job: ScheduleJob, patch: Partial<ScheduleJob['runtime']> & { enabled?: boolean; cadence?: string; circuit_broken?: boolean; consecutive_failures?: number; model_role?: string | null; effective_model_role?: string | null; uses_llm?: boolean }): ScheduleJob {
  const next: ScheduleJob = { ...job };
  if (patch.enabled !== undefined) next.enabled = patch.enabled;
  if (patch.cadence !== undefined) next.cadence = patch.cadence;
  if (patch.model_role !== undefined) next.model_role = patch.model_role;
  if (next.runtime) {
    next.runtime = {
      ...next.runtime,
      ...(patch.enabled !== undefined ? { enabled: patch.enabled } : {}),
      ...(patch.cadence !== undefined ? { cadence: patch.cadence } : {}),
      ...(patch.circuit_broken !== undefined ? { circuit_broken: patch.circuit_broken } : {}),
      ...(patch.consecutive_failures !== undefined ? { consecutive_failures: patch.consecutive_failures } : {}),
      ...(patch.model_role !== undefined ? { model_role: patch.model_role } : {}),
      ...(patch.effective_model_role !== undefined ? { effective_model_role: patch.effective_model_role } : {}),
      ...(patch.uses_llm !== undefined ? { uses_llm: patch.uses_llm } : {}),
    };
  }
  return next;
}

export const useScheduleStore = create<ScheduleState>((set) => ({
  jobs: [],
  runFlashes: {},
  loading: false,
  lastError: null,

  fetchJobs: async () => {
    set({ loading: true, lastError: null });
    try {
      const res = await fetchSchedule();
      set({ jobs: res.jobs, loading: false });
    } catch (err) {
      set({ loading: false, lastError: err instanceof Error ? err.message : String(err) });
    }
  },

  setJobs: (jobs) => set({ jobs }),

  applyState: (data) => {
    const name = data.job_name;
    if (!name) return;
    set((state) => ({
      jobs: state.jobs.map((job) => job.name === name ? _patchRuntime(job, data) : job),
    }));
  },

  markStarted: (data) => {
    set((state) => ({
      runFlashes: {
        ...state.runFlashes,
        [data.job_name]: {
          run_id: data.run_id,
          started_at: Date.now(),
        },
      },
    }));
  },

  markDone: (data) => {
    set((state) => {
      const job = state.jobs.find((j) => j.name === data.job_name);
      const serverFiredAt = data.fired_at ?? new Date().toISOString();
      const lastResult = {
        ok: data.ok,
        detail: data.detail,
        duration_ms: data.duration_ms,
        payload: data.payload,
      };
      const updatedJobs = job
        ? state.jobs.map((j) => j.name === data.job_name
            ? {
                ...j,
                runtime: {
                  name: j.runtime?.name ?? j.name,
                  cadence: j.runtime?.cadence ?? j.cadence,
                  enabled: j.runtime?.enabled ?? j.enabled,
                  consecutive_failures: j.runtime?.consecutive_failures ?? 0,
                  ...j.runtime,
                  circuit_broken: data.circuit_broken,
                  last_fired_at: serverFiredAt,
                  last_result: lastResult,
                },
              }
            : j)
        : state.jobs;
      return {
        jobs: updatedJobs,
        runFlashes: {
          ...state.runFlashes,
          [data.job_name]: {
            run_id: data.run_id,
            ok: data.ok,
            detail: data.detail,
            started_at: state.runFlashes[data.job_name]?.started_at ?? Date.now(),
            finished_at: Date.now(),
          },
        },
      };
    });
  },
}));

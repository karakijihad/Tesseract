import { create } from 'zustand';

export type TaskStatus = 'pending' | 'in_progress' | 'completed';

export interface TaskItem {
  id: string;
  title: string;
  status: TaskStatus;
}

interface TasksState {
  // Phase 3 (CLI parity) — operator-visible todo checklist, Claude
  // Code TodoWrite analog. Replaced wholesale on every `tasks_state`
  // envelope (no diffing). Cleared on session reset / chat reset.
  items: TaskItem[];
  setItems: (items: TaskItem[]) => void;
  reset: () => void;
}

export const useTasksStore = create<TasksState>(set => ({
  items: [],
  setItems: (items) => set({ items }),
  reset: () => set({ items: [] }),
}));

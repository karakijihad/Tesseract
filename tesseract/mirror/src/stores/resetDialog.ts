import { create } from 'zustand';

interface ResetDialogState {
  open: boolean;
  openDialog: () => void;
  closeDialog: () => void;
}

export const useResetDialogStore = create<ResetDialogState>((set) => ({
  open: false,
  openDialog: () => set({ open: true }),
  closeDialog: () => set({ open: false }),
}));

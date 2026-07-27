// Tauri IPC wrappers for the self-update commands (task-12's update.rs).
// Not HTTP — these bypass the api.ts/timedFetch path entirely and talk
// straight to the Rust side via @tauri-apps/api's invoke().
import { invoke } from '@tauri-apps/api/core';

export interface UpdateStatus {
  behind: number;
  summaries: string[];
  version: string;
}

export const checkUpdate = (): Promise<UpdateStatus> => invoke<UpdateStatus>('update_check');

// Resolves to the new short SHA on success. Long-running (can take up to
// ~30s — graceful supervisor stop, fast-forward, optional dep reinstall,
// respawn). Rejects with a readable string, including "an update is
// already in progress" if a concurrent call is already in flight.
export const applyUpdate = (): Promise<string> => invoke<string>('update_apply');

export const appVersion = (): Promise<string> => invoke<string>('app_version');

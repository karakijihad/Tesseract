// Tauri IPC wrappers for the self-update commands in update.rs.
// Not HTTP — these bypass the api.ts/timedFetch path entirely and talk
// straight to the Rust side via @tauri-apps/api's invoke().
import { invoke } from "@tauri-apps/api/core";

// What actually differs between the checked-out branch and `origin/main`
// when a fast-forward would be refused — uncommitted changes and local-only
// commits. `dirty`/`ahead_summaries` are capped (see repo.rs); `dirty_total`/
// `ahead` carry the true counts.
export interface Divergence {
  dirty: string[];
  dirty_total: number;
  ahead: number;
  ahead_summaries: string[];
}

export interface UpdateStatus {
  behind: number;
  summaries: string[];
  version: string;
  divergence: Divergence | null;
}

export const checkUpdate = (): Promise<UpdateStatus> =>
  invoke<UpdateStatus>("update_check");

// Resolves to the new short SHA on success. Long-running (can take up to
// ~30s — graceful supervisor stop, fast-forward, optional dep reinstall,
// respawn). Rejects with a readable string, including "an update is
// already in progress" if a concurrent call is already in flight.
export const applyUpdate = (): Promise<string> =>
  invoke<string>("update_apply");

// Discards local divergence (uncommitted changes and local-only commits)
// and updates anyway. Only reachable from an explicit, operator-confirmed
// UI action — never called automatically.
export const forceApplyUpdate = (): Promise<string> =>
  invoke<string>("update_force_apply");

export const appVersion = (): Promise<string> => invoke<string>("app_version");

// Everything the always-rendered Settings About block needs, served by the
// Rust shell alone — works even when the Python backend is down (which is
// exactly when the operator most needs version + update controls).
export interface AppInfo {
  semver: string | null;
  sha: string;
  // Unix seconds of the installed HEAD commit — the de-facto release date.
  commit_epoch: number;
}

export const appInfo = (): Promise<AppInfo> => invoke<AppInfo>("app_info");

// Shell self-update (exe_update.rs): the UI + Rust shell ship inside the
// installer, so git updates can't deliver them. The shell checks the
// repo's GitHub Releases and, on an explicit operator click, downloads
// the verified installer and restarts into it.
export interface ExeUpdateStatus {
  available: boolean;
  version: string;
  notes: string;
}

export const exeUpdateCheck = (): Promise<ExeUpdateStatus> =>
  invoke<ExeUpdateStatus>("exe_update_check");

// Long-running (~30MB download); on success the app exits and reinstalls,
// so this promise usually never resolves in the surviving page.
export const exeUpdateApply = (): Promise<void> =>
  invoke<void>("exe_update_apply");

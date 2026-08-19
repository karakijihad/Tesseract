// F11, and nothing else.
//
// The main window is OS-decorated (`tauri.conf.json` sets `decorations: true`,
// and Tauri defaults `maximizable`/`minimizable` to true), so maximise and
// minimise already come from the titlebar and need no grant — a capability
// gates JS calls, not the chrome Windows draws. Fullscreen is the one the
// titlebar does not offer, which is why it is the only thing here.
//
// Kept out of `App.tsx` on the one-library rule: the app already has four
// keydown listeners in components that own a surface, and a window-level act
// belongs to no surface. A caller gets it by installing it once.
import { getCurrentWindow } from "@tauri-apps/api/window";

/** True in the packaged shell, false in a browser tab (`pnpm run dev`). */
function inShell(): boolean {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

export async function toggleFullscreen(): Promise<void> {
  if (!inShell()) return;
  const win = getCurrentWindow();
  // Read before writing rather than tracking a local flag: the operator can
  // leave fullscreen through the window manager (Windows does it on some
  // display changes), and a flag would then be inverted for the rest of the
  // session — the key would appear dead once and work the time after.
  await win.setFullscreen(!(await win.isFullscreen()));
}

/**
 * Bind F11. Returns the uninstaller, so a caller in a `useEffect` can hand it
 * straight back.
 *
 * `preventDefault` matters: the webview has its own F11 handling, and letting
 * both run puts the window and the page's idea of fullscreen out of step.
 */
export function installFullscreenKey(): () => void {
  const onKey = (e: KeyboardEvent) => {
    if (e.key !== "F11" || e.ctrlKey || e.altKey || e.metaKey) return;
    e.preventDefault();
    // Fire-and-forget: a refused toggle is a window that did not change, and
    // an unhandled rejection in a key handler is worse than that.
    void toggleFullscreen().catch(() => {});
  };
  window.addEventListener("keydown", onKey);
  return () => window.removeEventListener("keydown", onKey);
}

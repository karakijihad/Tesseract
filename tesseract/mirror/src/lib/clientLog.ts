import { BACKEND_BASE } from "./endpoints";

// Frontend error reporting (2026-07-30). The packaged webview's console
// is invisible — a UI crash left no trace on disk. Window errors and
// unhandled rejections are posted to the backend's /api/client-log,
// landing in mirror-backend.log. Fire-and-forget: if the backend is
// down there is nowhere to report to, and that failure is swallowed.

const MAX_REPORTS_PER_SESSION = 30;
let reported = 0;
let lastMessage = "";

export function reportClientError(message: string, source = ""): void {
  if (!message || reported >= MAX_REPORTS_PER_SESSION) return;
  // Consecutive-duplicate suppression: a render-loop error would
  // otherwise burn the whole budget on one message.
  if (message === lastMessage) return;
  lastMessage = message;
  reported += 1;
  void fetch(`${BACKEND_BASE}/api/client-log`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      level: "error",
      message: message.slice(0, 4000),
      source: source.slice(0, 200),
    }),
  }).catch(() => {
    /* backend unreachable — nothing to report to */
  });
}

export function installClientErrorReporter(): void {
  window.addEventListener("error", (e) => {
    reportClientError(
      String(e.message || e.error || "window error"),
      `${e.filename ?? ""}:${e.lineno ?? 0}`,
    );
  });
  window.addEventListener("unhandledrejection", (e) => {
    const r: unknown = e.reason;
    reportClientError(
      r instanceof Error ? `${r.name}: ${r.message}` : String(r),
      "unhandledrejection",
    );
  });
}

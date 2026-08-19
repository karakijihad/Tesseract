// The cockpit tells the shell when it is worth looking at.
//
// The shell keeps this window hidden behind the launch splash and reveals it
// on `cockpit_ready`. The alternative — the shell polling `/api/health`
// itself — would put the backend's address in a second place, in a language
// that cannot read `mirror.yaml`; this window already resolves it
// (`endpoints.ts`) because everything else it does goes through the same base.
//
// The wait is bounded twice over. Here, so a backend that never finishes
// preparing itself still hands the operator their app; and again in the shell,
// which reveals on its own timer if this module never calls at all.
import { invoke } from "@tauri-apps/api/core";

import { BACKEND_BASE, isTauri } from "./endpoints";

// Fast enough that the reveal follows the last substrate rather than trailing
// it visibly, and cheap: `/api/health` reads three values off the app dict.
const POLL_MS = 300;

// The backstop, not the expectation. A launch that loads a local speech model
// and a reranker measured 28-31s on the operator's machine, and a cold one on
// a slower disk is the case this must not cut short — firing early would hand
// over exactly the half-warm cockpit the splash exists to prevent. What it
// covers is a substrate that HANGS rather than fails (a wedged local daemon, a
// filesystem that stopped answering), where the honest outcome is a cockpit
// with some panels empty rather than no cockpit at all.
const GIVE_UP_MS = 90_000;

/**
 * Poll the backend until it reports itself warm, then ask the shell to reveal
 * this window. No-op outside the packaged shell — in a browser tab there is
 * no splash and nothing hidden.
 */
export function revealWhenWarm(now: () => number = Date.now): void {
  if (!isTauri()) return;

  const deadline = now() + GIVE_UP_MS;
  let done = false;
  const reveal = () => {
    if (done) return;
    done = true;
    // Fire-and-forget: the shell reveals on its own timer regardless, and an
    // unhandled rejection at launch is worse than a duplicate reveal (which
    // the command ignores).
    void invoke("cockpit_ready").catch(() => {});
  };

  const tick = async () => {
    try {
      const res = await fetch(`${BACKEND_BASE}/api/health`);
      if (res.ok) {
        const body = (await res.json()) as { warm?: boolean };
        if (body.warm) {
          reveal();
          return;
        }
      }
    } catch {
      // The listener is not up yet, or the request was refused mid-boot.
      // Both are the normal early-launch shape, not a reason to stop.
    }
    if (now() >= deadline) {
      reveal();
      return;
    }
    window.setTimeout(() => void tick(), POLL_MS);
  };

  void tick();
}

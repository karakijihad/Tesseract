import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import type { RefObject } from "react";
import { createPortal } from "react-dom";

import { useIdentityStore } from "../../stores/identity";
import { NOT_HEARD_BARE, useVoiceStore } from "../../stores/voice";

/** The wake gate's verdict, as a bubble over the mic it is about.
 *
 * It used to sit in the stage's top-left corner, which is nowhere near where
 * anyone looks while deciding whether to keep talking.
 *
 * **Portalled to `<body>`, for the same reason `HudMenu` is.** The HUD bar
 * clips its own overflow, so a bubble positioned inside it was sliced off at
 * the bar's top edge and only its tail showed. Measured against the mic and
 * placed in fixed coordinates instead, the way every other thing that has to
 * escape that bar is placed.
 *
 * `woken` is live mid-utterance ("keep going"), `notHeard` is held for a
 * couple of seconds after a refusal. Only one is ever set.
 *
 * A refusal shows whether or not the browser gave us a preview of the words.
 * Requiring one meant the whole marker was invisible in the Tauri webview,
 * which has no Web Speech API, so an utterance the gate threw away looked
 * exactly like a dead microphone.
 */

/** Clear of the mic, and clear of the window's edge. */
const GAP = 10;
const PAD = 8;

export function WakeToast({
  anchorRef,
}: {
  anchorRef: RefObject<HTMLElement | null>;
}) {
  const woken = useVoiceStore((s) => s.woken);
  const notHeard = useVoiceStore((s) => s.notHeard);
  const name = useIdentityStore((s) => s.name);

  const bubbleRef = useRef<HTMLDivElement>(null);
  const [coords, setCoords] = useState<{ top: number; left: number } | null>(
    null,
  );
  const shown = woken || Boolean(notHeard);

  const reposition = useCallback(() => {
    const anchor = anchorRef.current;
    const bubble = bubbleRef.current;
    if (!anchor || !bubble) return;
    const a = anchor.getBoundingClientRect();
    const b = bubble.getBoundingClientRect();
    // Centred on the mic, then held inside the window so a long preview
    // slides inward rather than off the edge.
    const half = b.width / 2;
    const left = Math.max(
      PAD + half,
      Math.min(a.left + a.width / 2, window.innerWidth - half - PAD),
    );
    setCoords({ top: a.top - b.height - GAP, left });
  }, [anchorRef]);

  useLayoutEffect(() => {
    if (shown) reposition();
    else setCoords(null);
  }, [shown, notHeard, reposition]);

  useEffect(() => {
    if (!shown) return;
    const onMove = () => reposition();
    window.addEventListener("resize", onMove);
    // Capture phase: the HUD sits above scrollable panes, and a bubble that
    // stayed put while the bar moved would point at nothing.
    window.addEventListener("scroll", onMove, true);
    return () => {
      window.removeEventListener("resize", onMove);
      window.removeEventListener("scroll", onMove, true);
    };
  }, [shown, reposition]);

  if (!shown) return null;
  const bare = notHeard === NOT_HEARD_BARE;
  return createPortal(
    <div
      ref={bubbleRef}
      className={`wake-toast${woken ? " is-woken" : " is-not-heard"}`}
      role="status"
      aria-live="polite"
      // Measured in the DOM for one frame before it is placed, so the first
      // paint is not a visible jump from the corner.
      style={{
        top: coords?.top ?? -9999,
        left: coords?.left ?? -9999,
        opacity: coords ? 1 : 0,
      }}
    >
      <span className="wake-toast__dot" aria-hidden="true" />
      <span className="wake-toast__mark">
        {woken ? "hearing you" : "not heard"}
      </span>
      {!woken && (
        <span className="wake-toast__text t-meta">
          {bare ? `start with "hey ${name || "assistant"}"` : notHeard}
        </span>
      )}
    </div>,
    document.body,
  );
}

import { useCallback, useEffect, useRef, useState, type RefObject } from "react";

// How far from the bottom still counts as "at the bottom". A click, a single
// wheel notch or a rounding error must not unstick the transcript; a real
// scroll up must.
const SCROLL_LOCK_THRESHOLD_PX = 40;

// The settle loop can never spin: at 60fps this is ~1.5s of re-assertion,
// and it stops as soon as the height stops moving.
const SETTLE_FRAME_BUDGET = 90;

interface StickToBottom {
  /** True once the operator has scrolled away from the bottom. */
  scrolledUp: boolean;
  /** Attach to the scrolling element's `onScroll`. */
  onScroll: () => void;
  /**
   * Re-stick and settle to the true bottom. Backs a "jump to latest" control,
   * and is also how a surface resets the lock when the transcript underneath
   * it changes — switching chat, or closing the panel.
   */
  stickToLatest: () => void;
}

/**
 * Follow new content while the operator is at the bottom, and stop the moment
 * they scroll up to read something.
 *
 * In a voice-first interface a transcript that does not follow is worse than
 * in a typed one: the reply is being spoken while the screen still shows the
 * previous turn, so the two channels disagree about where you are. Equally,
 * yanking the view while the operator is reading history is the louder bug —
 * hence the release, not just the follow.
 *
 * One shared answer on purpose: a second transcript that reimplements this
 * gets the release wrong, or the settle, and the two surfaces then disagree
 * about what "at the bottom" means.
 *
 * `deps` are the content signals that should re-assert the scroll — message
 * count, streaming text length, pending approvals, open state.
 */
export function useStickToBottom(
  ref: RefObject<HTMLElement | null>,
  deps: unknown[],
): StickToBottom {
  const [scrolledUp, setScrolledUp] = useState(false);
  // The scrollTop the settle loop last wrote. A `scroll` event is dispatched
  // asynchronously, so by the time `onScroll` reads the container the height
  // has often grown again and the position we just pinned measures as far from
  // the bottom — which is the operator's own gesture, as far as the listener
  // can tell. It is not, and it must never latch the lock. -1 = nothing pinned.
  const pinnedTop = useRef(-1);

  useEffect(() => {
    if (scrolledUp) return;
    let raf = 0;
    let frames = 0;
    let lastHeight = -1;
    // One assignment lands short whenever the content is still measuring:
    // virtualized rows measure as they scroll into view, markdown reflows as
    // it renders, images arrive late. Re-assert across frames until the
    // scroll height stabilises — instant while streaming, ~0.5s for a
    // resumed session.
    const settle = () => {
      const el = ref.current;
      if (!el) return;
      el.scrollTop = el.scrollHeight;
      // Read back rather than reuse the assignment: the browser clamps to
      // `scrollHeight - clientHeight`, and the guard compares against what the
      // element actually holds.
      pinnedTop.current = el.scrollTop;
      frames += 1;
      const height = el.scrollHeight;
      if (height !== lastHeight && frames < SETTLE_FRAME_BUDGET) {
        lastHeight = height;
        raf = requestAnimationFrame(settle);
      }
    };
    raf = requestAnimationFrame(settle);
    return () => cancelAnimationFrame(raf);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ref, scrolledUp, ...deps]);

  const onScroll = useCallback(() => {
    const el = ref.current;
    if (!el) return;
    const distanceFromBottom = el.scrollHeight - el.clientHeight - el.scrollTop;
    // Releasing is always honoured, whoever scrolled: at the bottom is at the
    // bottom. Only ENGAGING the lock is guarded, and only against the position
    // the settle loop itself wrote — anywhere else is the operator.
    if (distanceFromBottom <= SCROLL_LOCK_THRESHOLD_PX) {
      setScrolledUp(false);
      return;
    }
    if (el.scrollTop === pinnedTop.current) return;
    setScrolledUp(true);
  }, [ref]);

  // Clearing the lock re-runs the effect above, which settles to the true
  // bottom even through rows that have not been measured yet.
  const stickToLatest = useCallback(() => setScrolledUp(false), []);

  return { scrolledUp, onScroll, stickToLatest };
}

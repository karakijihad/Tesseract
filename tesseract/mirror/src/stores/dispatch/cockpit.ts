import { usePanelStore } from "../../cockpit/panelStore";
import {
  MAX_TYPE_SCALE,
  MIN_TYPE_SCALE,
  DEFAULT_TYPE_SCALE,
  useAppearanceStore,
} from "../appearance";
import { useUIStore, type View } from "../ui";

// The `cockpit_show` tool's landing site. Every branch writes the store the
// operator's own control writes, so the assistant and the person cannot end
// up holding different answers about what is open or how big the text is.
// Nothing here reaches for the DOM except the scroll case, where the pane's
// position is a browser fact rather than app state.

export interface CockpitShowData {
  action?: string;
  view?: string | null;
  section?: string | null;
  direction?: string | null;
  scale?: number | null;
}

/** One notch. Small enough to be worth asking twice, large enough to see. */
const ZOOM_STEP = 0.1;

const clamp = (n: number) => Math.min(MAX_TYPE_SCALE, Math.max(MIN_TYPE_SCALE, n));

/** The panel body RailView renders into. Named by its own class rather than
 *  found by walking the tree: the app owns this class, so a rename is a
 *  compile-adjacent change in one file, not a selector that rots quietly. */
const PANE = ".rail-view__body";
//: A panel that is on screen. `focus` is expressed as z-order rather than as a
//: stored id (`cockpit/panelStore.ts::focus`), so the panel in front is the
//: visible one with the highest z-index, and that is what "the open panel"
//: means to the person looking at it.
const VISIBLE_PANEL = ".glass-panel:not(.glass-panel--hidden)";

/** The body of the panel in front, or the only one there is.
 *
 *  `document.querySelector` alone returns the FIRST match in the document, and
 *  `PanelHost` renders panels in the order they were opened while focusing one
 *  again only changes its z. So with two panels open, scrolling moved whichever
 *  had been opened first and the tool reported it had scrolled the other. */
function openPane(): HTMLElement | null {
  const panels = Array.from(
    document.querySelectorAll<HTMLElement>(VISIBLE_PANEL),
  );
  if (panels.length > 1) {
    const front = panels.reduce((top, el) =>
      (Number(el.style.zIndex) || 0) > (Number(top.style.zIndex) || 0) ? el : top,
    );
    const body = front.querySelector<HTMLElement>(PANE);
    if (body) return body;
  }
  return document.querySelector<HTMLElement>(PANE);
}

function scrollPane(direction: string): void {
  const pane = openPane();
  if (!pane) return;
  if (direction === "top") {
    pane.scrollTo({ top: 0, behavior: "smooth" });
    return;
  }
  if (direction === "bottom") {
    pane.scrollTo({ top: pane.scrollHeight, behavior: "smooth" });
    return;
  }
  const page = pane.clientHeight * 0.8;
  pane.scrollBy({ top: direction === "up" ? -page : page, behavior: "smooth" });
}

export function handleCockpitShow(data: CockpitShowData | undefined): void {
  if (!data) return;

  if (data.action === "show" && data.view) {
    // `openPanel` and not `setView`: a tab summons the whole panel and drives
    // the view as part of doing it, so writing the view alone would move the
    // highlight on the bar and leave the stage empty. Found by opening
    // Settings from the tool and watching nothing appear.
    useUIStore
      .getState()
      .setRequestedSection(data.view as View, data.section ?? null);
    usePanelStore.getState().openPanel(data.view as View);
    return;
  }

  if (data.action === "zoom") {
    const { typeScale, setTypeScale } = useAppearanceStore.getState();
    if (typeof data.scale === "number") {
      setTypeScale(clamp(data.scale));
      return;
    }
    if (data.direction === "reset") {
      setTypeScale(DEFAULT_TYPE_SCALE);
      return;
    }
    const delta = data.direction === "out" ? -ZOOM_STEP : ZOOM_STEP;
    setTypeScale(clamp(typeScale + delta));
    return;
  }

  if (data.action === "scroll" && data.direction) {
    scrollPane(data.direction);
  }
}

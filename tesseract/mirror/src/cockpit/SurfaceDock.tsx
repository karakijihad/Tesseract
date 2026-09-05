// The dock — everything the operator put away, in a row above the HUD.
//
// Minimize takes a card off the canvas entirely, which is only usable if there
// is somewhere to see it. This is that somewhere.
//
// It holds both kinds of surface — the agent's canvas cards and the summoned
// view panels — because a minimized panel was invisible too, restorable only by
// finding its tab again. Nothing here closes anything: a chip brings it back.

import { useShallow } from "zustand/react/shallow";

import { useSurfacesStore } from "../stores/surfaces";
import { usePanelStore } from "./panelStore";
import { VIEW_LABELS } from "./viewLabels";
import { Chip } from "../components/common/Chip";
import { Hint } from "../components/ui/Hint";

interface DockEntry {
  key: string;
  label: string;
  restore: () => void;
  /** What to add to this entry's label if some other entry shares it. Absent
   *  on a view panel, whose name is the one that should stay clean. */
  mark?: string;
}

// Two cards can carry the same title: a lane card is titled after the task it
// was given, so running the same task twice makes two chips that read alike.
// The card's id is what actually differs, so a repeated title borrows its tail.
//
// Only CARDS are marked. A panel's name comes from a fixed list and is already
// the canonical one, so when a card happens to be titled 'Terminal' the card is
// what needs telling apart. Marking the panel too also read as a fault: its key
// is `panel:terminal`, whose tail is four letters of the word on the chip.
function disambiguate(entries: DockEntry[]): DockEntry[] {
  const seen = new Map<string, number>();
  for (const e of entries) seen.set(e.label, (seen.get(e.label) ?? 0) + 1);
  return entries.map((e) =>
    e.mark && (seen.get(e.label) ?? 0) > 1
      ? { ...e, label: `${e.label} ${e.mark}` }
      : e,
  );
}

interface SurfaceDockProps {
  /** The canvas view whose cards this dock carries. */
  view: string;
}

export function SurfaceDock({ view }: SurfaceDockProps) {
  const surfaces = useSurfacesStore((s) => s.byView[view]);
  const minimized = useSurfacesStore((s) => s.minimized);
  const toggleMinimizeSurface = useSurfacesStore((s) => s.toggleMinimize);
  // useShallow: a fresh array each call would retrigger the store subscription
  // every render (the hazard documented on ActivityMap).
  const minimizedPanels = usePanelStore(
    useShallow((s) =>
      s.panels.filter((p) => p.open && p.minimized).map((p) => p.id),
    ),
  );
  const toggleMinimizePanel = usePanelStore((s) => s.toggleMinimize);

  const entries: DockEntry[] = disambiguate([
    ...minimizedPanels.map((id) => ({
      key: `panel:${id}`,
      label: VIEW_LABELS[id],
      restore: () => toggleMinimizePanel(id),
    })),
    ...Object.values(surfaces ?? {})
      .filter((d) => minimized[d.id])
      .map((d) => ({
        key: `surface:${d.id}`,
        label: d.title ?? d.type,
        mark: d.id.slice(-4),
        restore: () => toggleMinimizeSurface(view, d.id),
      })),
  ]);

  if (entries.length === 0) return null;

  return (
    <div className="surface-dock" role="group" aria-label="Put away">
      {entries.map((e) => (
        <Hint
          key={e.key}
          label={`Bring back ${e.label}`}
          position="top"
          maxWidth={200}
        >
          <Chip
            className="surface-dock__chip"
            onClick={e.restore}
            ariaLabel={`Bring back ${e.label}`}
          >
            {e.label}
          </Chip>
        </Hint>
      ))}
    </div>
  );
}

import { useIdentityStore } from "../../../stores/identity";
import { useObserverStore, type ObserverState } from "../../../stores/observer";
import { Hint } from "../../ui/Hint";
import { HudSection } from "./HudSection";

const OBSERVER_ICON: Record<ObserverState, string> = {
  off: "○",
  armed: "◎",
  observing: "◉",
};

const OBSERVER_LABEL: Record<ObserverState, string> = {
  off: "Observer off — click to arm",
  armed: "Observer armed — click to disarm",
  observing: "Observer is observing — click to disarm",
};

// Hint direction: 'right' inside the folded vertical stack (a top hint would
// cover the stack item above — 2026-07-31 review finding), 'top' in the bar.
type HintPos = "top" | "right";

function ObserverModelBadge({ hintPos = "top" }: { hintPos?: HintPos }) {
  const model = useIdentityStore((s) => s.observerModel);
  const provider = useIdentityStore((s) => s.observerProvider);
  if (!model) return null;
  return (
    <Hint
      label={`Observer model — ${provider}/${model}`}
      position={hintPos}
      maxWidth={240}
    >
      <span className="hud-model" aria-label={`Observer model ${model}`}>
        {model}
      </span>
    </Hint>
  );
}

function ObserverToggle({ hintPos = "top" }: { hintPos?: HintPos }) {
  const state = useObserverStore((s) => s.state);
  const arm = useObserverStore((s) => s.arm);
  const disarm = useObserverStore((s) => s.disarm);

  const handleClick = () => {
    if (state === "off") arm();
    else disarm();
  };

  return (
    <Hint label={OBSERVER_LABEL[state]} position={hintPos} maxWidth={240}>
      <button
        type="button"
        className={`hud-observer hud-observer--${state}`}
        onClick={handleClick}
        aria-label={OBSERVER_LABEL[state]}
      >
        <span className="hud-observer-icon" aria-hidden="true">
          {OBSERVER_ICON[state]}
        </span>
        <span className="hud-observer-label">observer</span>
      </button>
    </Hint>
  );
}

interface Props {
  /** Sectioned dock (2026-07-31): folds the observer toggle + model badge
   *  into a ◎ section stack when the bar stops fitting. The face keeps the
   *  live tint so armed/observing stays readable at a glance. */
  folded?: boolean;
}

export function ObserverHudGroup({ folded = false }: Props) {
  const state = useObserverStore((s) => s.state);

  if (folded) {
    return (
      <div
        className="hud-group hud-group--observer"
        role="group"
        aria-label="Observer controls"
      >
        <HudSection
          id="observer"
          label="Observer"
          icon={<span aria-hidden="true">{OBSERVER_ICON[state]}</span>}
          live={state !== "off"}
        >
          <ObserverToggle hintPos="right" />
          <ObserverModelBadge hintPos="right" />
        </HudSection>
      </div>
    );
  }

  return (
    <div
      className="hud-group hud-group--observer"
      role="group"
      aria-label="Observer controls"
    >
      <ObserverToggle />
      <ObserverModelBadge />
    </div>
  );
}

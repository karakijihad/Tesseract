import { useIdentityStore } from '../../../stores/identity';
import { useObserverStore, type ObserverState } from '../../../stores/observer';
import { Hint } from '../../ui/Hint';

const OBSERVER_ICON: Record<ObserverState, string> = {
  off: '○',
  armed: '◎',
  observing: '◉',
};

const OBSERVER_LABEL: Record<ObserverState, string> = {
  off: 'Observer off — click to arm',
  armed: 'Observer armed — click to disarm',
  observing: 'Observer is observing — click to disarm',
};

function ObserverModelBadge() {
  const model = useIdentityStore((s) => s.observerModel);
  const provider = useIdentityStore((s) => s.observerProvider);
  if (!model) return null;
  return (
    <Hint label={`Observer model — ${provider}/${model}`} position="top" maxWidth={240}>
      <span className="hud-model" aria-label={`Observer model ${model}`}>
        {model}
      </span>
    </Hint>
  );
}

export function ObserverHudGroup() {
  const state = useObserverStore((s) => s.state);
  const arm = useObserverStore((s) => s.arm);
  const disarm = useObserverStore((s) => s.disarm);

  const handleClick = () => {
    if (state === 'off') arm();
    else disarm();
  };

  return (
    <div
      className="hud-group hud-group--observer"
      role="group"
      aria-label="Observer controls"
    >
      <Hint label={OBSERVER_LABEL[state]} position="top" maxWidth={240}>
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
      <ObserverModelBadge />
    </div>
  );
}

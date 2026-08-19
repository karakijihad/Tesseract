import { useEntityStore } from '../../../stores/entity';
import { useRoutingStore } from '../../../stores/routing';
import { Hint } from '../../ui/Hint';

export function StatusLine() {
  const state = useEntityStore(s => s.state);
  const role = useRoutingStore(s => s.role);
  const provider = useRoutingStore(s => s.provider);
  const model = useRoutingStore(s => s.model);
  const isLive = state !== 'idle';
  const hasRoute = role || provider || model;

  return (
    <div className="hud-status-line">
      <span className={`hud-sl-dot${isLive ? ' is-live' : ''}`} />
      <span className="hud-sl-text">{state}</span>
      {hasRoute && (
        <Hint label={`${role} · ${provider} · ${model}`}>
          <span className="hud-sl-route">
            <span className="hud-sl-route-sep">│</span>
            <span className="hud-sl-role">{role}</span>
            <span className="hud-sl-sep">·</span>
            <span className="hud-sl-provider">{provider}</span>
            <span className="hud-sl-sep">·</span>
            <span className="hud-sl-model">{model}</span>
          </span>
        </Hint>
      )}
    </div>
  );
}

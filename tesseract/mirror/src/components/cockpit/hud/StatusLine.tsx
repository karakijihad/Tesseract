import { useEntityStore } from '../../../stores/entity';
import { useRoutingStore } from '../../../stores/routing';

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
        <span className="hud-sl-route" title={`${role} · ${provider} · ${model}`}>
          <span className="hud-sl-route-sep">│</span>
          <span className="hud-sl-role">{role}</span>
          <span className="hud-sl-sep">·</span>
          <span className="hud-sl-provider">{provider}</span>
          <span className="hud-sl-sep">·</span>
          <span className="hud-sl-model">{model}</span>
        </span>
      )}
    </div>
  );
}

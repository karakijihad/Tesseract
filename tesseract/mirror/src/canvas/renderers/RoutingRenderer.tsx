// CV-1 — the trio's center "TARS routing" applet. Shows TARS connecting the
// two lanes: live busy/idle of each bound lane + the routing model (autonomy-
// kernel-driven per X-5). Drag-to-forward routing UI is deferred (phase §3).

import { useLanesStore } from '../../stores/lanes';
import type { RendererProps } from './index';

interface TrioLaneRef {
  name: string;
  lane_id: string;
  kind: string;
}

export function RoutingRenderer({ descriptor }: RendererProps) {
  const lanes = (descriptor.props?.lanes as TrioLaneRef[] | undefined) ?? [];
  const byLane = useLanesStore((s) => s.byLane);

  return (
    <div className="trio-routing">
      <div className="trio-routing__title">TARS</div>
      <div className="trio-routing__sub t-meta">connecting the trio</div>
      <div className="trio-routing__lanes">
        {lanes.map((l) => {
          const busy = byLane[l.lane_id]?.status?.busy ?? false;
          return (
            <div key={l.lane_id} className="trio-routing__lane">
              <span className={`lane-card__dot lane-card__dot--${l.kind}`} aria-hidden="true" />
              <span className="trio-routing__lane-name">{l.name}</span>
              <span className={`trio-routing__flow ${busy ? 'is-active' : ''}`}>
                {busy ? '⇄ working' : '· idle'}
              </span>
            </div>
          );
        })}
      </div>
      <div className="trio-routing__note t-meta">
        Routing is autonomy-kernel-driven; follow up in either lane directly.
      </div>
    </div>
  );
}

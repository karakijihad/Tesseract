import { useCallback, useState } from 'react';
import { FLOWS } from './flows';
import { FlowRail } from './FlowRail';

// Tabs rather than a stack: one diagram is mounted at a time, so opening a
// flow never moves the one you were reading. Same rule as the Monitor panel.

const STORAGE_KEY = 'panel.kernel.flow';

// The turn is the default because it is the one you watch: send a message and
// this is what moves. The other tabs are for going deeper on demand.
function storedTab(): string {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw && FLOWS.some((f) => f.id === raw)) return raw;
  } catch {
    // storage disabled — fall through to the default
  }
  return 'turn';
}

export function KernelFlows() {
  const [active, setActive] = useState<string>(storedTab);

  const select = useCallback((id: string) => {
    setActive(id);
    try {
      localStorage.setItem(STORAGE_KEY, id);
    } catch {
      // storage disabled — the tab still switches, it just won't persist
    }
  }, []);

  const flow = FLOWS.find((f) => f.id === active) ?? FLOWS[0];

  return (
    <div className="kernel-flows">
      <div className="monitor-tabs" role="tablist" aria-label="Kernel flows">
        {FLOWS.map((f) => (
          <button
            key={f.id}
            type="button"
            role="tab"
            aria-selected={active === f.id}
            className={`monitor-tab t-meta${active === f.id ? ' is-active' : ''}`}
            onClick={() => select(f.id)}
          >
            {f.label}
          </button>
        ))}
      </div>

      <div className="kernel-flow-body" role="tabpanel">
        <FlowRail flow={flow} />
        <p className="kernel-flow-source t-meta">{flow.source}</p>
      </div>
    </div>
  );
}

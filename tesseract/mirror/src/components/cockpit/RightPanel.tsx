import { useCallback, useState } from 'react';
import { Tabs } from '../common/Tabs';
import { BreakersSection } from './right/BreakersSection';
import { ObserverSection } from './right/ObserverSection';
import { SpendSection } from './right/SpendSection';
import { PulseTailSection } from './right/PulseTailSection';
import { useCostStore } from '../../stores/cost';
import { useHealthStore } from '../../stores/health';
import { useObservationsStore } from '../../stores/observations';
import { formatUsd } from '../../lib/money';

// Tabs, not stacked collapsibles. Opening one section used to move the others
// down the panel, so the thing you were reading walked away from the pointer.
// One body is mounted at a time and the panel never reflows; each tab carries
// its headline figure so switching is a choice, not a hunt.
const TABS = [
  { key: 'cost', label: 'Cost' },
  { key: 'breakers', label: 'Breakers' },
  { key: 'observer', label: 'Observer' },
] as const;

type TabKey = (typeof TABS)[number]['key'];

const STORAGE_KEY = 'panel.lifeline.tab';

function storedTab(): TabKey {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (TABS.some((t) => t.key === raw)) return raw as TabKey;
  } catch {
    // storage disabled — fall through to the default
  }
  return 'cost';
}

export function RightPanel() {
  const [active, setActive] = useState<TabKey>(storedTab);

  const spent = useCostStore((s) => s.globalState?.spent_usd ?? null);
  const openBreakers = useHealthStore(
    (s) => s.breakers.filter((b) => b.state === 'open').length,
  );
  const observations = useObservationsStore((s) => s.observations.length);

  const select = useCallback((key: TabKey) => {
    setActive(key);
    try {
      localStorage.setItem(STORAGE_KEY, key);
    } catch {
      // storage disabled — the tab still switches, it just won't persist
    }
  }, []);

  const badge = (key: TabKey): string | null => {
    if (key === 'cost') return spent === null ? null : formatUsd(spent);
    if (key === 'breakers') return openBreakers > 0 ? String(openBreakers) : null;
    return observations > 0 ? String(observations) : null;
  };

  return (
    <div className="right-panel-inner">
      <Tabs
        items={TABS.map((t) => ({ ...t, badge: badge(t.key) }))}
        active={active}
        onSelect={select}
        label="Monitor sections"
        fill
      />
      <div className="right-panel-body monitor-body" role="tabpanel">
        <div className="cat-section">
          {active === 'cost' && <SpendSection />}
          {active === 'breakers' && <BreakersSection />}
          {active === 'observer' && <ObserverSection />}
        </div>
      </div>
      <PulseTailSection />
    </div>
  );
}

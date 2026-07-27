import { useEffect, useMemo } from 'react';
import { useHealthStore, type Breaker } from '../../../stores/health';
import { usePanelCollapse } from '../../../lib/usePanelCollapse';

const REFRESH_MS = 60_000;

function dotClass(state: Breaker['state']): string {
  return state === 'open' ? 'is-bad' : 'is-ok';
}

export function BreakersSection() {
  const breakers = useHealthStore(s => s.breakers);
  const fetchBreakers = useHealthStore(s => s.fetchBreakers);
  const [collapsed, toggle] = usePanelCollapse('panel.breakers.collapsed', true);

  useEffect(() => {
    fetchBreakers();
    const timer = window.setInterval(fetchBreakers, REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [fetchBreakers]);

  const { active, history } = useMemo(() => {
    const a: Breaker[] = [];
    const h: Breaker[] = [];
    for (const b of breakers) (b.state === 'open' ? a : h).push(b);
    return { active: a, history: h };
  }, [breakers]);

  return (
    <section className="right-section">
      <div className="right-section-header">
        <button
          type="button"
          className="right-section-toggle t-meta"
          onClick={toggle}
          aria-expanded={!collapsed}
        >
          <span className="right-section-chevron">{collapsed ? '▸' : '▾'}</span>
          Breakers
        </button>
        <span className="t-caption right-section-count">{active.length}</span>
      </div>
      {!collapsed && (
        active.length === 0 && history.length === 0 ? (
          <div className="t-caption right-section-empty">all closed</div>
        ) : (
          <>
            {active.length > 0 && (
              <ul className="right-section-list">
                {active.map((b) => (
                  <li key={b.name} className="breaker-row">
                    <span className={`breaker-dot ${dotClass(b.state)}`} aria-hidden="true" />
                    <span className="t-body">{b.name}</span>
                    <span className="t-meta">{b.state}</span>
                  </li>
                ))}
              </ul>
            )}
            {history.length > 0 && (
              <>
                <div className="t-caption right-section-empty">history</div>
                <ul className="right-section-list">
                  {history.map((b) => (
                    <li key={b.name} className="breaker-row">
                      <span className={`breaker-dot ${dotClass(b.state)}`} aria-hidden="true" />
                      <span className="t-body">{b.name}</span>
                      <span className="t-meta">{b.lastReset ? `reset ${b.lastReset.slice(0, 10)}` : 'closed'}</span>
                    </li>
                  ))}
                </ul>
              </>
            )}
          </>
        )
      )}
    </section>
  );
}

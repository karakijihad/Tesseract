import { useState } from 'react';
import { useAgentsStore } from '../../../stores/agents';
import type { AgentStatus } from '../../../lib/types';

const FILTERS: { id: AgentStatus | 'all'; label: string }[] = [
  { id: 'all', label: 'All' },
  { id: 'active', label: 'Active' },
  { id: 'pending', label: 'Pending' },
];

export function AgentsList() {
  const agents = useAgentsStore((s) => s.agents);
  const pending = useAgentsStore((s) => s.pending);
  const selectedName = useAgentsStore((s) => s.selectedName);
  const select = useAgentsStore((s) => s.selectAgent);
  const [filter, setFilter] = useState<AgentStatus | 'all'>('all');

  const list =
    filter === 'all'
      ? [...agents, ...pending]
      : filter === 'active'
        ? agents
        : pending;

  return (
    <div className="agents-list">
      <div className="agents-list-filters" role="tablist" aria-label="Agent status filter">
        {FILTERS.map((f) => (
          <button
            key={f.id}
            type="button"
            role="tab"
            aria-selected={filter === f.id}
            className={`agents-list-filter${filter === f.id ? ' is-active' : ''}`}
            onClick={() => setFilter(f.id)}
          >
            {f.label}
          </button>
        ))}
      </div>
      {list.length === 0 ? (
        <div className="agents-list-empty t-meta">No agents in this view.</div>
      ) : (
        <ul className="agents-list-items">
          {list.map((a) => (
            <li key={`${a.status}:${a.name}`}>
              <button
                type="button"
                className={`agents-list-row${a.name === selectedName ? ' is-selected' : ''}`}
                onClick={() => select(a.name)}
              >
                <span className="agents-list-name">{a.name}</span>
                <span className={`agent-status-badge is-${a.status}`}>{a.status}</span>
                {a.description && (
                  <span className="agents-list-desc t-meta">{a.description}</span>
                )}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

import { useEffect } from 'react';
import { useAgentsStore } from '../stores/agents';
import { AgentsList } from './agents/components/AgentsList';
import { AgentDetail } from './agents/components/AgentDetail';

export function AgentsView() {
  const fetchAll = useAgentsStore((s) => s.fetchAll);
  const agents = useAgentsStore((s) => s.agents);
  const pending = useAgentsStore((s) => s.pending);
  const error = useAgentsStore((s) => s.error);

  useEffect(() => {
    fetchAll();
  }, [fetchAll]);

  return (
    <div className="agents-view">
      <header className="agents-view-head">
        <span className="agents-view-title">Agents</span>
        <span className="agents-view-count t-meta">
          {agents.length} active · {pending.length} pending
        </span>
        <div className="agents-view-actions">
          <button
            type="button"
            className="agents-view-btn"
            onClick={fetchAll}
          >
            refresh
          </button>
        </div>
      </header>

      {error && <div className="agents-view-error">{error}</div>}

      <div className="agents-view-grid">
        <div className="agents-view-list">
          <AgentsList />
        </div>
        <div className="agents-view-detail">
          <AgentDetail />
        </div>
      </div>
    </div>
  );
}

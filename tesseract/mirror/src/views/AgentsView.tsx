import { useEffect } from 'react';
import { Button } from '../components/common/Button';
import { Note } from '../components/common/Note';
import { RailView, type RailGroup } from '../components/common/RailView';
import { useAgentsStore } from '../stores/agents';
import type { Agent } from '../lib/types';
import { AgentDetail } from './agents/components/AgentDetail';

/** The agent list was a hand-rolled rail with its own filter chips; it is the
 *  shared rail now, grouped by status — which is what the filter was for, and
 *  searchable, which is what 24 rows actually need. */
export function AgentsView() {
  const fetchAll = useAgentsStore((s) => s.fetchAll);
  const agents = useAgentsStore((s) => s.agents);
  const pending = useAgentsStore((s) => s.pending);
  const selectedName = useAgentsStore((s) => s.selectedName);
  const select = useAgentsStore((s) => s.selectAgent);
  const error = useAgentsStore((s) => s.error);

  useEffect(() => {
    fetchAll();
  }, [fetchAll]);

  // The head belongs to the agent the rail has open. It read
  // "agent-self · 24 active · 0 pending" before, which says the fleet's
  // numbers under one agent's name — the fleet count is the rail's job, and
  // the rail already shows it as two groups.
  const section = (a: Agent) => ({
    key: a.name,
    label: a.name,
    // Short enough to sit on one line beside the name: what it is, which
    // version, and where it routes. The description is prose and stays in the
    // pane, where it has room to be read.
    meta: [a.status, a.version ? `v${a.version}` : null, a.model_role]
      .filter(Boolean)
      .join(' · '),
    actions: (
      <Button onClick={fetchAll} ariaLabel="refresh agent list">
        refresh
      </Button>
    ),
    render: () => <AgentDetail />,
  });

  const groups: RailGroup[] = [
    { label: `Active · ${agents.length}`, sections: agents.map(section) },
    { label: `Pending · ${pending.length}`, sections: pending.map(section) },
  ].filter((g) => g.sections.length > 0);

  if (groups.length === 0) {
    return (
      <div className="rail-view__empty">
        {error ? <Note tone="bad">{error}</Note> : <Note>No agents registered.</Note>}
      </div>
    );
  }

  return (
    <RailView
      groups={groups}
      label="Agents"
      searchable
      initial={selectedName ?? undefined}
      onSectionChange={select}
    />
  );
}

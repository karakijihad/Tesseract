import { useState } from 'react';
import { InboxPanel } from './workspace/InboxPanel';
import { DailyBriefTab } from './workspace/DailyBriefTab';
import './workspace/WorkspaceView.css';

type WorkspaceTab = 'inbox' | 'brief';

export function WorkspaceView() {
  const [tab, setTab] = useState<WorkspaceTab>('inbox');

  return (
    <div className="workspace-shell">
      <nav className="workspace-tabs" role="tablist" aria-label="Workspace sections">
        <button
          type="button"
          role="tab"
          aria-selected={tab === 'inbox'}
          className={`workspace-tab${tab === 'inbox' ? ' is-active' : ''}`}
          onClick={() => setTab('inbox')}
          data-testid="workspace-tab-inbox"
        >
          Inbox
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={tab === 'brief'}
          className={`workspace-tab${tab === 'brief' ? ' is-active' : ''}`}
          onClick={() => setTab('brief')}
          data-testid="workspace-tab-brief"
        >
          Daily Brief
        </button>
      </nav>
      <div className="workspace-tab-body">
        {tab === 'inbox' && <InboxPanel />}
        {tab === 'brief' && <DailyBriefTab />}
      </div>
    </div>
  );
}

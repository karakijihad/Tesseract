/* Workspace → Daily Brief tab. Picks the latest `daily_brief` workspace
 * event and renders the newsletter body without the event-detail chrome
 * (no title strip, no Resolve/Delete buttons). When the inbox already
 * resolved the brief, the history list still surfaces it so the tab
 * always has *something* to show. */
import { useEffect, useMemo } from 'react';
import { useWorkspaceStore, type WorkspaceEvent } from '../../stores/workspace';
import { DailyBriefBody } from './DailyBriefBody';

function pickLatestBrief(events: WorkspaceEvent[], history: WorkspaceEvent[]): WorkspaceEvent | null {
  const all = [...events, ...history].filter((e) => e.kind === 'daily_brief');
  if (all.length === 0) return null;
  return all.reduce((best, cur) => (cur.ts > best.ts ? cur : best), all[0]);
}

export function DailyBriefTab() {
  const events = useWorkspaceStore((s) => s.events);
  const history = useWorkspaceStore((s) => s.history);
  const fetchInbox = useWorkspaceStore((s) => s.fetchInbox);
  const fetchHistory = useWorkspaceStore((s) => s.fetchHistory);
  const loading = useWorkspaceStore((s) => s.loading);

  useEffect(() => {
    void fetchInbox();
    void fetchHistory();
  }, [fetchInbox, fetchHistory]);

  const latest = useMemo(() => pickLatestBrief(events, history), [events, history]);

  if (!latest) {
    return (
      <div className="brief-tab brief-tab--empty">
        <div className="brief-tab-empty-msg t-meta">
          {loading ? 'Loading briefs…' : 'No daily brief yet. The morning cron will land one at 08:00.'}
        </div>
      </div>
    );
  }

  return (
    <div className="brief-tab">
      <header className="brief-tab-head">
        <h2 className="brief-tab-title">{latest.title}</h2>
        <span className="t-meta brief-tab-date">{new Date(latest.ts).toLocaleString([], {
          year: 'numeric', month: 'short', day: 'numeric',
        })}</span>
      </header>
      <DailyBriefBody payload={latest.payload} />
    </div>
  );
}

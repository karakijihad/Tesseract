// HUD runs-surface fix (change-set A/B) — repoints the former
// ActivityPill SSR coverage at the permanent TopStatusHud activity segment.
// Unlike the pill (rendered `null` when idle), the segment is always
// present, so both the "0 running" and "N running" states assert on the
// same static markup rather than presence/absence of the whole component.

import { describe, it, expect, beforeEach } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { TopStatusHud } from './TopStatusHud';
import { useActivityStore, type ActivityRecord } from '../../stores/activity';

function rec(id: string, state = 'running'): ActivityRecord {
  return {
    activity_id: id, kind: 'lane', label: id, state, durability: 'persistent',
    provider: 'claude', parent_turn_id: null, parent_session_id: null,
    transcript_ref: null, started_at: '', updated_at: '',
  };
}

describe('TopStatusHud activity segment', () => {
  beforeEach(() => useActivityStore.setState({ byId: {} }));

  it('renders the permanent segment showing 0 running when idle', () => {
    // SSR render: the hydrate effect does not run, so no fetch fires.
    expect(renderToStaticMarkup(<TopStatusHud />)).toContain('0 running');
  });

  it('shows the running count when work is running', () => {
    useActivityStore.setState({ byId: { 'lane:1': rec('lane:1'), 'lane:2': rec('lane:2') } });
    expect(renderToStaticMarkup(<TopStatusHud />)).toContain('2 running');
  });

  it('does not render the activity map until the segment is toggled', () => {
    useActivityStore.setState({ byId: { 'lane:1': rec('lane:1') } });
    expect(renderToStaticMarkup(<TopStatusHud />)).not.toContain('activity-map');
  });
});

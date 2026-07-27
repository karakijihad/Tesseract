import { describe, it, expect, beforeEach } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { ActivityMap } from './ActivityMap';
import { useActivityStore, type ActivityRecord } from '../stores/activity';

function rec(id: string, kind: string): ActivityRecord {
  return {
    activity_id: id, kind, label: id, state: 'running', durability: 'ephemeral',
    provider: null, parent_turn_id: null, parent_session_id: null,
    transcript_ref: null, started_at: '', updated_at: '',
  };
}

describe('ActivityMap groups', () => {
  beforeEach(() => useActivityStore.setState({ byId: {} }));

  it('renders the routine and autonomy groups', () => {
    useActivityStore.setState({ byId: {
      'routine:1': rec('routine:1', 'routine'),
      'autonomy:a1': rec('autonomy:a1', 'autonomy'),
    }});
    const html = renderToStaticMarkup(<ActivityMap onClose={() => undefined} />);
    expect(html).toContain('Routines');
    expect(html).toContain('Autonomy');
  });
});

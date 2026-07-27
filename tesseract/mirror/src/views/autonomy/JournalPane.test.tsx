// TC-1 — JournalPane render tests.

import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { JournalPane } from './JournalPane';
import type { OperatorJournalRow } from '../../lib/api';

function _row(overrides: Partial<OperatorJournalRow>): OperatorJournalRow {
  return {
    ts: '2026-05-23T12:00:00+00:00',
    event_type: 'approval',
    agenda_item_id: null,
    worker_id: null,
    summary: null,
    artifacts: null,
    follow_up_draft_id: null,
    ...overrides,
  };
}

describe('JournalPane', () => {
  it('renders empty-state when rows is empty', () => {
    const html = renderToStaticMarkup(
      <JournalPane rows={[]} status="ready" error={null} />,
    );
    expect(html).toContain('No journal entries yet');
  });

  it('renders error message when status=error', () => {
    const html = renderToStaticMarkup(
      <JournalPane rows={[]} status="error" error="boom" />,
    );
    expect(html).toContain('Failed to load: boom');
  });

  it('renders rows in the order passed in (caller is reverse-chrono)', () => {
    const rows: OperatorJournalRow[] = [
      _row({
        ts: '2026-05-23T15:00:00+00:00',
        event_type: 'outcome',
        agenda_item_id: 'ag-3',
        worker_id: 'wk-3',
        summary: 'newest',
      }),
      _row({
        ts: '2026-05-23T14:00:00+00:00',
        event_type: 'dispatch',
        agenda_item_id: 'ag-2',
        worker_id: 'wk-2',
        summary: 'middle',
      }),
      _row({
        ts: '2026-05-23T13:00:00+00:00',
        event_type: 'approval',
        agenda_item_id: 'ag-1',
        summary: 'oldest',
      }),
    ];
    const html = renderToStaticMarkup(
      <JournalPane rows={rows} status="ready" error={null} />,
    );
    const newestIdx = html.indexOf('newest');
    const middleIdx = html.indexOf('middle');
    const oldestIdx = html.indexOf('oldest');
    expect(newestIdx).toBeGreaterThan(-1);
    expect(middleIdx).toBeGreaterThan(newestIdx);
    expect(oldestIdx).toBeGreaterThan(middleIdx);
  });

  it('labels each event_type with the operator-readable label', () => {
    const rows: OperatorJournalRow[] = [
      _row({ event_type: 'approval', agenda_item_id: 'ag-1' }),
      _row({ event_type: 'dispatch', worker_id: 'wk-1' }),
      _row({ event_type: 'outcome', worker_id: 'wk-1' }),
      _row({ event_type: 'advice_only', summary: 'tip' }),
      _row({ event_type: 'follow_up_draft', agenda_item_id: 'ag-2' }),
    ];
    const html = renderToStaticMarkup(
      <JournalPane rows={rows} status="ready" error={null} />,
    );
    expect(html).toContain('approved');
    expect(html).toContain('dispatched');
    expect(html).toContain('outcome');
    expect(html).toContain('advice only');
    expect(html).toContain('follow-up');
  });

  it('renders advice_only row with the distinctive class for visual emphasis', () => {
    const html = renderToStaticMarkup(
      <JournalPane
        rows={[
          _row({
            event_type: 'advice_only',
            agenda_item_id: 'ag-1',
            worker_id: 'wk-1',
            summary: 'just advice',
          }),
        ]}
        status="ready"
        error={null}
      />,
    );
    expect(html).toContain('autonomy-row--journal-advice_only');
    expect(html).toContain('just advice');
  });
});

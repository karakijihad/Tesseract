/* MO-9-11 Channels tab — left rail (channels) + right pane (Status/Logs).
 * MO-9-12 — adds Users (allowlist/pending + approval modal) and Conversations
 *           (per-user transcript) sub-panes. The rail still talks to the
 *           channel registry via GET /api/channels; mutating endpoints live
 *           inside the sub-panes that own them. */
import { useEffect, useState } from 'react';
import { Button } from '../../components/common/Button';
import { Tabs, type TabItem } from '../../components/common/Tabs';
import { Note } from '../../components/common/Note';
import { RailView, type RailGroup } from '../../components/common/RailView';
import { useChannelsStore, selectChannelByName } from '../../stores/channels';
import { StatusPane } from './StatusPane';
import { LogsPane } from './LogsPane';
import { UsersPane } from './UsersPane';
import { ConversationsPane } from './ConversationsPane';
import './channels.css';

type SubPane = 'status' | 'logs' | 'users' | 'conversations';

const PANE_TABS: TabItem<SubPane>[] = [
  { key: 'status', label: 'Status', testId: 'channels-pane-status' },
  { key: 'users', label: 'Users', testId: 'channels-pane-users' },
  {
    key: 'conversations',
    label: 'Conversations',
    testId: 'channels-pane-conversations',
  },
  { key: 'logs', label: 'Logs', testId: 'channels-pane-logs' },
];

export function ChannelsTab() {
  const channels = useChannelsStore((s) => s.channels);
  const selectedName = useChannelsStore((s) => s.selectedChannel);
  const loading = useChannelsStore((s) => s.loading);
  const fetchChannels = useChannelsStore((s) => s.fetchChannels);
  const selectChannel = useChannelsStore((s) => s.selectChannel);
  const selected = useChannelsStore((s) => selectChannelByName(s, selectedName));
  const [subPane, setSubPane] = useState<SubPane>('status');

  // One-shot load on mount so the rail is correct the first time the
  // operator opens the tab. The Refresh button drives subsequent
  // round-trips; we deliberately avoid a poll loop because the bridge
  // status is best-effort and a manual refresh is enough for now.
  useEffect(() => {
    void fetchChannels();
  }, [fetchChannels]);

  const groups: RailGroup[] = [
    {
      label: 'Registered',
      sections: channels.map((c) => ({
        key: c.name,
        label: c.name,
        render: () => (
          <>
            <Tabs
              items={PANE_TABS}
              active={subPane}
              onSelect={setSubPane}
              label="Channel panes"
            />
            {selected && subPane === 'status' && <StatusPane channel={selected} />}
            {selected && subPane === 'users' && <UsersPane channel={selected.name} />}
            {selected && subPane === 'conversations' && (
              <ConversationsPane channel={selected.name} />
            )}
            {selected && subPane === 'logs' && <LogsPane channel={selected.name} />}
          </>
        ),
      })),
    },
  ];

  if (channels.length === 0) {
    return (
      <div className="rail-view__empty" data-testid="channels-view">
        <Note>{loading ? 'Loading…' : 'No channels registered.'}</Note>
      </div>
    );
  }

  return (
    <RailView
      groups={groups}
      label="Registered channels"
      initial={selectedName ?? undefined}
      onSectionChange={selectChannel}
      meta={`${channels.length} channel${channels.length === 1 ? '' : 's'} registered`}
      actions={
        <Button
          onClick={() => fetchChannels()}
          disabled={loading}
          testId="channels-refresh-btn"
          ariaLabel="refresh channel list"
        >
          {loading ? 'refreshing…' : 'refresh'}
        </Button>
      }
    />
  );
}

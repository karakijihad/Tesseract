/* MO-9-11 Channels tab — left rail (channels) + right pane (Status/Logs).
 * MO-9-12 — adds Users (allowlist/pending + approval modal) and Conversations
 *           (per-user transcript) sub-panes. The rail still talks to the
 *           channel registry via GET /api/channels; mutating endpoints live
 *           inside the sub-panes that own them. */
import { useEffect, useState } from 'react';
import { useChannelsStore, selectChannelByName } from '../../stores/channels';
import { StatusPane } from './StatusPane';
import { LogsPane } from './LogsPane';
import { UsersPane } from './UsersPane';
import { ConversationsPane } from './ConversationsPane';
import './channels.css';

type SubPane = 'status' | 'logs' | 'users' | 'conversations';

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

  return (
    <div className="channels-view" data-testid="channels-view">
      <header className="channels-view-head">
        <span className="channels-view-title">Channels</span>
        <span className="channels-view-count t-meta">
          {channels.length} channel{channels.length === 1 ? '' : 's'} registered
        </span>
        <button
          type="button"
          className="channels-view-btn"
          onClick={() => fetchChannels()}
          disabled={loading}
          data-testid="channels-refresh-btn"
          aria-label="refresh channel list"
        >
          {loading ? 'refreshing…' : 'refresh'}
        </button>
      </header>

      <div className="channels-body">
        <nav className="channels-rail" aria-label="registered channels">
          {channels.length === 0 ? (
            <div className="channels-empty">
              {loading ? 'loading…' : 'No channels registered.'}
            </div>
          ) : (
            channels.map((c) => (
              <button
                key={c.name}
                type="button"
                className={`channels-rail-btn${
                  c.name === selectedName ? ' is-active' : ''
                }`}
                onClick={() => selectChannel(c.name)}
                data-testid={`channels-rail-${c.name}`}
                aria-current={c.name === selectedName ? 'page' : undefined}
              >
                {c.name}
              </button>
            ))
          )}
        </nav>

        <div className="channels-panes">
          <div className="channels-pane-tabs" role="tablist" aria-label="pane">
            <PaneTabButton
              id="status"
              label="Status"
              active={subPane === 'status'}
              onSelect={setSubPane}
            />
            <PaneTabButton
              id="users"
              label="Users"
              active={subPane === 'users'}
              onSelect={setSubPane}
            />
            <PaneTabButton
              id="conversations"
              label="Conversations"
              active={subPane === 'conversations'}
              onSelect={setSubPane}
            />
            <PaneTabButton
              id="logs"
              label="Logs"
              active={subPane === 'logs'}
              onSelect={setSubPane}
            />
          </div>
          <div className="channels-pane-body">
            {!selected && (
              <div className="channels-empty">
                Select a channel to inspect.
              </div>
            )}
            {selected && subPane === 'status' && (
              <StatusPane channel={selected} />
            )}
            {selected && subPane === 'users' && (
              <UsersPane channel={selected.name} />
            )}
            {selected && subPane === 'conversations' && (
              <ConversationsPane channel={selected.name} />
            )}
            {selected && subPane === 'logs' && (
              <LogsPane channel={selected.name} />
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

interface PaneTabButtonProps {
  id: SubPane;
  label: string;
  active: boolean;
  onSelect: (id: SubPane) => void;
}

function PaneTabButton({ id, label, active, onSelect }: PaneTabButtonProps) {
  return (
    <button
      type="button"
      role="tab"
      aria-selected={active}
      className={`channels-pane-tab${active ? ' is-active' : ''}`}
      onClick={() => onSelect(id)}
      data-testid={`channels-pane-${id}`}
    >
      {label}
    </button>
  );
}

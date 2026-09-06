// Channels. Whether anything the app writes can actually reach you.
//
// It replaces the Notifications pane, and the difference is not the name.
// Notifications listed the settings: eight categories, a mute toggle and a
// rate count. This room answers the question those settings are FOR, which is
// whether the operator is being reached, so the door leads: a bridge that is
// down makes every other fact here beside the point.
//
// **This file authors nothing about any kind.** Where each is sent, what holds
// it back and what may be done to it arrive in the payload, on the rule
// Managed system already follows: a view that decided would draw a control the
// write then refuses.

import { useEffect, useState } from 'react';
import { Note } from '../../components/common/Note';
import { Button } from '../../components/common/Button';
import { RowActions } from '../../components/common/Row';
import { Band, StateStrip, type StateLine } from '../../components/common/StateStrip';
import {
  ApiError,
  postNotificationMute,
  type ChannelsResponse,
} from '../../lib/api';
import { useAutonomyStore } from '../../stores/autonomy';
import { useChannelsStore } from '../../stores/channels';
import { useWebSocketStore } from '../../stores/websocket';
import { useToastStore } from '../../stores/toasts';
import { clock } from '../../lib/time';

/** The channel a mute is written against. One today, and the payload says
 *  where each kind goes, so a second one changes this file and not the room. */
const CHANNEL = 'telegram';

export function ChannelsRoomView({
  data,
  status,
  error,
  onMute,
  onRestart,
  busy,
}: {
  data: ChannelsResponse | null;
  status: 'idle' | 'loading' | 'ready' | 'error';
  error: string | null;
  onMute: (category: string, muted: boolean) => void;
  onRestart: (channel: string) => void;
  busy: Set<string>;
}): React.ReactElement {
  if (status === 'error') {
    return <Note tone="bad">What can reach you could not be read. {error}</Note>;
  }
  if (status !== 'ready' || data === null) return <></>;

  const doors: StateLine[] = data.adapters.map((door) => ({
    key: `door:${door.name}`,
    state: door.state,
    obligation: door.obligation,
    label: door.label,
    name: door.name,
    said: door.said,
    when: clock(door.at),
    value: door.value,
    actions: door.acts.includes('restart') ? (
      // Always visible: a channel that cannot reach you is exactly the row
      // whose remedy the operator came here for.
      <RowActions className="state-acts state-acts--waiting">
        <Button
          onClick={() => onRestart(door.name)}
          disabled={busy.has(door.name)}
          ariaLabel={`Restart ${door.name}`}
        >
          restart
        </Button>
      </RowActions>
    ) : undefined,
  }));

  const kinds: StateLine[] = data.kinds.map((kind) => ({
    key: kind.name,
    state: kind.state,
    obligation: kind.obligation,
    label: kind.label,
    name: kind.name,
    said: kind.said,
    when: clock(kind.at),
    value: kind.value,
    actions: kind.acts.length > 0 ? (
      <RowActions className="state-acts">
        <Button
          onClick={() => onMute(kind.name, !kind.muted)}
          disabled={busy.has(kind.name)}
          ariaLabel={`${kind.muted ? 'Unmute' : 'Mute'} ${kind.name}`}
        >
          {kind.muted ? 'unmute' : 'mute'}
        </Button>
      </RowActions>
    ) : undefined,
  }));

  return (
    <>
      {doors.length > 0 && (
        <div className="autonomy-group">
          <Band label="The way out" count={doors.length} />
          <StateStrip lines={doors} />
        </div>
      )}

      <div className="autonomy-group">
        <Band label="What it can send you" count={kinds.length} />
        <StateStrip lines={kinds} />
      </div>

      <div className="autonomy-group">
        <Band label="The last message it wrote you" count={data.lastMessage ? 1 : 0} />
        {data.lastMessage === null ? (
          <p className="t-meta">
            It has sent you nothing on this machine yet. What it sends is kept
            here as it goes out, so this is empty rather than unrecorded.
          </p>
        ) : (
          <StateStrip
            // The whole message, because nothing opens onto the rest of it.
            whole
            lines={[
              {
                key: 'last',
                state: 'idle',
                name: data.lastMessage.category,
                said: data.lastMessage.text,
                when: clock(data.lastMessage.at),
                value: data.lastMessage.channels.join(', '),
              },
            ]}
          />
        )}
      </div>
    </>
  );
}

export function ChannelsRoom(): React.ReactElement {
  const channels = useAutonomyStore((s) => s.channels);
  const fetchChannels = useAutonomyStore((s) => s.fetchChannels);
  const [busy, setBusy] = useState<Set<string>>(new Set());

  // The rail fills this whether or not the room is open, so opening it is
  // usually free. A room that lands on an empty store asks once.
  useEffect(() => {
    if (channels.status === 'idle') void fetchChannels();
  }, [channels.status, fetchChannels]);

  const hold = (key: string, run: () => Promise<unknown>) => {
    setBusy((b) => new Set(b).add(key));
    void run()
      // What it says afterwards is the backend's answer, never one composed
      // here from what was sent.
      .then(() => fetchChannels())
      .catch((err: unknown) => {
        const detail = err instanceof ApiError ? err.message : String(err);
        useToastStore.getState().push(detail, 'error');
      })
      .finally(() =>
        setBusy((b) => {
          const next = new Set(b);
          next.delete(key);
          return next;
        }),
      );
  };

  return (
    <ChannelsRoomView
      data={channels.data}
      status={channels.status}
      error={channels.error}
      busy={busy}
      onMute={(category, muted) => {
        const sid = useWebSocketStore.getState().sessionId;
        if (!sid) {
          useToastStore.getState().push('No session id. Connect first.', 'error');
          return;
        }
        hold(category, () =>
          postNotificationMute({ session_id: sid, channel: CHANNEL, category, muted }),
        );
      }}
      onRestart={(channel) => {
        const sid = useWebSocketStore.getState().sessionId;
        if (!sid) {
          useToastStore.getState().push('No session id. Connect first.', 'error');
          return;
        }
        // The store that already owns this act, with its approval outcome:
        // bouncing a bridge is gated, and a second path would be a second
        // answer to what the operator agreed to.
        hold(channel, async () => {
          const result = await useChannelsStore
            .getState()
            .restartChannel(channel, sid);
          if (!result.approved) {
            useToastStore.getState().push(result.output, 'error');
          }
        });
      }}
    />
  );
}

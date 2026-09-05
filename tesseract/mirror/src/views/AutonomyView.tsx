// The Autonomy panel. One screen that answers what needs the operator, what is
// executing, what just changed, and what is unhealthy, without opening
// anything.
//
// The shell is a rail of rooms beside one room template, and nothing sits
// above them. There was an operations strip there for a while: it spent about
// a fifth of the panel's height in every room, permanently, to say what
// Overview now says in its own first band, and the reference build has no such
// thing. `GET /api/autonomy/pipeline` stays, because that is where a night's
// steps come from for an entry card's own track.
//
// **The rail is the overview, not a menu.** Every row carries its room's own
// written line, so Health is readable without opening Health. The line is the
// backend's: `GET /api/autonomy/rooms` counts the facts and a model on the
// declared chain phrases them, dropping any figure nobody observed. Nothing
// here writes a word about the machine. `rooms.ts` owns the two things that
// are a reading rather than a description: the state mark, and the record
// lines in the bottom band.
//
// **Every room renders inside `RoomShell`** — a band at the top saying where
// you are and what is in this room, the room in the middle, a band at the
// bottom carrying what it just did. Built once, so moving between rooms never
// moves the furniture and no room draws a head of its own.
//
// **The map is level 4 and only level 4.** It answers how a thing is put
// together, which is a question asked on purpose about something already
// opened, so it is drawn beside an entry card rather than on the room the
// panel opens on.
//
// 2026-05-18: the scheduled-jobs pane was removed from here because scheduled
// work belonged to the Schedule tab. That was about DUPLICATION, the same jobs
// in two places. Scheduled work returns under a different rule, which is
// OWNERSHIP: each entry in exactly one place, decided by which tree its record
// lives in.

import { useEffect, useMemo } from 'react';
import { Button } from '../components/common/Button';
import { RailView, type RailGroup } from '../components/common/RailView';
import { useAutonomyStore } from '../stores/autonomy';
import { AgendaDetail } from './autonomy/AgendaDetail';
import { AtlasRoom } from './autonomy/AtlasRoom';
import { ThrownAwayRoom } from './autonomy/ThrownAwayRoom';
import { BlockedPane } from './autonomy/BlockedPane';
import { ChannelsRoom } from './autonomy/ChannelsRoom';
import { DecisionLogPane } from './autonomy/DecisionLogPane';
import { EntryCard } from './autonomy/EntryCard';
import { AgentCard } from './autonomy/AgentCard';
import { HealthRoom } from './autonomy/HealthRoom';
import { ManagedRoom } from './autonomy/ManagedRoom';
import { MachineMap } from './autonomy/MachineMap';
import { MemoryRoom } from './autonomy/MemoryRoom';
import { JournalPane } from './autonomy/JournalPane';
import { OverviewRoom } from './autonomy/OverviewRoom';
import { PrunedPane } from './autonomy/PrunedPane';
import { RecoveryPane, type RecoverySummaryPayload } from './autonomy/RecoveryPane';
import { RoomShell } from './autonomy/RoomShell';
import { roomLines as roomMarks } from './autonomy/rooms';
import { WorkerDetail } from './autonomy/WorkerDetail';

// `workers/active/` retains terminal records (done/failed/cancelled/…)
// until the archive janitor sweeps them. The header count is meant as a
// liveness signal — count live statuses only so it doesn't read "83
// workers" against a fleet of corpses.
const LIVE_WORKER_STATUSES = new Set([
  'queued',
  'spawning',
  'running',
  'awaiting_io',
]);

export function AutonomyView(): React.ReactElement {
  const agenda = useAutonomyStore((s) => s.agenda);
  const workers = useAutonomyStore((s) => s.workers);
  const governor = useAutonomyStore((s) => s.governor);
  const recovery = useAutonomyStore((s) => s.recovery);
  const journal = useAutonomyStore((s) => s.journal);
  const health = useAutonomyStore((s) => s.health);
  const managed = useAutonomyStore((s) => s.managed);
  const channels = useAutonomyStore((s) => s.channels);
  const memory = useAutonomyStore((s) => s.memory);
  const atlas = useAutonomyStore((s) => s.atlas);
  const retention = useAutonomyStore((s) => s.retention);
  const overview = useAutonomyStore((s) => s.overview);
  const roomLines = useAutonomyStore((s) => s.roomLines);
  const prunedLedger = useAutonomyStore((s) => s.pruned);
  const fetchAll = useAutonomyStore((s) => s.fetchAll);
  const clearLevels = useAutonomyStore((s) => s.clearLevels);
  // The store self-hydrates on WS connect (websocket.ts onopen). The
  // manual refresh below covers a hard refresh that lands on this view
  // before the WS has opened. Idempotent — Promise.allSettled internally.
  useEffect(() => {
    if (agenda.status === 'idle') {
      void fetchAll();
    }
  }, [agenda.status, fetchAll]);

  const recoveryPayload: RecoverySummaryPayload | null =
    recovery.data?.recovery ? { ...recovery.data.recovery } : null;
  const recoveryState: 'recovering' | 'ready' = recovery.data?.state ?? 'ready';

  const liveWorkerCount = workers.data.filter((w) =>
    LIVE_WORKER_STATUSES.has(w.status),
  ).length;

  const marks = useMemo(
    () =>
      roomMarks({
        overview: overview.data,
        items: agenda.data,
        pauses: governor.data?.pauses ?? [],
        journal: journal.data,
        health: health.data,
        managed: managed.data,
        pruned: prunedLedger,
        channels: channels.data,
        memory: memory.data,
        atlas: atlas.data,
        retention: retention.data,
      }),
    [
      overview.data,
      agenda.data,
      // The pauses, not the whole payload: a governor tick moves `last_tick`
      // every minute and would invalidate this on a field nothing here reads.
      governor.data?.pauses,
      journal.data,
      health.data,
      managed.data,
      prunedLedger,
      channels.data,
      memory.data,
      atlas.data,
      retention.data,
    ],
  );

  // The sentence each room opens with. Keyed by the same room keys the rail
  // uses, and empty until the feed lands: a room says nothing rather than
  // saying something this file made up.
  //
  // The counted facts the line was written from are NOT rendered. When no
  // model can be reached the route publishes those facts AS the sentence, so
  // printing them again underneath was the same paragraph twice, and the rest
  // of the time it was a second copy of the room's own contents. What it was
  // written from is in `GET /api/autonomy/rooms` for anyone tracing it.
  const lines = useMemo(
    () =>
      new Map(
        (roomLines.data?.rooms ?? []).map((room) => [room.key, room]),
      ),
    [roomLines.data],
  );
  const said = (key: string) => lines.get(key)?.said ?? '';
  // What the room is for, as opposed to what is in it tonight. Also the
  // backend's, and shown only at level 0: the operator opened the Journal and
  // had to ask in a chat what it was.
  const purpose = (key: string) => lines.get(key)?.purpose ?? '';

  const groups: RailGroup[] = [
    {
      label: 'Now',
      sections: [
        {
          key: 'overview',
          label: 'Overview',
          said: said('overview'), // the backend's sentence
          mark: marks.overview.mark,
          render: () => (
            <RoomShell
              room="Overview"
              said={said('overview')}
              purpose={purpose('overview')}
              tail={marks.overview.tail}
              root={() => <OverviewRoom />}
              level={(lvl) => {
                if (lvl.kind === 'agenda') {
                  const item = agenda.data.find((i) => i.id === lvl.id);
                  // The item was archived between the click and the render.
                  return item ? <AgendaDetail item={item} /> : null;
                }
                if (lvl.kind === 'worker') return <WorkerDetail />;
                if (lvl.kind === 'entry') return <EntryCard name={lvl.id} />;
                return null;
              }}
              // Level 4 is how the thing at level 3 is WIRED, and only the
              // machine's declared shape can answer that. An agenda item and a
              // worker are not on it, so the pane stays full width rather than
              // putting a picture of the whole machine beside one item.
              beside={(lvl) =>
                lvl.kind === 'entry' ? <MachineMap marked={lvl.id} /> : null
              }
            />
          ),
        },
        {
          key: 'blocked',
          label: 'Blocked & paused',
          said: said('blocked'), // the backend's sentence
          mark: marks.blocked.mark,
          render: () => (
            <RoomShell
              room="Blocked & paused"
              said={said('blocked')}
              purpose={purpose('blocked')}
              tail={marks.blocked.tail}
              root={() => <BlockedPane />}
              level={(lvl) => {
                if (lvl.kind !== 'agenda') return null;
                const item = agenda.data.find((i) => i.id === lvl.id);
                return item ? <AgendaDetail item={item} /> : null;
              }}
            />
          ),
        },
      ],
    },
    {
      label: 'Depth',
      sections: [
        {
          key: 'health',
          label: 'Health',
          said: said('health'), // the backend's sentence
          mark: marks.health.mark,
          render: () => (
            <RoomShell
              room="Health"
              said={said('health')}
              purpose={purpose('health')}
              tail={marks.health.tail}
              root={() => <HealthRoom />}
              level={(lvl) => (lvl.kind === 'entry' ? <EntryCard name={lvl.id} /> : null)}
              beside={(lvl) =>
                lvl.kind === 'entry' ? <MachineMap marked={lvl.id} /> : null
              }
            />
          ),
        },
        {
          key: 'managed',
          label: 'Managed system',
          said: said('managed'), // the backend's sentence
          mark: marks.managed.mark,
          render: () => (
            <RoomShell
              room="Managed system"
              said={said('managed')}
              purpose={purpose('managed')}
              tail={marks.managed.tail}
              root={() => <ManagedRoom />}
              level={(lvl) => {
                if (lvl.kind === 'entry') return <EntryCard name={lvl.id} />;
                if (lvl.kind === 'agent') return <AgentCard name={lvl.id} />;
                return null;
              }}
              // Only a declared entry is on the machine's map. An agent is
              // not, so the pane stays full width rather than drawing the
              // whole machine beside one card.
              beside={(lvl) =>
                lvl.kind === 'entry' ? <MachineMap marked={lvl.id} /> : null
              }
            />
          ),
        },
        {
          key: 'memory',
          label: 'Memory',
          said: said('memory'), // the backend's sentence
          mark: marks.memory.mark,
          render: () => (
            <RoomShell
              room="Memory"
              said={said('memory')}
              purpose={purpose('memory')}
              tail={marks.memory.tail}
              root={() => <MemoryRoom />}
            />
          ),
        },
        {
          key: 'channels',
          label: 'Channels',
          said: said('channels'), // the backend's sentence
          mark: marks.channels.mark,
          render: () => (
            <RoomShell
              room="Channels"
              said={said('channels')}
              purpose={purpose('channels')}
              tail={marks.channels.tail}
              root={() => <ChannelsRoom />}
            />
          ),
        },
        {
          key: 'atlas',
          label: 'Atlas',
          said: said('atlas'), // the backend's sentence
          mark: marks.atlas.mark,
          render: () => (
            <RoomShell
              room="Atlas"
              said={said('atlas')}
              purpose={purpose('atlas')}
              tail={marks.atlas.tail}
              root={() => <AtlasRoom />}
            />
          ),
        },
      ],
    },
    {
      label: 'Record',
      sections: [
        // Recent outcomes is a record of what finished, which is what this
        // group is. It sat under Depth while the rooms beside it were live
        // readings of the machine, and the reference build files it here.
        {
          key: 'outcomes',
          label: 'Recent outcomes',
          said: said('outcomes'), // the backend's sentence
          mark: marks.outcomes.mark,
          render: () => (
            <RoomShell
              room="Recent outcomes"
              said={said('outcomes')}
              purpose={purpose('outcomes')}
              tail={marks.outcomes.tail}
              root={() => (
                <>
                  <RecoveryPane
                    summary={recoveryPayload}
                    recoveryState={recoveryState}
                  />
                  <DecisionLogPane
                    items={agenda.data}
                    lastTick={governor.data?.last_tick ?? null}
                  />
                </>
              )}
            />
          ),
        },
        {
          key: 'journal',
          label: 'Journal',
          said: said('journal'), // the backend's sentence
          mark: marks.journal.mark,
          render: () => (
            <RoomShell
              room="Journal"
              said={said('journal')}
              purpose={purpose('journal')}
              tail={marks.journal.tail}
              root={() => (
                <JournalPane
                  rows={journal.data}
                  status={journal.status}
                  error={journal.error}
                />
              )}
            />
          ),
        },
        {
          key: 'pruned',
          label: 'Pruned',
          said: said('pruned'), // the backend's sentence
          mark: marks.pruned.mark,
          render: () => (
            <RoomShell
              room="Pruned"
              said={said('pruned')}
              purpose={purpose('pruned')}
              tail={marks.pruned.tail}
              root={() => <PrunedPane />}
            />
          ),
        },
        // Next to Pruned deliberately: what was dropped before it became work
        // and what was aged out after the work was done are the same question
        // asked at two ends of a life.
        {
          key: 'retention',
          label: 'Thrown away',
          said: said('retention'), // the backend's sentence
          mark: marks.retention.mark,
          render: () => (
            <RoomShell
              room="Thrown away"
              said={said('retention')}
              purpose={purpose('retention')}
              tail={marks.retention.tail}
              root={() => <ThrownAwayRoom />}
            />
          ),
        },
      ],
    },
  ];

  return (
    <div className="autonomy-view" data-testid="autonomy-view">
      <RailView
      view="autonomy"
        groups={groups}
        label="Autonomy rooms"
        chrome="bare"
        collapsible
        // Clicking a room comes back to that room, including the one already
        // open. `RoomShell` drops the trail when the room CHANGES, which left
        // the only way out of a card being a key nobody is told about: a
        // person three levels down clicked the row they were in and nothing
        // happened.
        onSectionChange={() => clearLevels()}
        foot={
          <div className="autonomy-rail-foot">
            <span>
              {governor.data?.running ? 'governor running' : 'governor offline'}
              {' · '}
              {liveWorkerCount} worker{liveWorkerCount === 1 ? '' : 's'}
              {' · '}
              {agenda.data.length} agenda
            </span>
            <Button onClick={() => void fetchAll()} ariaLabel="refresh autonomy state">
              refresh
            </Button>
          </div>
        }
      />
    </div>
  );
}

// The room template. Three fixed containers, and every room on this panel
// renders inside them.
//
//   top     where you are, and what is in this room
//   middle  the room. The only part that scrolls.
//   bottom  what just happened here, as time, source, what happened
//
// Neither band scrolls and both run the full width, so moving between rooms
// never moves the furniture. It is built once here rather than assembled per
// room: a room that draws its own header is how a panel ends up with a
// different head in every tab, which is what the app already fixed once.
//
// It also owns the levels. Selecting something pushes a level and the middle
// draws THAT instead, with the breadcrumb in the top band and escape to go up
// one. Level 4, the wiring of whatever is open, renders BESIDE level 3 rather
// than replacing it. Nothing opens a window: a floor plan you cannot see past
// has stopped being a floor plan.

import { useEffect, type ReactNode } from 'react';
import { Breadcrumb, type Crumb } from '../../components/common/Breadcrumb';
import { useAutonomyStore, type AutonomyLevel } from '../../stores/autonomy';

/** One line of what a room just did. Every field is the producer's. */
export interface TailLine {
  key: string;
  /** What orders the band, newest first. The raw stamp, not the clock: the
   *  clock is already formatted and two of them sort alphabetically. */
  sortKey: string;
  /** The clock, as the producer wrote it. */
  at: string;
  /** Which part of the machine said it. */
  source: string;
  text: string;
  severity?: 'info' | 'warn' | 'bad';
}

export interface RoomTail {
  /** What this band is showing, in the producer's terms. */
  label: string;
  lines: TailLine[];
  /** Set when nothing records this room's activity yet. Rendered instead of
   *  the lines, because a band that goes quiet when its camera is unplugged
   *  says nothing is happening when it means nothing is watching. */
  unwired?: string;
}

interface RoomShellProps {
  /** What the rail row is called. The first crumb, and the room at level 0. */
  room: string;
  /** The room's own written line. The backend's words, never the view's, and
   *  it arrives with AR-18; until then a room shows its counted facts. */
  said?: string;
  /** What the room is FOR, in one sentence. Shown only at level 0, which is
   *  where a reader asks it: three levels down they are reading one thing, not
   *  wondering what the room does. It is the backend's sentence for the same
   *  reason `said` is, and it is a different question from `said` — one is
   *  about the room, the other is about tonight. */
  purpose?: string;
  /** Counted facts, shown when there is no written line yet. */
  facts?: ReactNode;
  tail: RoomTail;
  root: () => ReactNode;
  /** Renders one level down. Returning null means the room cannot draw that
   *  level, and the pane says so rather than showing an empty frame. */
  level?: (level: AutonomyLevel, depth: number) => ReactNode;
  /** Level 4: how the thing at level 3 is wired, beside it. Returning null
   *  leaves the middle at full width, which is the right answer for anything
   *  whose wiring nothing declares. */
  beside?: (level: AutonomyLevel, depth: number) => ReactNode;
}

export function RoomShell({
  room,
  said,
  purpose,
  facts,
  tail,
  root,
  level,
  beside,
}: RoomShellProps): React.ReactElement {
  const levels = useAutonomyStore((s) => s.levels);
  const goToLevel = useAutonomyStore((s) => s.goToLevel);
  const clearLevels = useAutonomyStore((s) => s.clearLevels);

  // A trail that survives into a different room names a place you are no
  // longer in.
  useEffect(() => clearLevels, [room, clearLevels]);

  // Escape goes up one, which is the other half of the breadcrumb. Bound on
  // the document because the pane is not focused when a row was clicked.
  useEffect(() => {
    if (levels.length === 0) return undefined;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape' || e.defaultPrevented) return;
      goToLevel(levels.length - 1);
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [levels.length, goToLevel]);

  const here = levels.length > 0 ? levels[levels.length - 1] : null;
  const crumbs: Crumb[] = [
    { key: 'root', label: room },
    ...levels.map((l) => ({ key: `${l.kind}:${l.id}`, label: l.label })),
  ];

  let body: ReactNode;
  if (here === null) {
    body = root();
  } else {
    const drawn = level?.(here, levels.length) ?? null;
    body = drawn ?? (
      <p className="t-meta">This room cannot open a {here.kind} yet.</p>
    );
  }
  const wiring = here ? (beside?.(here, levels.length) ?? null) : null;

  return (
    <div className="room" data-testid="room-shell">
      <div className="room__top">
        <div className="room__where t-meta t-label">
          <Breadcrumb
            crumbs={crumbs}
            onGo={goToLevel}
            trailing={levels.length > 0 ? 'esc goes back one' : undefined}
          />
        </div>
        {said ? (
          <p className="room__said">{said}</p>
        ) : facts ? (
          <div className="room__facts">{facts}</div>
        ) : null}
        {purpose && levels.length === 0 && (
          <p className="room__purpose t-meta">{purpose}</p>
        )}
      </div>

      <div className="room__mid">
        <div className="room__main">{body}</div>
        {wiring && <aside className="room__side">{wiring}</aside>}
      </div>

      <div className="room__bottom">
        <div className="room__tail-label t-meta t-label">{tail.label}</div>
        {tail.unwired ? (
          <p className="room__unwired t-meta">{tail.unwired}</p>
        ) : tail.lines.length === 0 ? (
          <p className="room__unwired t-meta">Nothing yet.</p>
        ) : (
          tail.lines.map((line) => (
            <div
              key={line.key}
              className={`room__tail-line${
                line.severity && line.severity !== 'info'
                  ? ` room__tail-line--${line.severity}`
                  : ''
              }`}
            >
              <span className="room__tail-at">{line.at}</span>
              <span className="room__tail-src">{line.source}</span>
              <span className="room__tail-text">{line.text}</span>
            </div>
          ))
        )}
      </div>
    </div>
  );
}

// What it throws away.
//
// Two different things get called pruning and only one of them was visible.
// The agenda's admission gate has its own room next door; what a *file* sweep
// did had no surface at all, so the machine deleted things nightly and the
// only way to know what was to notice something missing.
//
// Three bands, and the third is the reason the room exists. A tree with a
// window is a decision somebody made. A tree kept on purpose is also a
// decision somebody made. A tree with neither looks exactly like both from a
// directory listing, and that is how one log reached 868 KB in eleven days
// while five trees around it had windows.
//
// **This file writes nothing about a tree.** The window, what the last sweep
// did, what would be lost and what to do about a tree nobody has decided on
// are all the payload's sentences. The room's own words are the three band
// labels and nothing else.

import { useEffect, useState } from 'react';
import { Button } from '../../components/common/Button';
import { Input } from '../../components/common/Input';
import { Note } from '../../components/common/Note';
import { RowActions } from '../../components/common/Row';
import { Band, StateStrip, type StateLine } from '../../components/common/StateStrip';
import { sendCommand } from '../../lib/commands';
import type { RetentionLine, RetentionResponse } from '../../lib/api';
import { useAutonomyStore } from '../../stores/autonomy';
import { useStaleStore } from '../../stores/stale';
import { clock } from '../../lib/time';

// The sweep runs once a night and the sizes behind this are a walk over every
// log directory, which the backend holds for two minutes. It is here so a
// window changed from this room shows up without the operator refreshing, not
// because the file tree moves: the same reason Atlas polls at all.
const POLL_MS = 90_000;

/** The tool that changes a window, by name. The same one a channel calls, so
 *  the field here and the sentence said on a phone reach one implementation
 *  and one gate. */
const SET_WINDOW = '/retention_set_window';

/** The shortest window the runtime accepts. Stated here only as the field's
 *  own floor, so a browser refuses before a round trip does; the runtime
 *  refuses it again either way, and its sentence is the one the operator
 *  reads. */
const SHORTEST = 1;

/** What the backend calls this room's reading when it says one is out of date.
 *  The other half of `orchestrator/panel_refresh.py::RETENTION_KEY`, and the
 *  reason the poll above can stay slow: a window is the one thing here that
 *  moves because somebody moved it, and they are looking at the row when it
 *  does. */
const STALE_KEY = 'autonomy.retention';

function lines(
  rows: RetentionLine[],
  actionsFor?: (row: RetentionLine) => React.ReactNode,
): StateLine[] {
  return rows.map((row) => ({
    key: row.name,
    state: row.state,
    obligation: row.obligation,
    label: row.label,
    name: row.name,
    said: (
      <>
        <span>{row.said}</span>
        {row.detail && <span className="t-meta entry-step__for">{row.detail}</span>}
      </>
    ),
    when: clock(row.at),
    value: row.value,
    actions: actionsFor?.(row),
  }));
}

/** What is typed into one row's field and not yet sent, with the window it was
 *  typed against. The second half is what makes a draft a draft: a row whose
 *  window has since moved is showing a number about a policy that no longer
 *  exists, and the field goes back to what is in force. Held per tree, because
 *  one row's window moving says nothing about what is being typed into
 *  another. */
interface Draft {
  against: number;
  text: string;
}

/** How long to keep one tree, on the row that says what happens to it.
 *
 * The window is the one thing about a sweep that is the operator's, and until
 * now the only way to change it was an editor on this machine. The field is a
 * draft until it is sent: typing a number changes nothing, which is what keeps
 * a stray keystroke from shortening the record an investigation would read.
 *
 * **What it shows after a send is what is in force, never what was asked.**
 * The tool is gated, so the write happens when the operator answers the gate
 * and can be refused there. A field still reading 45 while the sweep is on 30
 * is a control reporting a result it never had.
 */
function Window({
  row,
  draft,
  onDraft,
  onSent,
}: {
  row: RetentionLine;
  draft: Draft | undefined;
  onDraft: (next: string) => void;
  onSent: () => void;
}): React.ReactElement | null {
  if (!row.tree || row.days === null) return null;
  const held = draft !== undefined && draft.against === row.days;
  const shown = held ? draft.text : String(row.days);
  const asked = Number(shown);
  const sendable =
    shown !== '' && Number.isInteger(asked) && asked >= SHORTEST && asked !== row.days;
  return (
    <RowActions className="state-acts">
      <Input
        type="number"
        value={shown}
        onChange={onDraft}
        min={SHORTEST}
        step={1}
        className="retention-days"
        ariaLabel={`Days to keep ${row.name}`}
        ariaInvalid={held && !sendable}
      />
      <Button
        onClick={() => {
          sendCommand(SET_WINDOW, ` tree=${row.tree} days=${asked}`);
          onSent();
        }}
        disabled={!sendable}
        ariaLabel={`Keep ${row.name} for ${shown} days`}
      >
        keep
      </Button>
    </RowActions>
  );
}

export function ThrownAwayRoomView({
  data,
  status,
  error,
}: {
  data: RetentionResponse | null;
  status: 'idle' | 'loading' | 'ready' | 'error';
  error: string | null;
}): React.ReactElement {
  // What is typed and not yet sent, per tree. There is no effect clearing
  // this: a draft carries the window it was typed against, so a row goes back
  // to what is in force exactly when ITS OWN window moves. Clearing the map on
  // any change would take a number the operator is still typing into one row
  // because a poll found a different row changed elsewhere.
  const [drafts, setDrafts] = useState<Record<string, Draft>>({});

  if (status === 'error') {
    return <Note tone="bad">What this machine ages could not be read. {error}</Note>;
  }
  if (status !== 'ready' || data === null) return <></>;

  return (
    <>
      {/* Set only when no sweep has run here, or when its record could not be
          read. Those are two different claims and the backend picks which. */}
      {data.lastSweepSaid && <Note>{data.lastSweepSaid}</Note>}

      <div className="autonomy-group">
        <Band label="What ages" count={data.ages.length} />
        <StateStrip
          whole
          lines={lines(data.ages, (row) => (
            <Window
              row={row}
              draft={drafts[row.tree]}
              onDraft={(text) =>
                setDrafts((held) => ({
                  ...held,
                  [row.tree]: { against: row.days ?? 0, text },
                }))
              }
              onSent={() =>
                setDrafts(({ [row.tree]: _gone, ...rest }) => rest)
              }
            />
          ))}
        />
      </div>

      <div className="autonomy-group">
        <Band label="Kept on purpose" count={data.kept.length} />
        <StateStrip whole lines={lines(data.kept)} />
      </div>

      <div className="autonomy-group">
        <Band label="Nothing has decided" count={data.undecided.length} />
        {data.undecided.length === 0 ? (
          <p className="t-meta">
            Everything under the log trees is either aged or kept on purpose.
          </p>
        ) : (
          <StateStrip whole lines={lines(data.undecided)} />
        )}
      </div>
    </>
  );
}

export function ThrownAwayRoom(): React.ReactElement {
  const retention = useAutonomyStore((s) => s.retention);
  const fetchRetention = useAutonomyStore((s) => s.fetchRetention);
  // A counter, not a flag: two windows changed in a row are two nudges, and
  // nothing here has to clear it.
  const staleTick = useStaleStore((s) => s.ticks[STALE_KEY] ?? 0);

  useEffect(() => {
    void fetchRetention();
    const timer = window.setInterval(() => void fetchRetention(), POLL_MS);
    return () => window.clearInterval(timer);
  }, [fetchRetention, staleTick]);

  return (
    <ThrownAwayRoomView
      data={retention.data}
      status={retention.status}
      error={retention.error}
    />
  );
}

// Memory. The library it works from, and what last night changed in it.
//
// Three bands, in the order a reader needs them: whether it can be searched
// at all, what is in it, and what the nightly pass did. Retrieval leads for
// the reason the door leads in Channels: a library nothing can search is a
// library that is not being used, whatever is in it.
//
// **This file authors nothing about the library.** Every count, every size and
// every sentence is the payload's, and the steps of the last pass carry the
// stage's own reason, rendered as it was written.
//
// Two controls, both existing tools and neither invented here: a failed step
// of the last pass can be run again, off its schedule, and the vault can be
// checked on demand. Neither writes a sentence of its own; what happened is
// read back from the room the next time it asks.

import { useEffect } from 'react';
import { Button } from '../../components/common/Button';
import { Note } from '../../components/common/Note';
import { RowActions } from '../../components/common/Row';
import { Hint } from '../../components/ui/Hint';
import { Band, StateStrip, type StateLine } from '../../components/common/StateStrip';
import { sendCommand } from '../../lib/commands';
import type { MemoryLine, MemoryResponse } from '../../lib/api';
import { useAutonomyStore } from '../../stores/autonomy';
import { clock } from '../../lib/time';

/** The vault is the one tree here with a tool that reads it. A tree nothing
 *  has written yet has nothing to check, which is why the row that says so
 *  carries no button either: `vault_lint` would only confirm the same
 *  emptiness the row already states. */
function treeActions(line: MemoryLine): React.ReactNode {
  if (line.name !== 'vault' || line.state === 'not_instrumented') return undefined;
  return (
    <RowActions className="state-acts">
      <Hint label="Checks the vault wiki for orphans, stale pages, contradictions and missing hubs, and writes down what it can fix safely.">
        <Button onClick={() => sendCommand('/vault_lint')} ariaLabel="Check the vault now">
          check it
        </Button>
      </Hint>
    </RowActions>
  );
}

function rows(
  lines: MemoryResponse['trees'],
  actionsFor?: (line: MemoryLine) => React.ReactNode,
): StateLine[] {
  return lines.map((line) => ({
    key: line.name,
    state: line.state,
    obligation: line.obligation,
    label: line.label,
    name: line.name,
    said: line.said,
    when: clock(line.at),
    value: line.value,
    actions: actionsFor?.(line),
  }));
}

export function MemoryRoomView({
  data,
  status,
  error,
}: {
  data: MemoryResponse | null;
  status: 'idle' | 'loading' | 'ready' | 'error';
  error: string | null;
}): React.ReactElement {
  if (status === 'error') {
    return <Note tone="bad">The library could not be read. {error}</Note>;
  }
  if (status !== 'ready' || data === null) return <></>;

  return (
    <>
      <p className="t-meta">
        Whether the library can be searched right now, what is on disk, and
        what last night&apos;s maintenance pass did to it.
      </p>

      <div className="autonomy-group">
        <Band label="Whether it can be searched" count={data.retrieval.length} />
        <StateStrip lines={rows(data.retrieval)} />
      </div>

      <div className="autonomy-group">
        <Band label="What is in it" count={data.trees.length} />
        <StateStrip lines={rows(data.trees, treeActions)} />
      </div>

      <div className="autonomy-group">
        <Band label="What the last pass did to it" count={data.lastNight.length} />
        {data.lastNight.length === 0 ? (
          // The backend says WHY there is nothing: never run, unreadable
          // record, or a pass that touched nothing. Three different claims,
          // and one sentence here would flatten them back into one.
          <p className="t-meta">{data.lastNightSaid}</p>
        ) : (
          <StateStrip
            // Nothing opens onto the rest of a step's reason, so it is shown
            // whole rather than clamped to two lines.
            whole
            lines={data.lastNight.map((step) => ({
              key: step.stage,
              state: step.state,
              label: step.label,
              name: step.stage,
              said: (
                <>
                  {step.reason && <span>{step.reason}</span>}
                  {/* What the step is FOR, from the stage that declares it. */}
                  {step.summary && (
                    <span className="t-meta entry-step__for">{step.summary}</span>
                  )}
                </>
              ),
              when: step.took,
              value: step.changed ? `${step.changed} changed` : '',
              // A step that found nothing to do is the commonest healthy
              // outcome and gets no button. A step that FAILED is the one
              // that most needs one, so its control shows without being
              // hovered, the same as a row that is asking for attention.
              actions:
                step.state === 'failed' ? (
                  <RowActions className="state-acts state-acts--waiting">
                    <Hint label="Runs this one step of tonight's maintenance again, right now, outside its schedule.">
                      <Button
                        onClick={() =>
                          sendCommand(
                            '/pipeline_run_stage',
                            ` stage=${JSON.stringify(step.stage)}`,
                          )
                        }
                        ariaLabel={`Run ${step.stage} again`}
                      >
                        run it again
                      </Button>
                    </Hint>
                  </RowActions>
                ) : undefined,
            }))}
          />
        )}
      </div>
    </>
  );
}

export function MemoryRoom(): React.ReactElement {
  const memory = useAutonomyStore((s) => s.memory);
  const fetchMemory = useAutonomyStore((s) => s.fetchMemory);

  // The rail fills this whether or not the room is open, so opening it is
  // usually free. A room that lands on an empty store asks once.
  useEffect(() => {
    if (memory.status === 'idle') void fetchMemory();
  }, [memory.status, fetchMemory]);

  return (
    <MemoryRoomView data={memory.data} status={memory.status} error={memory.error} />
  );
}
